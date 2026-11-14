"""Genesis: the network the gallery would have grown, replayed from publish dates.

Runs as the worker job `mycelium_genesis`, never in the api, and only while the
organism is stopped and no gallery address is public. Tick 0 is the ten-minute
mark at which the live corpus first holds `centered_min_images` pictures with a
description vector, so the graph is centered from the first tick, as a live
rebuild of that corpus would be; pictures published before it enter together,
later ones at the tick whose window holds their publish time. Every graph uses
today's corpus mean. The run ends at the last tick scheduled before now and
leaves the clock on the next one.

The step is the same pure code the live tick runs (tick.build_graph, tick.step,
tick.stats). There are no clicks in the past, and base overgrowth feeds only the
stored neighbor level, so it is computed for the last tick alone.

One transaction holds the state row from the refusal checks to the commit: the
wipe and the whole replay land together or not at all, and a failed run leaves
the network as it was. Publishes, deletes and embedding writes wait on the state
row meanwhile. Readers do not: the wipe deletes rather than truncates, so image
pages and click checks see the old network until the commit, and neighbor levels
are written only at the end, so views never wait on the replay.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from penish.config import Settings, get_settings
from penish.mycelium import colonies as colony
from penish.mycelium import engine, store, tick
from penish.mycelium.graph import Edge
from penish.mycelium.tunables import TICK_SECONDS
from penish.worker.jobs import HardError, Job, Pool

log = logging.getLogger(__name__)

JOB = "mycelium_genesis"
PROGRESS_EVERY = 1000


class GenesisRefused(Exception):
    """Nothing was touched: the organism is running, galleries are public, or the
    corpus never reached the size the replay starts from."""


class GenesisMismatch(Exception):
    """The replayed graph is not the one a live rebuild makes; the run rolls back."""


@dataclass(frozen=True, slots=True)
class Plan:
    # Scheduled time of tick 0.
    start: datetime
    # Picture id to the tick it enters at.
    entry: dict[int, int]
    last_tick: int

    def scheduled_at(self, k: int) -> datetime:
        return self.start + k * store.TICK


def plan(
    published: Mapping[int, datetime | None],
    counted: Collection[int],
    threshold: int,
    now: datetime,
) -> Plan:
    """When each live picture enters, and how many ticks fit before `now`. Pure.

    `counted` are the pictures with a vector; tick 0 is the publish time of the
    `threshold`-th of them (ties by id). A live picture without a publish time has
    been there all along and enters at tick 0.
    """
    need = max(1, threshold)
    dated = sorted((at, pid) for pid, at in published.items() if pid in counted and at is not None)
    undated = sum(1 for pid in counted if pid in published and published[pid] is None)
    if not dated:
        raise GenesisRefused("no publish dates")
    if len(dated) + undated < need:
        raise GenesisRefused(f"fewer than {need} pictures")
    start = dated[max(0, need - 1 - undated)][0]
    entry: dict[int, int] = {}
    for pid, at in published.items():
        secs = 0.0 if at is None else (at - start).total_seconds()
        entry[pid] = max(0, math.floor(secs / TICK_SECONDS))
    last = math.floor((now - start).total_seconds() / TICK_SECONDS)
    if last < 0:
        raise GenesisRefused("nothing to replay")
    return Plan(start, entry, last)


@dataclass(frozen=True, slots=True)
class Genesis:
    start: datetime
    last_tick: int
    next_due: datetime
    events: int
    graph_version: int
    state_hash: str
    colonies_hash: str


async def run(
    conn: store.Conn, *, now: datetime | None = None, settings: Settings | None = None
) -> Genesis:
    """Wipe the network and replay it up to now. Raises GenesisRefused before touching
    anything, GenesisMismatch after (rolled back)."""
    cfg = get_settings() if settings is None else settings
    async with conn.transaction():
        st = await store.read_state(conn, lock=True)
        if st.running:
            raise GenesisRefused("running")
        if st.public:
            raise GenesisRefused("public")
        clock = st.now if now is None else now
        tunables, tversion, _ = await store.current_tunables(conn)
        inputs = await store.load_graph_inputs(conn, model=cfg.embed_model, dim=cfg.embed_dim)
        published = await store.live_published(conn)
        slugs = await store.live_slugs(conn)
        p = plan(published, inputs.vectors.keys(), cfg.centered_min_images, clock)
        log.info(
            "genesis from %s, %d ticks, %d pictures",
            p.start.isoformat(),
            p.last_tick + 1,
            len(p.entry),
        )

        # Levels are rewritten in the final few statements instead, so views of
        # pictures with a level do not wait on this transaction for the whole replay.
        await store.wipe_network(conn, levels=False)

        arrivals = sorted(p.entry, key=lambda pid: (p.entry[pid], pid))
        nxt = 0
        live: list[int] = []
        grown: dict[int, store.FloatArray] = {}
        graph: list[Edge] = []
        graph_version = st.graph_version
        states: Mapping[str, engine.LayerState] = {}
        col = colony.ColoniesState()
        events = 0
        last: tick.Stepped | None = None

        for k in range(p.last_tick + 1):
            at = p.scheduled_at(k)
            arrived = False
            while nxt < len(arrivals) and p.entry[arrivals[nxt]] <= k:
                pid = arrivals[nxt]
                bisect.insort(live, pid)
                if pid in inputs.vectors:
                    grown[pid] = inputs.vectors[pid]
                    arrived = True
                nxt += 1
            if arrived:
                # Only an arrival with a vector changes who may grow toward whom.
                sub = store.GraphInputs(dict(sorted(grown.items())), inputs.mean)
                graph = await asyncio.to_thread(
                    tick.build_graph, sub, tunables, centered_min=cfg.centered_min_images
                )
                graph_version += 1
            base = await store.base_overgrowth(conn, live, at, tunables) if k == p.last_tick else {}
            s = await asyncio.to_thread(
                tick.step, states, {tick.WHOLE: graph}, {}, live, base, tunables, st.seed, k, col
            )
            await store.apply_colonies(
                conn,
                col,
                s.colonies,
                s.events,
                tick=k,
                scheduled_at=at,
                simulated=True,
                slugs=slugs,
                latched=s.latched,
            )
            await store.insert_tick(
                conn,
                store.TickRow(
                    tick=k,
                    scheduled_at=at,
                    simulated=True,
                    state_hash=engine.state_hash(s.result.states),
                    colonies_hash=colony.state_hash(s.colonies),
                    stats=tick.stats(s, live, {}),
                    tunables_version=tversion,
                    graph_version=graph_version,
                ),
            )
            states = s.result.states
            col = s.colonies
            events += len(s.events)
            last = s
            if (k + 1) % PROGRESS_EVERY == 0:
                log.info("genesis tick %d of %d", k, p.last_tick)

        assert last is not None
        fresh = await store.load_graph_inputs(conn, model=cfg.embed_model, dim=cfg.embed_dim)
        rebuilt = await asyncio.to_thread(
            tick.build_graph, fresh, tunables, centered_min=cfg.centered_min_images
        )
        if rebuilt != graph:
            raise GenesisMismatch(
                f"replayed graph has {len(graph)} edges, a live rebuild {len(rebuilt)}"
            )

        await store.replace_candidate_edges(conn, tick.WHOLE, graph)
        await store.replace_edge_states(conn, states)
        await store.clear_neighbor_levels(conn, keep=sorted(last.result.neighbor_level))
        await store.write_neighbor_levels(conn, last.result.neighbor_level)
        next_due = p.scheduled_at(p.last_tick + 1)
        await store.save_state(
            conn,
            last_tick=p.last_tick,
            next_due=next_due,
            colonies=col.to_dict(),
            graph_version=graph_version,
            graph_dirty=False,
            tunables_version=tversion,
            simulated_through=p.last_tick,
        )
    out = Genesis(
        start=p.start,
        last_tick=p.last_tick,
        next_due=next_due,
        events=events,
        graph_version=graph_version,
        state_hash=engine.state_hash(states),
        colonies_hash=colony.state_hash(col),
    )
    log.info("genesis done through tick %d, %d events", out.last_tick, out.events)
    return out


async def handle(job: Job, *, pool: Pool, settings: Settings) -> None:
    """The worker job. Never retried: a refusal stays a refusal, and a run that broke
    halfway is something to look at before pressing the button again."""
    try:
        async with pool.connection() as conn:
            await run(conn, settings=settings)
    except Exception as e:
        raise HardError(str(e) or type(e).__name__) from e
