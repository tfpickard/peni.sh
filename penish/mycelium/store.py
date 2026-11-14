"""The only module of the organism that touches Postgres.

Everything here takes a connection and leaves transactions to the caller, except
the few admin actions (start, pause, set_tunables, reset) that must lock the state
row themselves. Lock order is always `mycelium_state` first: the tick holds it for
its whole transaction, so anything else that touches the network takes it before
any other row, or a delete and a tick could wait on each other.
"""

from __future__ import annotations

import copy
import logging
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish.mycelium import colonies as colony
from penish.mycelium import facets, overgrowth
from penish.mycelium.engine import EdgeState, LayerState, Pair, damped, layer_order
from penish.mycelium.graph import NEED, Edge, Phrase
from penish.mycelium.tunables import (
    DEFAULTS,
    LAYERS,
    SPECS,
    TICK_SECONDS,
    TunableError,
    Tunables,
    with_overrides,
)

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
FloatArray = NDArray[np.float64]

# The embedding kind the whole layer grows on.
KIND = "description"
TICK = timedelta(seconds=TICK_SECONDS)

# Not a colonies-module cause: only the admin kills a colony this way, outside a tick.
RESET = "reset"

# A change to any of these changes who may grow toward whom, so the graph is rebuilt.
GRAPH_TUNABLES = frozenset({"candidates", "affinity_floor", "min_size", "facet_bonus"})

# Network and colony tables a full wipe empties. Tunables are settings, not network.
NETWORK_TABLES = (
    "candidate_edges",
    "edge_state",
    "mycelium_traffic",
    "mycelium_ticks",
    "colony_ticks",
    "colony_events",
    "colony_members",
    "colonies",
)

_STATE_COLUMNS = frozenset(
    {
        "running",
        "seed",
        "next_due",
        "last_tick",
        "graph_version",
        "graph_dirty",
        "tunables_version",
        "colonies",
        "public_since",
        "simulated_through",
        "facet_recipe",
    }
)


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


