"""Genesis: the network the gallery would have grown, replayed from publish dates.

Runs as the worker job `mycelium_genesis`, never in the api, and only while the
organism is stopped and no gallery address is public. Tick 0 is the ten-minute
mark at which the live corpus first holds `centered_min_images` pictures with a
description vector, so the graph is centered from the first tick, as a live
rebuild of that corpus would be; pictures published before it enter together,
later ones at the tick whose window holds their publish time. Every graph uses
today's corpus mean. The run ends at the last tick scheduled before now and
leaves the clock on the next one.

The step is the same pure code the live tick runs (tick.build_graphs, tick.step,
tick.stats). There are no clicks in the past, and base overgrowth feeds only the
stored neighbor level, so it is computed for the last tick alone.

A genesis of some facet layers (`layers`) replays only them over the ticks already
stored, at their recorded times, and is allowed after galleries are public as long
as no colony in those layers ever had a serial. See `run_layers`.

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
from collections.abc import Collection, Iterable, Mapping, Sequence
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


def scope(layers: Iterable[str] | None) -> tuple[str, ...] | None:
    """None for a full genesis, else the facet layers to replay in layer order.

    Every active layer named is a full genesis. The whole layer defines the clock the
    others replay against, so it never replays alone or beside only some facets.
    """
    if layers is None:
        return None
    chosen = set(layers)
    unknown = sorted(chosen - set(tick.ACTIVE_LAYERS))
    if unknown:
        raise GenesisRefused(f"unknown layer {unknown[0]}")
    if not chosen:
        raise GenesisRefused("no layers")
    if chosen == set(tick.ACTIVE_LAYERS):
        return None
    if tick.WHOLE in chosen:
        raise GenesisRefused("whole replays with every layer")
    return tuple(n for n in tick.ACTIVE_LAYERS if n in chosen)


def entries_at(
    published: Mapping[int, datetime | None], times: Sequence[datetime]
) -> dict[int, int]:
    """For a replay over stored ticks: the index in `times` each live picture enters at.

    The tick whose window holds the publish time, or the first after it when the clock
    was paused then; before the first tick, or with no publish time, the first; after
    the last window, the last, so the replay ends on the corpus a live rebuild reads.
    """
    if not times:
        return {}
    ends = [at + store.TICK for at in times]
    last = len(times) - 1
    return {
        pid: 0 if at is None else min(last, bisect.bisect_right(ends, at))
        for pid, at in published.items()
    }


@dataclass(frozen=True, slots=True)
class Genesis:
    start: datetime
    last_tick: int
    next_due: datetime | None
    events: int
    graph_version: int
    state_hash: str
    colonies_hash: str


async def run(
    conn: store.Conn,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    layers: Iterable[str] | None = None,
) -> Genesis:
    """Wipe the network and replay it up to now. Raises GenesisRefused before touching
    anything, GenesisMismatch after (rolled back). `layers` naming only facet layers
    replays just those (`run_layers`)."""
    cfg = get_settings() if settings is None else settings
    scoped = scope(layers)
    if scoped is not None:
        return await run_layers(conn, scoped, settings=cfg)
    async with conn.transaction():
        st = await store.read_state(conn, lock=True)
        if st.running:
            raise GenesisRefused("running")
        if st.public:
            raise GenesisRefused("public")
        clock = st.now if now is None else now
        tunables, tversion, _ = await store.current_tunables(conn)
        inputs = await store.load_graph_inputs(conn, model=cfg.embed_model, dim=cfg.embed_dim)
        facet_inputs = await store.load_facet_inputs(
            conn, recipe=st.facet_recipe, model=cfg.embed_model, dim=cfg.embed_dim
        )
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
        grown_facets: dict[str, dict[int, store.FloatArray]] = {n: {} for n in facet_inputs}
        graphs: dict[str, list[Edge]] = {n: [] for n in tick.ACTIVE_LAYERS}
        graph_version = st.graph_version
        states: Mapping[str, engine.LayerState] = {}
        col = colony.ColoniesState()
        events = 0
        last: tick.Stepped | None = None

        for k in range(p.last_tick + 1):
            at = p.scheduled_at(k)
            arrived = False
            faceted = False
            while nxt < len(arrivals) and p.entry[arrivals[nxt]] <= k:
                pid = arrivals[nxt]
                bisect.insort(live, pid)
                if pid in inputs.vectors:
                    grown[pid] = inputs.vectors[pid]
                    arrived = True
                for name, inp in facet_inputs.items():
                    if pid in inp.vectors:
                        grown_facets[name][pid] = inp.vectors[pid]
                        faceted = True
                nxt += 1
            # Only an arrival with a vector changes who may grow toward whom.
            if arrived:
                sub = store.GraphInputs(dict(sorted(grown.items())), inputs.mean)
                graphs[tick.WHOLE] = await asyncio.to_thread(
                    tick.build_graph, sub, tunables, centered_min=cfg.centered_min_images
                )
            if faceted:
                graphs.update(
                    await asyncio.to_thread(
                        tick.build_facet_graphs,
                        _arrived(facet_inputs, grown_facets),
                        tunables,
                        centered_min=cfg.centered_min_images,
                    )
                )
            if arrived or faceted:
                graph_version += 1
            base = await store.base_overgrowth(conn, live, at, tunables) if k == p.last_tick else {}
            s = await asyncio.to_thread(
                tick.step,
                states,
                tick.stepping(graphs),
                {},
                live,
                base,
                tunables,
                st.seed,
                k,
                col,
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
        fresh_facets = await store.load_facet_inputs(
            conn, recipe=st.facet_recipe, model=cfg.embed_model, dim=cfg.embed_dim
        )
        rebuilt = await asyncio.to_thread(
            tick.build_graphs, fresh, fresh_facets, tunables, centered_min=cfg.centered_min_images
        )
        _same(graphs, rebuilt, tick.ACTIVE_LAYERS)

        for name in tick.ACTIVE_LAYERS:
            await store.replace_candidate_edges(conn, name, graphs[name])
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


def _arrived(
    inputs: Mapping[str, store.GraphInputs], grown: Mapping[str, Mapping[int, store.FloatArray]]
) -> dict[str, store.GraphInputs]:
    # Today's mean with the vectors that have arrived, as for the whole layer.
    return {
        name: store.GraphInputs(dict(sorted(grown[name].items())), inputs[name].mean)
        for name in inputs
    }


def _same(
    replayed: Mapping[str, Sequence[Edge]],
    rebuilt: Mapping[str, Sequence[Edge]],
    layers: Iterable[str],
) -> None:
    for name in layers:
        a, b = list(replayed.get(name, ())), list(rebuilt.get(name, ()))
        if a != b:
            raise GenesisMismatch(
                f"replayed {name} graph has {len(a)} edges, a live rebuild {len(b)}"
            )


async def run_layers(
    conn: store.Conn, layers: Sequence[str], *, settings: Settings | None = None
) -> Genesis:
    """Replay some facet layers over the ticks already stored; everything else stays.

    Allowed while stopped, before or after galleries are public, as long as no colony
    in those layers ever had a serial: nothing with an address is wiped. The rule:

    - Those layers' candidates, edge state and colonies (forming and failed only, by
      the check above) are deleted. Ticks, clicks and every other layer stay.
    - The replay runs over the stored tick rows in order, each at its recorded
      scheduled time, so a paused stretch is a gap in time as it was live. A picture
      enters at the tick whose window holds its publish time (`entries_at`).
    - Graphs are rebuilt from today's facet vectors and means whenever a picture
      with one arrives. The bonus reads all four facets, replayed or not, as a live
      rebuild does.
    - The other layers' colonies are held as they are now, so they block duplicates
      for the whole replay, and never step. The fruit cap counts only the replayed
      layers' fruitings; afterwards both lists are kept, inside the rolling window.
    - Each tick's figures for the replayed layers are merged into its stored row;
      its hashes stay those of the run that wrote it. `simulated_through`, the clock
      and the dirty mark are left alone.
    - Neighbor levels are recomputed at the last tick over every layer's state.
    """
    cfg = get_settings() if settings is None else settings
    chosen = tuple(layers)
    async with conn.transaction():
        st = await store.read_state(conn, lock=True)
        if st.running:
            raise GenesisRefused("running")
        prev = st.colonies_state()
        named = sorted(
            (c for c in prev.colonies if c.layer in chosen and c.serial is not None),
            key=lambda c: c.id,
        )
        if named:
            raise GenesisRefused(f"{named[0].layer} has galleries")
        cur = await conn.execute(
            "select layer from colonies where layer = any(%s) and serial is not null limit 1",
            (list(chosen),),
        )
        row = await cur.fetchone()
        if row is not None:
            raise GenesisRefused(f"{row['layer']} has galleries")
        times = [
            (k, at)
            for k, at in await store.tick_times(conn)
            if st.last_tick is not None and k <= st.last_tick
        ]
        if not times:
            raise GenesisRefused("nothing to replay")
        tunables, _, _ = await store.current_tunables(conn)
        facet_inputs = await store.load_facet_inputs(
            conn, recipe=st.facet_recipe, model=cfg.embed_model, dim=cfg.embed_dim
        )
        published = await store.live_published(conn)
        slugs = await store.live_slugs(conn)
        entry = entries_at(published, [at for _, at in times])
        log.info(
            "genesis of %s over %d ticks, %d pictures", ", ".join(chosen), len(times), len(entry)
        )

        await store.wipe_layers(conn, chosen)

        held_recent = list(prev.recent_fruitings)
        col = colony.ColoniesState(
            colonies=[c for c in prev.colonies if c.layer not in chosen],
            next_id=prev.next_id,
            next_serial=prev.next_serial,
            recent_fruitings=[],
            tick=None,
        )
        arrivals = sorted(entry, key=lambda pid: (entry[pid], pid))
        nxt = 0
        live: list[int] = []
        grown: dict[str, dict[int, store.FloatArray]] = {n: {} for n in facet_inputs}
        graphs: dict[str, list[Edge]] = {n: [] for n in chosen}
        graph_version = st.graph_version
        states: Mapping[str, engine.LayerState] = {}
        events = 0
        last_index = len(times) - 1

        for i, (k, at) in enumerate(times):
            arrived = False
            while nxt < len(arrivals) and entry[arrivals[nxt]] <= i:
                pid = arrivals[nxt]
                bisect.insort(live, pid)
                for name, inp in facet_inputs.items():
                    if pid in inp.vectors:
                        grown[name][pid] = inp.vectors[pid]
                        arrived = True
                nxt += 1
            if arrived:
                built = await asyncio.to_thread(
                    tick.build_facet_graphs,
                    _arrived(facet_inputs, grown),
                    tunables,
                    centered_min=cfg.centered_min_images,
                )
                graphs = {n: built.get(n, []) for n in chosen}
            s = await asyncio.to_thread(
                tick.step,
                states,
                graphs,
                {},
                live,
                {},
                tunables,
                st.seed,
                k,
                col,
                chosen,
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
                layers=chosen,
            )
            await store.merge_tick_stats(conn, k, tick.stats(s, live, {}))
            states = s.result.states
            col = s.colonies
            events += len(s.events)
            if (i + 1) % PROGRESS_EVERY == 0:
                log.info("genesis tick %d of %d", k, times[last_index][0])

        fresh = await store.load_facet_inputs(
            conn, recipe=st.facet_recipe, model=cfg.embed_model, dim=cfg.embed_dim
        )
        rebuilt = await asyncio.to_thread(
            tick.build_facet_graphs, fresh, tunables, centered_min=cfg.centered_min_images
        )
        _same(graphs, rebuilt, chosen)

        replayed = {n: states.get(n) or engine.LayerState.empty(n) for n in chosen}
        for name in chosen:
            await store.replace_candidate_edges(conn, name, graphs[name])
        await store.replace_edge_states(conn, replayed)

        # Levels read every layer, the held ones included, at the last tick's time.
        last_tick, last_at = times[last_index]
        everything = {**await store.load_edge_states(conn), **replayed}
        base = await store.base_overgrowth(conn, live, last_at, tunables)
        levels = engine.neighbor_levels(everything.values(), base, live, tunables)
        await store.clear_neighbor_levels(conn, keep=sorted(levels))
        await store.write_neighbor_levels(conn, levels)

        window = last_tick - colony.FRUIT_WINDOW
        col.recent_fruitings = sorted(x for x in held_recent + col.recent_fruitings if x > window)
        graph_version += 1
        await store.save_state(conn, colonies=col.to_dict(), graph_version=graph_version)
    out = Genesis(
        start=times[0][1],
        last_tick=last_tick,
        next_due=st.next_due,
        events=events,
        graph_version=graph_version,
        state_hash=engine.state_hash(everything),
        colonies_hash=colony.state_hash(col),
    )
    log.info("genesis of %s done through tick %d, %d events", ", ".join(chosen), last_tick, events)
    return out


def _job_layers(payload: Mapping[str, object]) -> list[str] | None:
    raw = payload.get("layers")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise GenesisRefused("layers must be a list of names")
    return [str(x) for x in raw]


async def handle(job: Job, *, pool: Pool, settings: Settings) -> None:
    """The worker job. Never retried: a refusal stays a refusal, and a run that broke
    halfway is something to look at before pressing the button again. The payload may
    carry `layers`, a list of facet layer names, for a layer-scoped run."""
    try:
        layers = _job_layers(job.payload or {})
        async with pool.connection() as conn:
            await run(conn, settings=settings, layers=layers)
    except Exception as e:
        raise HardError(str(e) or type(e).__name__) from e
