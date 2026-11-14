"""The map: every live picture placed by a seeded force layout, the visible hyphae
between them and the living galleries they form.

Positions come from the whole layer's candidate graph (`layout.force_layout`), so they
change only when that graph is rebuilt, and are kept in memory per input. The rest
changes every tick, so the whole response is kept per tick and the browser may keep it
until the next one is due. With no tick coming (stopped, or behind schedule) it is kept
for `STOPPED_MAX_AGE` at most, so neglect and views still reach the map.

Reads edge state, candidate affinities and colonies only: no need or offer phrase, no
picture that is not live. Galleries follow the public gate like every gallery payload;
nodes and edges do not, as hyphae on a picture page do not.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request, Response

from penish.mycelium import layout, store
from penish.mycelium.engine import layer_order
from penish.mycelium.store import PUBLIC_STATES, gallery_title
from penish.mycelium.tunables import TICK_SECONDS

router = APIRouter()

WHOLE = "whole"
# While stopped no tick will come; a publish or unpublish still changes the map, and
# overgrowth keeps moving with time and views. Bounds the browser's copy and ours.
STOPPED_MAX_AGE = 60
# Layouts kept in memory. One is current; the others cover a request that read just
# before a rebuild committed.
LAYOUTS_KEPT = 4

_layouts: OrderedDict[str, layout.Positions] = OrderedDict()

# Monotonic, so a wall clock step cannot keep a body forever; tests replace it.
clock = time.monotonic


@dataclass(frozen=True, slots=True)
class _Cached:
    key: tuple[Any, ...]
    body: bytes
    built: float

    def fresh(self, key: tuple[Any, ...], st: store.State) -> bool:
        if self.key != key:
            return False
        # A tick on schedule renews the key itself; without one, neglect and views
        # would otherwise never reach the map.
        if _tick_coming(st):
            return True
        return clock() - self.built < STOPPED_MAX_AGE


async def _key(conn: store.Conn) -> tuple[tuple[Any, ...], store.State]:
    """What the map depends on, cheaply: the tick and graph, the gate, which pictures
    are live, the gallery titles and the tunables. A dirty mark (publish, unpublish,
    delete, reset) is part of it, so a picture that stops being live leaves the map
    before the next tick. Titles and tunables change outside the tick (the owner, the
    naming job, the admin), and while the network is stopped no tick would ever come
    to carry them.

    The state row's `updated_at` stands for every write that goes through
    `store.save_state`: a reset or wipe deletes edges and levels without moving the
    tick, the graph or the live set, and the dirty flag it sets is often set already."""
    st = await store.read_state(conn)
    cur = await conn.execute(
        """
        select (select count(*)::int from images where status = 'live') as n,
               (select coalesce(sum(id), 0)::bigint from images where status = 'live') as s,
               (select coalesce(md5(string_agg(serial || ':' || state || ':'
                                               || coalesce(title, ''), E'\n' order by serial)),
                                '')
                  from colonies
                 where serial is not null and state in ('alive', 'fading')) as titles,
               (select coalesce(max(version), 0) from mycelium_tunables) as tunables,
               (select updated_at from mycelium_state where id = 1) as changed
        """
    )
    live = await cur.fetchone()
    assert live is not None
    key = (
        st.last_tick,
        st.graph_version,
        st.graph_dirty,
        st.public,
        st.seed,
        int(live["n"]),
        int(live["s"]),
        str(live["titles"]),
        int(live["tunables"]),
        live["changed"],
    )
    return key, st


async def positions(
    ids: list[int], edges: list[tuple[int, int, float]], seed: int
) -> layout.Positions:
    """The layout for these inputs, computed once and then kept."""
    fp = layout.fingerprint(ids, edges, seed)
    found = _layouts.get(fp)
    if found is not None:
        _layouts.move_to_end(fp)
        return found
    pos = await asyncio.to_thread(layout.force_layout, ids, edges, seed)
    _layouts[fp] = pos
    while len(_layouts) > LAYOUTS_KEPT:
        _layouts.popitem(last=False)
    return pos


async def build(conn: store.Conn, st: store.State) -> dict[str, Any]:
    """The payload, from one snapshot of the database."""
    slugs = await store.live_slugs(conn)
    ids = list(slugs)
    t = await store.load_tunables(conn)

    cur = await conn.execute(
        "select src, dst, affinity from candidate_edges where layer = %s order by src, dst",
        (WHOLE,),
    )
    cand = [
        (int(r["src"]), int(r["dst"]), float(r["affinity"]))
        for r in await cur.fetchall()
        if r["src"] in slugs and r["dst"] in slugs
    ]
    pos = await positions(ids, cand, st.seed)

    cur = await conn.execute(
        """
        select e.layer, least(e.src, e.dst) as a, greatest(e.src, e.dst) as b,
               max(e.strength) as s
          from edge_state e
          join images i on i.id = e.src and i.status = 'live'
          join images j on j.id = e.dst and j.status = 'live'
         where e.strength >= %s and e.src <> e.dst
         group by 1, 2, 3
        """,
        (t.hypha_visible,),
    )
    rows = await cur.fetchall()
    rank = {name: n for n, name in enumerate(layer_order({r["layer"] for r in rows}))}
    rows.sort(key=lambda r: (rank[r["layer"]], r["a"], r["b"]))
    edges = [
        {
            "a": slugs[int(r["a"])],
            "b": slugs[int(r["b"])],
            "layer": str(r["layer"]),
            "strength": round(float(r["s"]), 3),
        }
        for r in rows
        if r["a"] in slugs and r["b"] in slugs
    ]

    member_of: dict[int, list[int]] = {}
    galleries: list[dict[str, Any]] = []
    if st.public:
        cur = await conn.execute(
            """
            select serial, title, state from colonies
             where serial is not null and state in ('alive', 'fading')
             order by serial
            """
        )
        galleries = [
            {
                "serial": int(r["serial"]),
                "title": gallery_title(int(r["serial"]), r["title"]),
                "state": PUBLIC_STATES[r["state"]],
            }
            for r in await cur.fetchall()
        ]
        cur = await conn.execute(
            """
            select distinct m.image_id, c.serial
              from colony_members m
              join colonies c on c.id = m.colony_id
              join images i on i.id = m.image_id and i.status = 'live'
             where m.left_tick is null and c.serial is not null
               and c.state in ('alive', 'fading')
             order by c.serial
            """
        )
        for r in await cur.fetchall():
            member_of.setdefault(int(r["image_id"]), []).append(int(r["serial"]))

    og = await store.overgrowth_by_id(conn, ids, t)
    nodes = [
        {
            "slug": slugs[i],
            "x": pos.get(i, (0.5, 0.5))[0],
            "y": pos.get(i, (0.5, 0.5))[1],
            "overgrowth": og.get(i, 0.0),
            "galleries": member_of.get(i, []),
        }
        for i in ids
    ]
    return {
        "graph_version": st.graph_version,
        "tick": st.last_tick,
        "nodes": nodes,
        "edges": edges,
        "galleries": galleries,
    }


def _tick_coming(st: store.State) -> bool:
    """Whether a tick is scheduled ahead of this read, to renew the key."""
    return st.running and st.next_due is not None and st.next_due > st.now


def _max_age(st: store.State) -> int:
    """Until the next tick is due, as far as this read could tell."""
    if not st.running or st.next_due is None:
        return STOPPED_MAX_AGE
    left = (st.next_due - st.now).total_seconds()
    return int(max(0.0, min(float(TICK_SECONDS), left)))


def _lock(request: Request) -> asyncio.Lock:
    lock: asyncio.Lock | None = getattr(request.app.state, "mycelium_map_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.mycelium_map_lock = lock
    return lock


@router.get("/api/mycelium")
async def mycelium_map(request: Request) -> Response:
    """`{graph_version, tick, nodes: [{slug, x, y, overgrowth, galleries}], edges: [{a, b,
    layer, strength}], galleries: [{serial, title, state}]}`, kept for one tick, or for
    `STOPPED_MAX_AGE` when none is coming."""
    pool = request.app.state.pool
    async with pool.connection() as conn:
        key, st = await _key(conn)
    cached: _Cached | None = getattr(request.app.state, "mycelium_map", None)
    if cached is None or not cached.fresh(key, st):
        # One request builds; the others wait and take what it built.
        async with _lock(request):
            cached = getattr(request.app.state, "mycelium_map", None)
            if cached is None or not cached.fresh(key, st):
                async with pool.connection() as conn, conn.transaction():
                    # One snapshot, so a tick committing midway cannot mix two ticks.
                    await conn.execute("set transaction isolation level repeatable read")
                    key, st = await _key(conn)
                    payload = await build(conn, st)
                body = json.dumps(payload, separators=(",", ":")).encode()
                cached = _Cached(key, body, clock())
                request.app.state.mycelium_map = cached
    return Response(
        cached.body,
        media_type="application/json",
        headers={"Cache-Control": f"public, max-age={_max_age(st)}"},
    )
