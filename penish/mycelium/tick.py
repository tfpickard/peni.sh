"""The live tick and the worker loop that drives it.

One tick is one transaction on one connection: lock the state row, rebuild the
candidate graph if it is dirty, take the waiting clicks, step the engine and the
colonies, write everything back, insert the tick row, move the clock. A crash
anywhere rolls all of it back and the next wake runs the same tick again; the
tick row's primary key makes a second commit of the same tick impossible.

The computation itself is the same pure code the harness and genesis run. No
model call ever happens in here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from penish.config import Settings, get_settings
from penish.mycelium import colonies as colony
from penish.mycelium import engine, facets, store
from penish.mycelium.graph import Edge, candidate_edges, facet_graphs
from penish.mycelium.tunables import LAYERS, Tunables

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
Latched = dict[str, list[tuple[int, int, float]]]

WHOLE = "whole"
# Every layer that has vectors today; `need` arrives with its phrases.
ACTIVE_LAYERS: tuple[str, ...] = (WHOLE, *facets.LAYERS)
# A day of ticks; more than that behind and the clock jumps instead of replaying.
CATCH_UP_MAX = 144
WAKE_SECONDS = 30.0


def build_graph(
    inputs: store.GraphInputs, tunables: Tunables, *, centered_min: int, layer: str = WHOLE
) -> list[Edge]:
    """Candidate edges for one layer, centered only once the corpus is big enough.

    The same threshold similarity.py uses for its auto mode, over the same rows, so the
    neighbors a picture page shows and the ones it grows toward agree on the mean.
    """
    mean = inputs.mean if len(inputs.vectors) >= centered_min else None
    return candidate_edges(layer, inputs.vectors, mean, tunables)


def build_facet_graphs(
    inputs: Mapping[str, store.GraphInputs], tunables: Tunables, *, centered_min: int
) -> dict[str, list[Edge]]:
    """Candidate edges for the facet layers, each centered on its own mean under the same
    size rule as the whole layer, built together because each one's bonus reads the
    others."""
    vectors = {name: inp.vectors for name, inp in inputs.items()}
    means = {
        name: inp.mean if len(inp.vectors) >= centered_min else None for name, inp in inputs.items()
    }
    return facet_graphs(vectors, means, tunables)


def build_graphs(
    whole: store.GraphInputs,
    facet_inputs: Mapping[str, store.GraphInputs],
    tunables: Tunables,
    *,
    centered_min: int,
) -> dict[str, list[Edge]]:
    """Every active layer's candidates, empty lists included, in layer order."""
    out = {WHOLE: build_graph(whole, tunables, centered_min=centered_min)}
    out.update(build_facet_graphs(facet_inputs, tunables, centered_min=centered_min))
    return {name: out.get(name, []) for name in ACTIVE_LAYERS}


def stepping(graphs: Mapping[str, Sequence[Edge]]) -> dict[str, Sequence[Edge]]:
    """The graphs a tick steps: the whole layer always, a facet layer once it has
    edges. A layer with no edges but old state still steps, since the engine runs
    every layer that has state or a graph, so its strengths decay."""
    return {n: g for n, g in graphs.items() if n == WHOLE or g}


@dataclass(frozen=True, slots=True)
class Stepped:
    result: engine.TickResult
    colonies: colony.ColoniesState
    events: list[colony.Event]
    latched: Latched


def step(
    states: Mapping[str, engine.LayerState],
    graphs: Mapping[str, Sequence[Edge]],
    traffic: Mapping[engine.Pair, int],
    live: Sequence[int],
    base: Mapping[int, float],
    tunables: Tunables,
    seed: int,
    tick: int,
    prev: colony.ColoniesState,
    layers: Collection[str] | None = None,
) -> Stepped:
    """Engine then colonies for one tick. Pure; genesis calls it too.

    `layers` limits the colonies to those layers (a layer-scoped genesis); the
    engine steps whatever it is given.
    """
    result = engine.run_tick(states, graphs, traffic, live, base, tunables, seed, tick)
    latched = {
        n: engine.latched_edges(s)
        for n, s in result.states.items()
        if n in LAYERS and (layers is None or n in layers)
    }
    col, events = colony.step(prev, tick, latched, set(live), tunables, layers)
    return Stepped(result, col, events, latched)


def stats(
    s: Stepped, live: Sequence[int], traffic: Mapping[engine.Pair, int]
) -> dict[str, dict[str, float | int]]:
    """The per-layer figures stored on each tick row and charted on the admin page."""
    live_set = set(live)
    out: dict[str, dict[str, float | int]] = {}
    for name in engine.layer_order(s.result.reports):
        rep = s.result.reports[name]
        values = [es.strength for es in s.result.states[name].edges.values()]
        held: set[int] = set()
        for c in colony.living(s.colonies, name):
            held |= c.images()
        out[name] = {
            "live": len(live_set),
            "in_gallery": len(held & live_set),
            "edges": rep.edges,
            "visible": rep.visible,
            "latched": rep.latched,
            "fused": rep.fused,
            "mean_strength": math.fsum(values) / len(values) if values else 0.0,
            "crossings": rep.crossings,
            "traffic": rep.traffic,
            "clicks": sum(traffic[p] for p in rep.traffic_flow),
        }
    return out