# -- state -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class State:
    running: bool
    seed: int
    next_due: datetime | None
    last_tick: int | None
    graph_version: int
    graph_dirty: bool
    tunables_version: int | None
    colonies: dict[str, Any] | None
    public_since: datetime | None
    simulated_through: int | None
    # The database clock when this was read: the transaction's start inside one.
    now: datetime
    # Which recipe's vectors the four facet layers grow on.
    facet_recipe: str = facets.SPLIT

    @property
    def public(self) -> bool:
        return self.public_since is not None

    @property
    def next_tick(self) -> int:
        return 0 if self.last_tick is None else self.last_tick + 1

    def behind(self, now: datetime | None = None) -> int:
        """Ticks due and not yet run; 0 while stopped."""
        clock = self.now if now is None else now
        if not self.running or self.next_due is None or self.next_due > clock:
            return 0
        return int((clock - self.next_due) // TICK) + 1

    def colonies_state(self) -> colony.ColoniesState:
        if not self.colonies:
            return colony.ColoniesState()
        return colony.ColoniesState.from_dict(self.colonies)


async def read_state(conn: Conn, *, lock: bool = False) -> State:
    """The singleton row. `lock` takes it `for update`, which only means something
    inside a transaction."""
    sql = "select *, now() as db_now from mycelium_state where id = 1"
    cur = await conn.execute(sql + (" for update" if lock else ""))
    row = await cur.fetchone()
    if row is None:
        # The migration inserts it; a hand-wiped table should not take the worker down.
        await conn.execute(
            "insert into mycelium_state (id, seed)"
            " values (1, floor(random() * 4294967296)::bigint) on conflict (id) do nothing"
        )
        cur = await conn.execute(sql + (" for update" if lock else ""))
        row = await cur.fetchone()
        assert row is not None
    return State(
        running=bool(row["running"]),
        seed=int(row["seed"]),
        next_due=row["next_due"],
        last_tick=None if row["last_tick"] is None else int(row["last_tick"]),
        graph_version=int(row["graph_version"]),
        graph_dirty=bool(row["graph_dirty"]),
        tunables_version=row["tunables_version"],
        colonies=row["colonies"],
        public_since=row["public_since"],
        simulated_through=row["simulated_through"],
        now=row["db_now"],
        facet_recipe=str(row["facet_recipe"]),
    )


async def save_state(conn: Conn, **values: Any) -> None:
    """Update the named columns of the singleton row; `colonies` takes a dict."""
    unknown = set(values) - _STATE_COLUMNS
    if unknown:
        raise ValueError(f"unknown state column {sorted(unknown)[0]}")
    if not values:
        return
    params = dict(values)
    if "colonies" in params and params["colonies"] is not None:
        params["colonies"] = Jsonb(params["colonies"])
    sets = ", ".join(f"{k} = %({k})s" for k in sorted(params))
    await conn.execute(
        f"update mycelium_state set {sets}, updated_at = now() where id = 1".encode(), params
    )


async def mark_dirty(conn: Conn) -> None:
    """The candidate graph is stale; the next tick rebuilds it.

    Unconditional on purpose: `where not graph_dirty` would skip the row while a
    tick that is about to clear the flag still holds it, and the change would be
    lost. Callers run this first in their transaction (see the module docstring).
    """
    await conn.execute("update mycelium_state set graph_dirty = true where id = 1")


async def start(conn: Conn, now: datetime | None = None) -> State:
    """Running from now. The first tick is one tick length away, or sooner when a
    genesis run has already set it inside that window."""
    async with conn.transaction():
        st = await read_state(conn, lock=True)
        clock = st.now if now is None else now
        due = st.next_due
        if due is None or not clock < due <= clock + TICK:
            due = clock + TICK
        await save_state(conn, running=True, next_due=due)
    return await read_state(conn)


async def pause(conn: Conn) -> State:
    """Stopped. Waits for a tick in progress, which then commits as usual."""
    async with conn.transaction():
        await read_state(conn, lock=True)
        await save_state(conn, running=False)
    return await read_state(conn)


async def regrow(conn: Conn) -> State:
    async with conn.transaction():
        await mark_dirty(conn)
    return await read_state(conn)


async def shift_due(conn: Conn, now: datetime | None = None) -> int:
    """Drop a backlog the catch-up could not clear: the next tick is due at once.

    Returns the number of ticks skipped, 0 when nothing was behind.
    """
    async with conn.transaction():
        st = await read_state(conn, lock=True)
        clock = st.now if now is None else now
        skipped = st.behind(clock)
        if skipped:
            await save_state(conn, next_due=clock)
    return skipped


# -- tunables ----------------------------------------------------------------


def _known(values: Mapping[str, Any] | None) -> dict[str, Any]:
    # A tunable removed from code may still sit in an old version; it means nothing now.
    return {k: v for k, v in (values or {}).items() if k in SPECS}


async def current_tunables(conn: Conn) -> tuple[Tunables, int | None, dict[str, Any]]:
    """Tunables in force, the version they come from (None before any) and its overrides."""
    cur = await conn.execute(
        'select version, "values" from mycelium_tunables order by version desc limit 1'
    )
    row = await cur.fetchone()
    if row is None:
        return DEFAULTS, None, {}
    version = int(row["version"])
    overrides = _known(row["values"])
    try:
        return with_overrides(DEFAULTS, overrides), version, overrides
    except TunableError as e:
        # Bounds tightened in code after the version was saved. Defaults keep the
        # organism alive; the admin page still shows the stored version to fix.
        log.warning("tunables version %d no longer valid (%s); using defaults", version, e)
        return DEFAULTS, version, {}


async def load_tunables(conn: Conn) -> Tunables:
    """`with_overrides(DEFAULTS, latest values)`."""
    t, _, _ = await current_tunables(conn)
    return t


async def set_tunables(conn: Conn, values: Mapping[str, Any], note: str | None = None) -> int:
    """Append a version: the current overrides with `values` applied. Raises TunableError.

    Only what differs from the defaults is stored. Returns the new version.
    """
    async with conn.transaction():
        _, _, current = await current_tunables(conn)
        merged = {**current, **values}
        t = with_overrides(DEFAULTS, merged)
        defaults = DEFAULTS.as_dict()
        clean = {k: v for k, v in t.as_dict().items() if v != defaults[k]}
        before = with_overrides(DEFAULTS, current).as_dict()
        if any(before[k] != t.as_dict()[k] for k in GRAPH_TUNABLES):
            await mark_dirty(conn)
        cur = await conn.execute(
            'insert into mycelium_tunables ("values", note) values (%s, %s) returning version',
            (Jsonb(clean), note),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row["version"])


async def tunables_history(conn: Conn, limit: int = 20) -> list[dict[str, Any]]:
    """Newest first: `{version, created_at (iso), values, note}`."""
    cur = await conn.execute(
        'select version, created_at, "values", note from mycelium_tunables'
        " order by version desc limit %s",
        (limit,),
    )
    return [
        {
            "version": int(r["version"]),
            "created_at": _iso(r["created_at"]),
            "values": r["values"],
            "note": r["note"],
        }
        for r in await cur.fetchall()
    ]


# -- pictures and vectors ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphInputs:
    """Live pictures with a vector in one (kind, model, dim) space, and that space's mean."""

    vectors: dict[int, FloatArray]
    mean: FloatArray | None
    published: dict[int, datetime] = field(default_factory=dict)


async def load_graph_inputs(conn: Conn, *, model: str, dim: int, kind: str = KIND) -> GraphInputs:
    """The same corpus and mean similarity.py reads: live, filtered by kind, model and dim.

    Vectors come back as float32 widened to float64, so every reader of the same rows
    builds the same graph.
    """
    return await _load_inputs(conn, model=model, dim=dim, kind=kind)


async def _load_inputs(conn: Conn, *, model: str, dim: int, kind: str) -> GraphInputs:
    cur = await conn.execute(
        """
        select i.id, i.published_at, e.vec::real[] as vec
          from images i
          join embeddings e on e.image_id = i.id
         where i.status = 'live' and e.kind = %(kind)s and e.model = %(model)s
           and e.dim = %(dim)s
         order by i.id
        """,
        {"kind": kind, "model": model, "dim": dim},
    )
    vectors: dict[int, FloatArray] = {}
    published: dict[int, datetime] = {}
    for r in await cur.fetchall():
        pid = int(r["id"])
        vectors[pid] = np.asarray(r["vec"], dtype=np.float64)
        if r["published_at"] is not None:
            published[pid] = r["published_at"]
    cur = await conn.execute(
        "select mean::real[] as mean from corpus_stats where kind = %s and model = %s",
        (kind, model),
    )
    row = await cur.fetchone()
    mean: FloatArray | None = None
    if row is not None and row["mean"] is not None and len(row["mean"]) == dim:
        mean = np.asarray(row["mean"], dtype=np.float64)
    return GraphInputs(vectors, mean, published)


async def load_facet_inputs(
    conn: Conn, *, recipe: str, model: str, dim: int
) -> dict[str, GraphInputs]:
    """One recipe's four facet spaces, keyed by layer (`facet:mood`), each with its own
    mean. A picture without a vector of a facet is absent from that layer only."""
    return {
        facets.layer(f): await _load_inputs(conn, model=model, dim=dim, kind=facets.kind(f, recipe))
        for f in facets.FACETS
    }


async def facet_counts(conn: Conn, *, model: str, dim: int) -> dict[str, int]:
    """`{split, extract, live}`: live pictures with all four bodies and all four vectors
    of each recipe, and live pictures in all."""
    cur = await conn.execute(
        """
        with live as (select id from images where status = 'live'),
        done as (
          select f.recipe, f.image_id
            from image_facets f
            join live on live.id = f.image_id
            join embeddings e on e.image_id = f.image_id
             and e.kind = 'facet:' || f.facet || '/' || f.recipe
             and e.model = %(model)s and e.dim = %(dim)s
           group by f.recipe, f.image_id
          having count(distinct f.facet) = %(n)s
        )
        select (select count(*) from live)::int as live,
               (select count(*) from done where recipe = 'split')::int as split,
               (select count(*) from done where recipe = 'extract')::int as extract
        """,
        {"model": model, "dim": dim, "n": len(facets.FACETS)},
    )
    row = await cur.fetchone()
    assert row is not None
    return {"split": row["split"], "extract": row["extract"], "live": row["live"]}


class FacetSourceRefused(Exception):
    pass


async def set_facet_recipe(conn: Conn, recipe: str, *, model: str, dim: int) -> State:
    """Switch the facet layers to `recipe` once every live picture has it in full.

    Takes the state row first, so the count and the switch see the same live set
    and no tick runs between them. Raises FacetSourceRefused with the flat message
    the admin shows.
    """
    if recipe not in facets.RECIPES:
        raise FacetSourceRefused(f"unknown recipe {recipe}")
    async with conn.transaction():
        st = await read_state(conn, lock=True)
        if st.facet_recipe != recipe:
            counts = await facet_counts(conn, model=model, dim=dim)
            if counts[recipe] < counts["live"]:
                raise FacetSourceRefused(f"not every live picture has {recipe} facets")
            await save_state(conn, facet_recipe=recipe)
            await mark_dirty(conn)
    return await read_state(conn)


# -- offers and needs ---------------------------------------------------------

# Phrases come from the extract call only (a split fallback call is stored as an
# extract too), so this is the one recipe the need layer reads.
PHRASE_RECIPE = facets.EXTRACT
NEEDS_MAX = 200


@dataclass(frozen=True, slots=True)
class PhraseInputs:
    """Live pictures' offer and need phrases in one (model, dim) space, by id."""

    offers: list[Phrase]
    needs: list[Phrase]


async def load_phrases(conn: Conn, *, model: str, dim: int) -> PhraseInputs:
    """Every phrase of a live picture under today's embed model and dim, id order.

    float32 widened to float64, as for the other layers, so every reader of the same
    rows builds the same need graph."""
    cur = await conn.execute(
        """
        select o.id, o.image_id, o.role, o.vec::real[] as vec
          from offers_needs o
          join images i on i.id = o.image_id
         where i.status = 'live' and o.model = %(model)s and o.dim = %(dim)s
           and o.recipe = %(recipe)s
         order by o.id
        """,
        {"model": model, "dim": dim, "recipe": PHRASE_RECIPE},
    )
    offers: list[Phrase] = []
    needs: list[Phrase] = []
    for r in await cur.fetchall():
        p = Phrase(int(r["id"]), int(r["image_id"]), np.asarray(r["vec"], dtype=np.float64))
        (needs if r["role"] == "need" else offers).append(p)
    return PhraseInputs(offers, needs)


async def load_satisfaction(conn: Conn, *, model: str, dim: int) -> dict[int, float]:
    """Satisfaction of every need the need layer can hold: live picture, today's embed
    model and dim, the phrase recipe. A need outside that set keeps its value untouched
    until its picture is live again."""
    cur = await conn.execute(
        """
        select o.id, o.satisfaction
          from offers_needs o
          join images i on i.id = o.image_id
         where o.role = 'need' and i.status = 'live' and o.model = %(model)s
           and o.dim = %(dim)s and o.recipe = %(recipe)s
         order by o.id
        """,
        {"model": model, "dim": dim, "recipe": PHRASE_RECIPE},
    )
    return {int(r["id"]): float(r["satisfaction"]) for r in await cur.fetchall()}


async def write_satisfaction(
    conn: Conn, values: Mapping[int, float], before: Mapping[int, float] | None = None
) -> None:
    """Persist the tick's satisfaction. Only values that differ from `before` are sent.

    The tick holds the state row, and every other writer of offers_needs takes it
    first, so these row locks never wait on a phrase write."""
    prev = before or {}
    ids = sorted(i for i, v in values.items() if prev.get(i) != v)
    if not ids:
        return
    await conn.execute(
        """
        update offers_needs o set satisfaction = v.s
          from unnest(%(ids)s::bigint[], %(vals)s::float8[]) as v(id, s)
         where o.id = v.id and o.role = 'need'
        """,
        {"ids": ids, "vals": [float(values[i]) for i in ids]},
    )


async def clear_satisfaction(conn: Conn) -> None:
    """Every need back to unsatisfied. The caller holds the state row."""
    await conn.execute("update offers_needs set satisfaction = 0 where satisfaction <> 0")


async def needs_listing(
    conn: Conn, *, model: str, dim: int, tunables: Tunables | None = None, limit: int = NEEDS_MAX
) -> list[dict[str, Any]]:
    """Admin only. `[{slug, phrase, satisfaction, pull, edges: [{to, offer, affinity,
    strength}]}]`, by pull desc, then slug and phrase, at most `limit`.

    `affinity` is what the walkers see now: the stored affinity damped by the need's
    current satisfaction (`engine.damped`). `pull` is its sum over the need's edges, so
    a satisfied need pulls measurably less. `strength` is the edge's in the need layer,
    0 with no state. Live pictures only, at both ends.
    """
    t = await load_tunables(conn) if tunables is None else tunables
    cur = await conn.execute(
        """
        select o.id, o.phrase, o.satisfaction, i.slug, i.id as image_id
          from offers_needs o
          join images i on i.id = o.image_id and i.status = 'live'
         where o.role = 'need' and o.model = %(model)s and o.dim = %(dim)s
           and o.recipe = %(recipe)s
        """,
        {"model": model, "dim": dim, "recipe": PHRASE_RECIPE},
    )
    needs = {int(r["id"]): r for r in await cur.fetchall()}
    cur = await conn.execute(
        """
        select c.need_id, c.affinity, d.slug as to_slug, d.id as dst, f.phrase as offer,
               coalesce(s.strength, 0) as strength
          from candidate_edges c
          join images d on d.id = c.dst and d.status = 'live'
          join offers_needs f on f.id = c.offer_id
          left join edge_state s on s.layer = c.layer and s.src = c.src and s.dst = c.dst
         where c.layer = %(layer)s and c.need_id is not null
         order by c.src, c.dst
        """,
        {"layer": NEED},
    )
    edges: dict[int, list[dict[str, Any]]] = {}
    for r in await cur.fetchall():
        nid = int(r["need_id"])
        if nid not in needs:
            continue
        sat = float(needs[nid]["satisfaction"])
        edges.setdefault(nid, []).append(
            {
                "to": str(r["to_slug"] or r["dst"]),
                "offer": r["offer"],
                "affinity": damped(float(r["affinity"]), sat, t),
                "strength": float(r["strength"]),
            }
        )
    out: list[dict[str, Any]] = []
    for nid, r in needs.items():
        mine = sorted(edges.get(nid, []), key=lambda e: (-e["affinity"], e["to"]))
        out.append(
            {
                "slug": str(r["slug"] or r["image_id"]),
                "phrase": r["phrase"],
                "satisfaction": float(r["satisfaction"]),
                "pull": math.fsum(e["affinity"] for e in mine),
                "edges": mine,
            }
        )
    out.sort(key=lambda n: (-n["pull"], n["slug"], n["phrase"]))
    return out[:limit]


async def live_slugs(conn: Conn) -> dict[int, str]:
    """Every live picture, id to slug, in id order."""
    cur = await conn.execute("select id, slug from images where status = 'live' order by id")
    return {int(r["id"]): str(r["slug"] or r["id"]) for r in await cur.fetchall()}


async def live_published(conn: Conn) -> dict[int, datetime | None]:
    """Every live picture's publish time, id order; genesis replays arrivals from it."""
    cur = await conn.execute(
        "select id, published_at from images where status = 'live' order by id"
    )
    return {int(r["id"]): r["published_at"] for r in await cur.fetchall()}


# -- candidate edges and edge state ------------------------------------------


async def replace_candidate_edges(conn: Conn, layer: str, edges: Sequence[Edge]) -> None:
    await conn.execute("delete from candidate_edges where layer = %s", (layer,))
    if not edges:
        return
    async with (
        conn.cursor() as cur,
        cur.copy(
            "copy candidate_edges (layer, src, dst, raw, affinity, need_id, offer_id) from stdin"
        ) as cp,
    ):
        for e in edges:
            if e.layer != layer:
                raise ValueError(f"edge for {e.layer} in {layer}")
            await cp.write_row(
                (layer, e.src, e.dst, float(e.raw), float(e.affinity), e.need, e.offer)
            )


async def load_candidate_edges(conn: Conn) -> dict[str, list[Edge]]:
    """Every layer's candidates, sorted by (src, dst) as graph.candidate_edges returns them."""
    cur = await conn.execute(
        "select layer, src, dst, raw, affinity, need_id, offer_id"
        " from candidate_edges order by layer, src, dst"
    )
    out: dict[str, list[Edge]] = {}
    for r in await cur.fetchall():
        out.setdefault(r["layer"], []).append(
            Edge(
                r["layer"],
                int(r["src"]),
                int(r["dst"]),
                float(r["raw"]),
                float(r["affinity"]),
                None if r["need_id"] is None else int(r["need_id"]),
                None if r["offer_id"] is None else int(r["offer_id"]),
            )
        )
    return out


async def load_edge_states(conn: Conn) -> dict[str, LayerState]:
    cur = await conn.execute(
        "select layer, src, dst, strength, latched, fatigue, fused, idle"
        " from edge_state order by layer, src, dst"
    )
    edges: dict[str, dict[Pair, EdgeState]] = {}
    for r in await cur.fetchall():
        edges.setdefault(r["layer"], {})[(int(r["src"]), int(r["dst"]))] = EdgeState(
            float(r["strength"]),
            bool(r["latched"]),
            float(r["fatigue"]),
            bool(r["fused"]),
            int(r["idle"]),
        )
    return {name: LayerState(name, edges[name]) for name in layer_order(edges)}


async def replace_edge_states(conn: Conn, states: Mapping[str, LayerState]) -> None:
    """A rewrite of the given layers. Nearly every row changes every tick (decay), so a
    diff would write almost as much."""
    names = list(layer_order(states))
    if not names:
        return
    await conn.execute("delete from edge_state where layer = any(%s)", (names,))
    async with (
        conn.cursor() as cur,
        cur.copy(
            "copy edge_state (layer, src, dst, strength, latched, fatigue, fused, idle) from stdin"
        ) as cp,
    ):
        for name in names:
            edges = states[name].edges
            for src, dst in sorted(edges):
                es = edges[(src, dst)]
                await cp.write_row(
                    (
                        name,
                        src,
                        dst,
                        float(es.strength),
                        es.latched,
                        float(es.fatigue),
                        es.fused,
                        es.idle,
                    )
                )


# -- traffic -----------------------------------------------------------------


async def click_feeds(conn: Conn, src: int, dst: int) -> bool:
    """Whether a click from `src` to `dst` would feed the organism right now."""
    cur = await conn.execute(
        """
        select (select running from mycelium_state where id = 1)
           and exists (select 1 from candidate_edges where src = %(s)s and dst = %(d)s) as ok
        """,
        {"s": src, "d": dst},
    )
    row = await cur.fetchone()
    return bool(row and row["ok"])


async def record_click(conn: Conn, src: int, dst: int) -> bool:
    """Queue one click for the next tick; True if queued.

    The checks repeat inside the insert, so a pause or a rebuild between the caller's
    check and this write still drops the click.
    """
    cur = await conn.execute(
        """
        insert into mycelium_traffic (src, dst)
        select %(s)s, %(d)s
         where (select running from mycelium_state where id = 1)
           and exists (select 1 from candidate_edges where src = %(s)s and dst = %(d)s)
        returning id
        """,
        {"s": src, "d": dst},
    )
    return await cur.fetchone() is not None


async def consume_traffic(conn: Conn, before: datetime) -> dict[Pair, int]:
    """Take every committed click older than `before`; counts per (src, dst).

    A click committed while the tick runs is not visible to this delete, and one
    stamped after `before` is left alone; either way the next tick takes it.
    """
    cur = await conn.execute(
        "delete from mycelium_traffic where created_at < %s returning src, dst", (before,)
    )
    out: dict[Pair, int] = {}
    for r in await cur.fetchall():
        pair = (int(r["src"]), int(r["dst"]))
        out[pair] = out.get(pair, 0) + 1
    return {p: out[p] for p in sorted(out)}


# -- overgrowth --------------------------------------------------------------


async def _attention(conn: Conn, ids: Sequence[int], at: datetime | None) -> list[DictRow]:
    # The pace is one row computed once, not per picture. `at` None is the database
    # clock, the same one a view writes with, so a picture viewed in this instant reads 0.
    cur = await conn.execute(
        """
        with clock as (select coalesce(%(at)s::timestamptz, now()) as t),
        pace as (
          select (select count(*) from images
                   where status = 'live' and published_at is not null)::int as live,
                 (select coalesce(sum(n), 0) from view_days, clock
                   where day > clock.t::date - 7 and day <= clock.t::date)::float8 as views
        )
        select i.id,
               extract(epoch from clock.t - coalesce(a.last_viewed_at, i.published_at))::float8
                 / 3600.0 as hours,
               coalesce(a.neighbor_level, 0)::float8 as neighbor_level,
               pace.live, pace.views
          from images i
          left join picture_attention a on a.image_id = i.id
          cross join pace
          cross join clock
         where i.id = any(%(ids)s)
        """,
        {"ids": list(ids), "at": at},
    )
    return await cur.fetchall()


async def overgrowth_by_id(
    conn: Conn, ids: Sequence[int], tunables: Tunables | None = None
) -> dict[int, float]:
    """Overgrowth in 0..1 per picture, rounded to 3 places; ids not found are absent.

    One query for the whole batch, plus one for the tunables when none are given.
    """
    if not ids:
        return {}
    t = await load_tunables(conn) if tunables is None else tunables
    rows = await _attention(conn, ids, None)
    if not rows:
        return {}
    # The last seven days, today included.
    vpd = overgrowth.views_per_day(rows[0]["views"])
    tau = overgrowth.tau_hours(rows[0]["live"], vpd, t)
    out: dict[int, float] = {}
    for r in rows:
        # Never seen and never published: nothing to have neglected yet.
        if r["hours"] is None:
            out[r["id"]] = 0.0
            continue
        base = overgrowth.base_from_hours(r["hours"], tau)
        lvl = overgrowth.level(base, r["neighbor_level"], t)
        out[r["id"]] = round(min(1.0, max(0.0, lvl)), 3)
    return out


async def record_view(conn: AsyncConnection[DictRow], image_id: int) -> bool:
    """Count a view unless the picture was counted within the last minute; True if counted.

    The minute rule lives in the row itself so it holds across restarts and workers.
    An upsert rather than a bare update: a picture published before its attention row
    existed still gets one on its first view. Never touches images.updated_at, which
    versions the sort snapshots.
    """
    async with conn.transaction():
        cur = await conn.execute(
            """
            insert into picture_attention (image_id, views, last_viewed_at)
            values (%(id)s, 1, now())
            on conflict (image_id) do update
               set views = picture_attention.views + 1, last_viewed_at = now()
             where picture_attention.last_viewed_at < now() - interval '1 minute'
            returning image_id
            """,
            {"id": image_id},
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            """
            insert into view_days (day, n) values (current_date, 1)
            on conflict (day) do update set n = view_days.n + 1
            """
        )
    return True


async def base_overgrowth(
    conn: Conn, ids: Sequence[int], at: datetime, tunables: Tunables
) -> dict[int, float]:
    """Base overgrowth (no neighbor spread) at a tick's scheduled time, unrounded."""
    if not ids:
        return {}
    rows = await _attention(conn, ids, at)
    if not rows:
        return {}
    tau = overgrowth.tau_hours(
        rows[0]["live"], overgrowth.views_per_day(rows[0]["views"]), tunables
    )
    return {
        int(r["id"]): 0.0 if r["hours"] is None else overgrowth.base_from_hours(r["hours"], tau)
        for r in rows
    }


async def write_neighbor_levels(conn: Conn, levels: Mapping[int, float]) -> None:
    if not levels:
        return
    ids = sorted(levels)
    # Only rows that change, so a quiet network does not rewrite every attention row.
    await conn.execute(
        """
        update picture_attention a set neighbor_level = v.lvl
          from unnest(%(ids)s::bigint[], %(lvls)s::float8[]) as v(id, lvl)
         where a.image_id = v.id and a.neighbor_level is distinct from v.lvl
        """,
        {"ids": ids, "lvls": [float(levels[i]) for i in ids]},
    )


# -- colonies projection -----------------------------------------------------


def _colony_row(c: colony.Colony) -> tuple[Any, ...]:
    return (
        c.state,
        c.serial,
        c.fruited,
        c.faded,
        c.died,
        c.cause,
        c.parent,
        c.absorbed_by,
        c.peak,
    )


def colony_strength_sums(
    state: colony.ColoniesState, latched: Mapping[str, Sequence[tuple[int, int, float]]]
) -> dict[int, tuple[float, float]]:
    """(total, mean) strength of the latched edges inside each forming or living colony;
    (0, 0) with none."""
    out: dict[int, tuple[float, float]] = {}
    for c in state.colonies:
        if c.state not in (colony.FORMING, colony.ALIVE, colony.FADING):
            continue
        mine = c.images()
        values = [s for a, b, s in latched.get(c.layer, ()) if a in mine and b in mine]
        total = math.fsum(values)
        out[c.id] = (total, total / len(values) if values else 0.0)
    return out


def colony_strengths(
    state: colony.ColoniesState, latched: Mapping[str, Sequence[tuple[int, int, float]]]
) -> dict[int, float]:
    """Mean strength of the latched edges inside each forming or living colony; 0 with none."""
    return {k: mean for k, (_, mean) in colony_strength_sums(state, latched).items()}


def cover_of(
    c: colony.Colony, latched: Mapping[str, Sequence[tuple[int, int, float]]]
) -> int | None:
    """The member with the highest summed strength to other members over the latched
    edges of its layer, ties to the lowest id; None for a colony with no members."""
    mine = c.images()
    if not mine:
        return None
    sums = dict.fromkeys(mine, 0.0)
    for a, b, s in latched.get(c.layer, ()):
        if a in mine and b in mine:
            sums[a] += s
            sums[b] += s
    return min(sums, key=lambda i: (-sums[i], i))


async def apply_colonies(
    conn: Conn,
    prev: colony.ColoniesState,
    new: colony.ColoniesState,
    events: Sequence[colony.Event],
    *,
    tick: int,
    scheduled_at: datetime,
    simulated: bool,
    slugs: Mapping[int, str],
    latched: Mapping[str, Sequence[tuple[int, int, float]]],
    layers: Collection[str] | None = None,
) -> None:
    """Write the difference between two colony states into the colony tables.

    `slugs` names pictures joining a colony; `latched` gives the per-tick strength.
    Colony rows are upserted when they change and never deleted. `layers` limits the
    per-tick size and strength rows to the layers that stepped, so a replay of some
    layers does not overwrite the others' rows for the same tick with zeros.
    """
    prev_by = {c.id: c for c in prev.colonies}

    rows = []
    for c in new.colonies:
        p = prev_by.get(c.id)
        if p is not None and _colony_row(p) == _colony_row(c):
            continue
        rows.append(
            {
                "id": c.id,
                "layer": c.layer,
                "state": c.state,
                "serial": c.serial,
                "born": c.born,
                "fruited": c.fruited,
                "faded": c.faded,
                "died": c.died,
                "cause": c.cause,
                "parent": c.parent,
                "absorbed_by": c.absorbed_by,
                "peak": c.peak,
                "simulated": simulated,
            }
        )
    if rows:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                insert into colonies (id, layer, state, serial, born_tick, fruited_tick,
                                      faded_tick, died_tick, cause, parent_id, absorbed_by,
                                      peak, simulated)
                values (%(id)s, %(layer)s, %(state)s, %(serial)s, %(born)s, %(fruited)s,
                        %(faded)s, %(died)s, %(cause)s, %(parent)s, %(absorbed_by)s,
                        %(peak)s, %(simulated)s)
                on conflict (id) do update set
                  state = excluded.state, serial = excluded.serial,
                  fruited_tick = excluded.fruited_tick, faded_tick = excluded.faded_tick,
                  died_tick = excluded.died_tick, cause = excluded.cause,
                  parent_id = excluded.parent_id, absorbed_by = excluded.absorbed_by,
                  peak = excluded.peak, updated_at = now()
                """,
                rows,
            )

    closes: list[dict[str, Any]] = []
    opens: list[dict[str, Any]] = []
    founders: list[dict[str, Any]] = []
    for c in new.colonies:
        p = prev_by.get(c.id)
        before = {m.image: m for m in p.members} if p is not None else {}
        after = {m.image: m for m in c.members}
        gone = sorted(set(before) - set(after))
        if gone:
            closes.append({"c": c.id, "ids": gone, "tick": tick})
        for image in sorted(set(after) - set(before)):
            m = after[image]
            opens.append(
                {
                    "c": c.id,
                    "image": image,
                    "slug": slugs.get(image, str(image)),
                    "founder": m.founder,
                    "joined": m.joined,
                }
            )
        for image in sorted(set(after) & set(before)):
            if after[image].founder != before[image].founder:
                founders.append({"c": c.id, "image": image, "founder": after[image].founder})
    async with conn.cursor() as cur:
        if closes:
            # A deleted picture's rows lost their image_id to the foreign key; it is
            # certainly not a member any more, so its open rows close with the rest.
            await cur.executemany(
                """
                update colony_members set left_tick = %(tick)s
                 where colony_id = %(c)s and left_tick is null
                   and (image_id = any(%(ids)s) or image_id is null)
                """,
                closes,
            )
        if opens:
            await cur.executemany(
                """
                insert into colony_members (colony_id, image_id, slug, founder, joined_tick)
                select %(c)s, (select id from images where id = %(image)s), %(slug)s,
                       %(founder)s, %(joined)s
                """,
                opens,
            )
        if founders:
            await cur.executemany(
                """
                update colony_members set founder = %(founder)s
                 where colony_id = %(c)s and image_id = %(image)s and left_tick is null
                """,
                founders,
            )
        if events:
            await cur.executemany(
                """
                insert into colony_events (tick, scheduled_at, kind, colony_id, layer, image_id,
                                           other_colony_id, simulated)
                select %(tick)s, %(at)s, %(kind)s, %(c)s, %(layer)s,
                       (select id from images where id = %(image)s), %(other)s, %(sim)s
                """,
                [
                    {
                        "tick": ev.tick,
                        "at": scheduled_at,
                        "kind": ev.kind,
                        "c": ev.colony,
                        "layer": ev.layer,
                        "image": ev.image,
                        "other": ev.other,
                        "sim": simulated,
                    }
                    for ev in events
                ],
            )
        strengths = colony_strength_sums(new, latched)
        if layers is not None:
            stepped = {c.id for c in new.colonies if c.layer in layers}
            strengths = {k: v for k, v in strengths.items() if k in stepped}
        if strengths:
            sizes = {c.id: len(c.members) for c in new.colonies}
            await cur.executemany(
                "insert into colony_ticks (colony_id, tick, size, mean_strength, total_strength)"
                " values (%(c)s, %(tick)s, %(size)s, %(s)s, %(total)s)"
                " on conflict (colony_id, tick) do update"
                " set size = excluded.size, mean_strength = excluded.mean_strength,"
                " total_strength = excluded.total_strength",
                [
                    {
                        "c": cid,
                        "tick": tick,
                        "size": sizes[cid],
                        "s": strengths[cid][1],
                        "total": strengths[cid][0],
                    }
                    for cid in sorted(strengths)
                ],
            )
        await _galleries(cur, prev_by, new, tick=tick, slugs=slugs, latched=latched, layers=layers)


async def _galleries(
    cur: Any,
    prev_by: Mapping[int, colony.Colony],
    new: colony.ColoniesState,
    *,
    tick: int,
    slugs: Mapping[int, str],
    latched: Mapping[str, Sequence[tuple[int, int, float]]],
    layers: Collection[str] | None,
) -> None:
    """Cover and peak membership of every living colony in a stepped layer.

    The cover is recomputed every tick while the gallery lives and left alone once it
    dies, so a tombstone keeps the face it had. A dead or failed colony is skipped, and
    so is a layer that did not step, whose latched edges this call was not given.
    """
    covers: list[dict[str, Any]] = []
    peaks: list[dict[str, Any]] = []
    for c in new.colonies:
        if c.state not in colony.LIVING or (layers is not None and c.layer not in layers):
            continue
        covers.append({"c": c.id, "img": cover_of(c, latched)})
        p = prev_by.get(c.id)
        # Only a tick that could have set a new high: the first living tick, or more
        # members than the tick before. The SQL guard settles it against the snapshot.
        if p is None or p.state not in colony.LIVING or len(c.members) > len(p.members):
            peaks.append(
                {
                    "c": c.id,
                    "n": len(c.members),
                    "tick": tick,
                    "members": Jsonb(
                        [
                            {"id": m.image, "slug": slugs.get(m.image, str(m.image))}
                            for m in c.members
                        ]
                    ),
                }
            )
    if covers:
        await cur.executemany(
            """
            update colonies set cover_image_id = (select id from images where id = %(img)s)
             where id = %(c)s
               and cover_image_id is distinct from (select id from images where id = %(img)s)
            """,
            covers,
        )
    if peaks:
        await cur.executemany(
            """
            update colonies set peak_members = %(members)s, peak_tick = %(tick)s
             where id = %(c)s and coalesce(jsonb_array_length(peak_members), 0) < %(n)s
            """,
            peaks,
        )


# -- ticks -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickRow:
    tick: int
    scheduled_at: datetime
    simulated: bool
    state_hash: str
    colonies_hash: str
    # Per layer: live, in_gallery, edges, visible, latched, fused, mean_strength,
    # crossings, traffic, clicks.
    stats: Mapping[str, Mapping[str, float | int]]
    tunables_version: int | None
    graph_version: int | None


async def insert_tick(conn: Conn, row: TickRow) -> None:
    """The primary key on tick is what turns a second run of the same tick into an error."""
    await conn.execute(
        """
        insert into mycelium_ticks (tick, scheduled_at, ran_at, simulated, state_hash,
                                    colonies_hash, stats, tunables_version, graph_version)
        values (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s, %s)
        """,
        (
            row.tick,
            row.scheduled_at,
            row.simulated,
            row.state_hash,
            row.colonies_hash,
            Jsonb({k: dict(v) for k, v in row.stats.items()}),
            row.tunables_version,
            row.graph_version,
        ),
    )


# -- wipe and reset ----------------------------------------------------------


async def wipe_network(conn: Conn, *, levels: bool = True) -> None:
    """Empty every network and colony table and forget the run; keeps running, seed,
    next_due and tunables. The caller holds the state lock.

    Delete, not truncate: truncate's access exclusive lock would stop every reader of
    these tables (each image page reads edge_state, each click candidate_edges) until
    the caller commits, which for genesis is minutes. Deleted rows stay visible to
    other transactions until then. Children before `colonies`, for the foreign keys.

    `levels=False` leaves neighbor levels to the caller, so a long transaction does
    not hold attention rows that every view of those pictures writes.
    """
    for table in NETWORK_TABLES:
        await conn.execute(f"delete from {table}".encode())
    # Satisfaction is the need layer's state, so it goes with the edges.
    await clear_satisfaction(conn)
    if levels:
        await clear_neighbor_levels(conn)
    await save_state(conn, last_tick=None, colonies=None, simulated_through=None, graph_dirty=True)


async def wipe_layers(conn: Conn, layers: Collection[str]) -> None:
    """Empty some layers' candidates, edge state and colonies (with their members,
    events and per-tick rows, by cascade). The caller holds the state lock and has
    checked that none of those colonies ever had a serial. Ticks, clicks and the
    other layers stay."""
    names = sorted(layers)
    await conn.execute("delete from candidate_edges where layer = any(%s)", (names,))
    await conn.execute("delete from edge_state where layer = any(%s)", (names,))
    await conn.execute("delete from colonies where layer = any(%s)", (names,))


async def tick_times(conn: Conn) -> list[tuple[int, datetime]]:
    """Every stored tick and its scheduled time, in order."""
    cur = await conn.execute("select tick, scheduled_at from mycelium_ticks order by tick")
    return [(int(r["tick"]), r["scheduled_at"]) for r in await cur.fetchall()]


async def merge_tick_stats(
    conn: Conn, tick: int, stats: Mapping[str, Mapping[str, float | int]]
) -> None:
    """Put some layers' figures on an existing tick row, leaving the others and the
    hashes as the run that wrote the row left them."""
    await conn.execute(
        "update mycelium_ticks set stats = stats || %s where tick = %s",
        (Jsonb({k: dict(v) for k, v in stats.items()}), tick),
    )


async def clear_neighbor_levels(conn: Conn, keep: Sequence[int] = ()) -> None:
    """Every neighbor level outside `keep` back to 0."""
    await conn.execute(
        """
        update picture_attention set neighbor_level = 0
         where neighbor_level <> 0 and image_id <> all(%s::bigint[])
        """,
        (list(keep),),
    )


async def reset(conn: Conn) -> str:
    """Start the network over. Returns "full" or "public".

    Before any gallery address is public it is a full wipe. After, galleries have
    addresses people may hold, so tombstones and serials stay: edges and waiting
    clicks go, and every forming or living colony dies with cause `reset`.
    """
    async with conn.transaction():
        st = await read_state(conn, lock=True)
        if not st.public:
            await wipe_network(conn)
            return "full"
        await conn.execute("delete from edge_state")
        await conn.execute("delete from mycelium_traffic")
        await clear_satisfaction(conn)
        await clear_neighbor_levels(conn)
        prev = st.colonies_state()
        new = copy.deepcopy(prev)
        tick = st.last_tick if st.last_tick is not None else 0
        events: list[colony.Event] = []
        for c in new.colonies:
            if c.state == colony.FORMING:
                c.state = colony.FAILED
            elif c.state in colony.LIVING:
                c.state = colony.DEAD
            else:
                continue
            c.cause = RESET
            c.died = tick
            c.pending = []
            events.append(colony.Event(tick, RESET, c.layer, c.id))
        await apply_colonies(
            conn,
            prev,
            new,
            events,
            tick=tick,
            scheduled_at=st.now,
            simulated=False,
            slugs={},
            latched={},
        )
        await save_state(conn, colonies=new.to_dict(), graph_dirty=True)
    return "public"


# -- reads for the admin page ------------------------------------------------


async def series(conn: Conn, layer: str = "whole", limit: int = 1008) -> list[dict[str, Any]]:
    """The last `limit` ticks of one layer, oldest first, JSON-ready."""
    cur = await conn.execute(
        """
        select * from (
          select tick, scheduled_at, simulated, stats -> %(layer)s as s
            from mycelium_ticks order by tick desc limit %(limit)s
        ) t order by tick
        """,
        {"layer": layer, "limit": limit},
    )
    keys = ("live", "in_gallery", "visible", "latched", "mean_strength", "crossings", "traffic")
    out = []
    for r in await cur.fetchall():
        s = r["s"] or {}
        item: dict[str, Any] = {
            "tick": int(r["tick"]),
            "scheduled_at": _iso(r["scheduled_at"]),
            "simulated": bool(r["simulated"]),
        }
        for k in keys:
            item[k] = s.get(k, 0)
        out.append(item)
    return out


async def colonies_listing(conn: Conn) -> dict[str, list[dict[str, Any]]]:
    """Forming and living colonies with their current live members, JSON-ready.

    `{forming: [colony], living: [colony]}` where colony is `{id, layer, state,
    serial, title, size, born_tick, fruited_tick, members: [{slug, founder}]}`.
    """
    cur = await conn.execute(
        """
        select id, layer, state, serial, title, born_tick, fruited_tick
          from colonies where state in ('forming', 'alive', 'fading')
         order by serial nulls last, born_tick, id
        """
    )
    cols = await cur.fetchall()
    ids = [int(c["id"]) for c in cols]
    members: dict[int, list[dict[str, Any]]] = {}
    if ids:
        cur = await conn.execute(
            """
            select m.colony_id, i.slug, m.founder
              from colony_members m
              join images i on i.id = m.image_id and i.status = 'live'
             where m.colony_id = any(%s) and m.left_tick is null
             order by m.colony_id, i.slug
            """,
            (ids,),
        )
        for r in await cur.fetchall():
            members.setdefault(int(r["colony_id"]), []).append(
                {"slug": r["slug"], "founder": bool(r["founder"])}
            )
    out: dict[str, list[dict[str, Any]]] = {"forming": [], "living": []}
    for c in cols:
        ms = members.get(int(c["id"]), [])
        item = {
            "id": int(c["id"]),
            "layer": c["layer"],
            "state": c["state"],
            "serial": c["serial"],
            "title": c["title"],
            "size": len(ms),
            "born_tick": int(c["born_tick"]),
            "fruited_tick": c["fruited_tick"],
            "members": ms,
        }
        out["forming" if c["state"] == colony.FORMING else "living"].append(item)
    return out


async def recent_events(conn: Conn, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first: `{tick, scheduled_at, kind, colony_id, layer, slug, other_colony_id,
    simulated}`, JSON-ready."""
    cur = await conn.execute(
        """
        select e.tick, e.scheduled_at, e.kind, e.colony_id, e.layer, i.slug,
               e.other_colony_id, e.simulated
          from colony_events e
          left join images i on i.id = e.image_id
         order by e.tick desc, e.id desc
         limit %s
        """,
        (limit,),
    )
    return [
        {
            "tick": int(r["tick"]),
            "scheduled_at": _iso(r["scheduled_at"]),
            "kind": r["kind"],
            "colony_id": int(r["colony_id"]),
            "layer": r["layer"],
            "slug": r["slug"],
            "other_colony_id": r["other_colony_id"],
            "simulated": bool(r["simulated"]),
        }
        for r in await cur.fetchall()
    ]