def _stale(graphs: Mapping[str, Sequence[Edge]], live: Mapping[int, str]) -> bool:
    # A picture that stopped being live without a dirty mark (a status changed by hand)
    # would otherwise keep its candidates until the next publish.
    return any(e.src not in live or e.dst not in live for es in graphs.values() for e in es)


@dataclass(frozen=True, slots=True)
class Ran:
    tick: int
    scheduled_at: datetime
    state_hash: str
    colonies_hash: str
    graph_version: int
    rebuilt: bool
    events: int


async def run_one(
    conn: Conn, *, now: datetime | None = None, settings: Settings | None = None
) -> Ran | None:
    """Run the next tick if the organism is running and it is due; None otherwise.

    `now` stands in for the database clock (tests); the tick's own time is always its
    scheduled time.
    """
    cfg = get_settings() if settings is None else settings
    async with conn.transaction():
        st = await store.read_state(conn, lock=True)
        clock = st.now if now is None else now
        if not st.running or st.next_due is None or st.next_due > clock:
            return None
        scheduled_at = st.next_due
        tick = st.next_tick
        tunables, tversion, _ = await store.current_tunables(conn)

        slugs = await store.live_slugs(conn)
        live = list(slugs)
        graph_version = st.graph_version
        stored = None if st.graph_dirty else await store.load_candidate_edges(conn)
        rebuilt = stored is None or _stale(stored, slugs)
        if rebuilt:
            inputs = await store.load_graph_inputs(conn, model=cfg.embed_model, dim=cfg.embed_dim)
            facet_inputs = await store.load_facet_inputs(
                conn, recipe=st.facet_recipe, model=cfg.embed_model, dim=cfg.embed_dim
            )
            built = await asyncio.to_thread(
                build_graphs,
                inputs,
                facet_inputs,
                tunables,
                centered_min=cfg.centered_min_images,
            )
            for name in ACTIVE_LAYERS:
                await store.replace_candidate_edges(conn, name, built[name])
            graph_version += 1
            graphs = stepping(built)
        else:
            assert stored is not None
            graphs = stepping({WHOLE: [], **stored})

        traffic = await store.consume_traffic(conn, scheduled_at + store.TICK)
        states = await store.load_edge_states(conn)
        prev = st.colonies_state()
        base = await store.base_overgrowth(conn, live, scheduled_at, tunables)

        s = await asyncio.to_thread(
            step, states, graphs, traffic, live, base, tunables, st.seed, tick, prev
        )

        await store.replace_edge_states(conn, s.result.states)
        await store.write_neighbor_levels(conn, s.result.neighbor_level)
        await store.apply_colonies(
            conn,
            prev,
            s.colonies,
            s.events,
            tick=tick,
            scheduled_at=scheduled_at,
            simulated=False,
            slugs=slugs,
            latched=s.latched,
        )
        row = store.TickRow(
            tick=tick,
            scheduled_at=scheduled_at,
            simulated=False,
            state_hash=engine.state_hash(s.result.states),
            colonies_hash=colony.state_hash(s.colonies),
            stats=stats(s, live, traffic),
            tunables_version=tversion,
            graph_version=graph_version,
        )
        await store.insert_tick(conn, row)
        # The site serves gallery addresses only once this is set, so the first live
        # tick that ends with a serial anywhere (its own fruiting or one genesis left)
        # is the moment they become public. Genesis runs stopped and never sets it,
        # which is what lets it run again until then.
        public: dict[str, datetime] = {}
        if st.public_since is None and any(c.serial is not None for c in s.colonies.colonies):
            public["public_since"] = clock
        await store.save_state(
            conn,
            last_tick=tick,
            next_due=scheduled_at + store.TICK,
            colonies=s.colonies.to_dict(),
            graph_version=graph_version,
            graph_dirty=False,
            tunables_version=tversion,
            **public,
        )
    return Ran(
        tick=tick,
        scheduled_at=scheduled_at,
        state_hash=row.state_hash,
        colonies_hash=row.colonies_hash,
        graph_version=graph_version,
        rebuilt=rebuilt,
        events=len(s.events),
    )


async def run_due(
    pool: Pool,
    *,
    now: datetime | None = None,
    max_ticks: int = CATCH_UP_MAX,
    stop: asyncio.Event | None = None,
    settings: Settings | None = None,
) -> int:
    """Every due tick in order, each its own transaction, at most `max_ticks`.

    Each tick re-reads `running` under the lock, so a pause lands between two ticks.
    A backlog left after the cap is dropped: the clock jumps to now, as after a pause.
    """
    ran = 0
    while ran < max_ticks:
        if stop is not None and stop.is_set():
            return ran
        async with pool.connection() as conn:
            done = await run_one(conn, now=now, settings=settings)
        if done is None:
            return ran
        ran += 1
        log.debug("mycelium tick %d (%s)", done.tick, done.scheduled_at.isoformat())
    async with pool.connection() as conn:
        skipped = await store.shift_due(conn, now)
    if skipped:
        log.warning(
            "mycelium ran %d ticks and is still %d behind; clock moved to now", ran, skipped
        )
    return ran


async def run_loop(pool: Pool, stop: asyncio.Event, *, interval: float = WAKE_SECONDS) -> None:
    """Beside the heartbeat: wake, run what is due, sleep. Never takes the worker down."""
    while not stop.is_set():
        try:
            await run_due(pool, stop=stop)
        except Exception:
            # The failed tick rolled back; the next wake runs it again.
            log.exception("mycelium tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