# -- public reads ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hypha:
    image_id: int
    slug: str
    strength: float
    layer: str


HYPHAE_MAX = 8


async def hyphae_for(
    conn: Conn, image_id: int, tunables: Tunables | None = None, limit: int = HYPHAE_MAX
) -> list[Hypha]:
    """Visible hyphae of one picture: the strongest edge per neighbor over both
    directions and every layer, to live pictures only, strongest first then by slug.

    Reads only edge state, so need phrases (Increment 5) can never leak through here.
    """
    t = await load_tunables(conn) if tunables is None else tunables
    cur = await conn.execute(
        """
        select distinct on (o.other) o.other, i.slug, o.strength, o.layer
          from (select dst as other, strength, layer from edge_state where src = %(id)s
                union all
                select src as other, strength, layer from edge_state where dst = %(id)s) o
          join images i on i.id = o.other and i.status = 'live'
         where o.strength >= %(vis)s and o.other <> %(id)s
         order by o.other, o.strength desc, array_position(%(layers)s::text[], o.layer), o.layer
        """,
        {"id": image_id, "vis": t.hypha_visible, "layers": list(LAYERS)},
    )
    found = [
        Hypha(int(r["other"]), str(r["slug"]), float(r["strength"]), str(r["layer"]))
        for r in await cur.fetchall()
    ]
    found.sort(key=lambda h: (-h.strength, h.slug))
    return found[:limit]


# -- galleries ---------------------------------------------------------------

# The colonies module's states as the public names them. Forming and failed colonies
# have no address and never appear.
PUBLIC_STATES: dict[str, str] = {
    colony.ALIVE: "living",
    colony.FADING: "fading",
    colony.DEAD: "dead",
}
GALLERY_STATES: tuple[str, ...] = tuple(PUBLIC_STATES)


def public_kind(layer: str) -> str:
    """The plain word a layer goes by in public: `facet:mood` is `mood`."""
    return layer.removeprefix("facet:")


def gallery_title(serial: int, title: str | None) -> str:
    """Until a gallery is named it is its address."""
    return title or f"gallery {serial}"


async def galleries_public(conn: Conn) -> bool:
    """Whether the site serves gallery addresses yet: only once a live tick has run
    with a serial in existence (`tick.run_one`). Before that, genesis may still
    renumber them, so nothing about them is public."""
    cur = await conn.execute(
        "select public_since is not null as p from mycelium_state where id = 1"
    )
    row = await cur.fetchone()
    return bool(row and row["p"])


async def galleries_for_image(conn: Conn, image_id: int) -> list[dict[str, Any]]:
    """`[{serial, title, state, current}]` for one picture, JSON-ready: galleries it is
    in now first (living, then fading), then galleries it was in, newest serial first.

    A stint counts only if it reached past fruiting, so a picture that left a colony
    while it was still forming was never in the gallery. Empty before galleries are
    public.
    """
    if not await galleries_public(conn):
        return []
    cur = await conn.execute(
        """
        select c.serial, c.title, c.state,
               bool_or(m.left_tick is null and c.state in ('alive', 'fading')) as current
          from colony_members m
          join colonies c on c.id = m.colony_id
         where m.image_id = %(id)s and c.serial is not null
           and c.state = any(%(states)s)
           and (m.left_tick is null or m.left_tick > c.fruited_tick)
         group by c.id, c.serial, c.title, c.state
        """,
        {"id": image_id, "states": list(GALLERY_STATES)},
    )
    rows = await cur.fetchall()
    order = {s: i for i, s in enumerate(GALLERY_STATES)}
    rows.sort(
        key=lambda r: (not r["current"], order[r["state"]] if r["current"] else 0, -r["serial"])
    )
    return [
        {
            "serial": int(r["serial"]),
            "title": gallery_title(int(r["serial"]), r["title"]),
            "state": PUBLIC_STATES[r["state"]],
            "current": bool(r["current"]),
        }
        for r in rows
    ]
