"""Galleries in public: the list, one gallery (or its tombstone), and the event log.

Nothing here is served before `mycelium_state.public_since` is set (see
`store.galleries_public`): until then genesis may renumber every serial. Only
colonies with a serial appear, and a picture that is not live (unpublished,
hidden, deleted) is treated as if it had never been a member.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from penish.api.routes_images import grid_items
from penish.mycelium import store
from penish.mycelium.store import (
    GALLERY_STATES,
    PUBLIC_STATES,
    galleries_public,
    gallery_title,
    public_kind,
)

router = APIRouter()

# The longest sparkline a gallery page draws; a longer life is thinned evenly.
SERIES_POINTS = 288
LOG_PAGE = 50
# Nine digits: the bound /g/<n> accepts, and safely inside a Postgres integer.
SERIAL_MAX = 999_999_999

DEATHS: tuple[str, ...] = ("depopulated", "absorbed", "schism", "drifted", "starved", "reset")
# What a gallery page shows of its own history. `failed` only ever happens to a
# colony that never fruited, so it has no page to show it on.
GALLERY_EVENTS: tuple[str, ...] = (
    "formed",
    "fruited",
    "joined",
    "left",
    "faded",
    "revived",
    *DEATHS,
)
LOG_EVENTS: tuple[str, ...] = ("fruited", "joined", "left", *DEATHS)
_PICTURE_EVENTS = ("joined", "left")

FILTERS: dict[str, tuple[str, ...]] = {
    "all": GALLERY_STATES,
    **{public: (state,) for state, public in PUBLIC_STATES.items()},
}


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


def _days(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, math.floor((end - start).total_seconds() / 86_400))


def parse_serial(raw: str) -> int | None:
    """A canonical serial: digits only, no leading zero, at most nine digits."""
    if not raw.isascii() or not raw.isdigit() or raw.startswith("0") or len(raw) > 9:
        return None
    return int(raw)


_IMAGE_COLS = "i.id, i.pub_sha, i.slug, i.width, i.height, i.blurhash, i.palette"


async def _items(conn: store.Conn, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Grid items with captions for the live pictures among `ids`, keyed by id."""
    if not ids:
        return {}
    cur = await conn.execute(
        f"""
        select {_IMAGE_COLS}, t.body as caption
          from images i
          left join image_texts t on t.image_id = i.id and t.field = 'caption'
         where i.id = any(%s) and i.status = 'live'
        """.encode(),
        (list(ids),),
    )
    rows = await cur.fetchall()
    og = await store.overgrowth_by_id(conn, [r["id"] for r in rows])
    items = grid_items(rows, og)
    return {r["id"]: {**item, "caption": r["caption"]} for r, item in zip(rows, items, strict=True)}


def _thumb(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {k: v for k, v in item.items() if k != "caption"}


_COLONY_SQL = """
    select c.id, c.serial, c.title, c.state, c.layer, c.cause, c.peak, c.peak_members,
           c.cover_image_id, c.parent_id, c.absorbed_by, c.born_tick, c.fruited_tick,
           c.faded_tick, c.died_tick,
           bt.scheduled_at as formed_at, ft.scheduled_at as fruited_at,
           vt.scheduled_at as faded_at, dt.scheduled_at as died_at,
           (select count(*) from colony_members m
              join images i on i.id = m.image_id and i.status = 'live'
             where m.colony_id = c.id and m.left_tick is null)::int as live_members,
           now() as db_now
      from colonies c
      left join mycelium_ticks bt on bt.tick = c.born_tick
      left join mycelium_ticks ft on ft.tick = c.fruited_tick
      left join mycelium_ticks vt on vt.tick = c.faded_tick
      left join mycelium_ticks dt on dt.tick = c.died_tick
     where c.serial is not null and c.state = any(%(states)s)
"""


def _size(r: Mapping[str, Any]) -> int:
    """Members now for a living gallery; for a dead one, how many it held at its peak."""
    if r["state"] != "dead":
        return int(r["live_members"])
    snap = r["peak_members"]
    return len(snap) if snap else int(r["peak"])


def _peak(r: Mapping[str, Any]) -> int:
    snap = r["peak_members"]
    return len(snap) if snap else int(r["peak"])


def _age(r: Mapping[str, Any]) -> int:
    end = r["died_at"] if r["state"] == "dead" else r["db_now"]
    return _days(r["fruited_at"], end)


async def gallery_row(conn: store.Conn, serial: int) -> dict[str, Any] | None:
    """One public gallery's colony row, or None when it is unknown or not yet public."""
    if not await galleries_public(conn):
        return None
    cur = await conn.execute(
        (_COLONY_SQL + " and c.serial = %(serial)s").encode(),
        {"states": list(GALLERY_STATES), "serial": serial},
    )
    row: dict[str, Any] | None = await cur.fetchone()
    return row


async def gallery_card(conn: store.Conn, serial: int) -> dict[str, Any] | None:
    """What a link preview needs: `{title, state, size, died_days, cover}`, cover being a
    grid item or None."""
    r = await gallery_row(conn, serial)
    if r is None:
        return None
    cover = await _items(conn, [r["cover_image_id"]] if r["cover_image_id"] else [])
    return {
        "title": gallery_title(serial, r["title"]),
        "state": PUBLIC_STATES[r["state"]],
        "size": _size(r),
        "died_days": _days(r["died_at"], r["db_now"]) if r["state"] == "dead" else None,
        "cover": _thumb(cover.get(r["cover_image_id"])),
    }


@router.get("/api/galleries")
async def list_galleries(request: Request, state: str = "all") -> list[dict[str, Any]]:
    """Living, then fading, then dead. Living and fading newest fruiting first, dead
    most recent death first; serial breaks ties."""
    states = FILTERS.get(state)
    if states is None:
        raise HTTPException(status_code=400, detail="bad state")
    async with request.app.state.pool.connection() as conn:
        if not await galleries_public(conn):
            return []
        cur = await conn.execute(
            (
                _COLONY_SQL
                + """
                order by array_position(%(order)s::text[], c.state),
                         case when c.state = 'dead' then c.died_tick else c.fruited_tick end
                           desc nulls last,
                         c.serial desc
                """
            ).encode(),
            {"states": list(states), "order": list(GALLERY_STATES)},
        )
        rows = await cur.fetchall()
        covers = await _items(conn, [r["cover_image_id"] for r in rows if r["cover_image_id"]])
    return [
        {
            "serial": int(r["serial"]),
            "title": gallery_title(int(r["serial"]), r["title"]),
            "state": PUBLIC_STATES[r["state"]],
            "layer": public_kind(r["layer"]),
            "size": _size(r),
            "fruited_at": _iso(r["fruited_at"]),
            "died_at": _iso(r["died_at"]),
            "cause": r["cause"],
            "cover": _thumb(covers.get(r["cover_image_id"])),
            "age_days": _age(r),
        }
        for r in rows
    ]


async def _named(conn: store.Conn, colony_id: int | None) -> dict[str, Any] | None:
    """`{serial, title}` of another colony when it has an address."""
    if colony_id is None:
        return None
    cur = await conn.execute(
        "select serial, title from colonies where id = %s and serial is not null"
        " and state = any(%s)",
        (colony_id, list(GALLERY_STATES)),
    )
    r = await cur.fetchone()
    return (
        None
        if r is None
        else {"serial": r["serial"], "title": gallery_title(r["serial"], r["title"])}
    )


async def _members(
    conn: store.Conn, colony_id: int, layer: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Current live members, the visible hyphae between them, and the other living
    galleries they share."""
    cur = await conn.execute(
        """
        select distinct on (m.image_id) m.image_id, m.founder, m.joined_tick
          from colony_members m
          join images i on i.id = m.image_id and i.status = 'live'
         where m.colony_id = %s and m.left_tick is null
         order by m.image_id, m.founder desc
        """,
        (colony_id,),
    )
    rows = await cur.fetchall()
    rows.sort(key=lambda r: (not r["founder"], r["joined_tick"], r["image_id"]))
    ids = [int(r["image_id"]) for r in rows]
    if not ids:
        return [], [], []
    items = await _items(conn, ids)

    cur = await conn.execute(
        """
        select m.image_id, c.serial, c.title
          from colony_members m
          join colonies c on c.id = m.colony_id
         where m.image_id = any(%(ids)s) and m.left_tick is null and c.id <> %(c)s
           and c.serial is not null and c.state in ('alive', 'fading')
         order by c.serial, m.image_id
        """,
        {"ids": ids, "c": colony_id},
    )
    others: dict[int, list[dict[str, Any]]] = {}
    shared: dict[int, dict[str, Any]] = {}
    for r in await cur.fetchall():
        named = {"serial": r["serial"], "title": gallery_title(r["serial"], r["title"])}
        mine = others.setdefault(int(r["image_id"]), [])
        if named not in mine:
            mine.append(named)
            entry = shared.setdefault(r["serial"], {**named, "n": 0})
            entry["n"] += 1
    shares = sorted(shared.values(), key=lambda s: (-s["n"], s["serial"]))

    t = await store.load_tunables(conn)
    cur = await conn.execute(
        """
        select least(src, dst) as a, greatest(src, dst) as b, max(strength) as s
          from edge_state
         where layer = %(layer)s and src = any(%(ids)s) and dst = any(%(ids)s)
         group by 1, 2
        having max(strength) >= %(vis)s
         order by 1, 2
        """,
        {"layer": layer, "ids": ids, "vis": t.hypha_visible},
    )
    slugs = {i: items[i]["slug"] for i in items}
    edges = [
        {"a": slugs[r["a"]], "b": slugs[r["b"]], "strength": round(float(r["s"]), 3)}
        for r in await cur.fetchall()
        if r["a"] in slugs and r["b"] in slugs
    ]

    members = [
        {**items[i], "founder": bool(r["founder"]), "others": others.get(i, [])}
        for r in rows
        if (i := int(r["image_id"])) in items
    ]
    return members, edges, shares


async def _former(
    conn: store.Conn, snapshot: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """The members at the peak, in snapshot order: live ones as items to link, the
    rest by the slug they had."""
    if not snapshot:
        return []
    ids = [int(m["id"]) for m in snapshot]
    items = await _items(conn, ids)
    out: list[dict[str, Any]] = []
    for m in snapshot:
        item = items.get(int(m["id"]))
        out.append({**item, "live": True} if item else {"slug": str(m["slug"]), "live": False})
    return out


async def _series(conn: store.Conn, colony_id: int) -> list[dict[str, Any]]:
    """Size and strength per tick from forming on, thinned to at most SERIES_POINTS,
    always ending on the last tick."""
    cur = await conn.execute(
        """
        select tick, size, mean_strength, total_strength from (
          select tick, size, mean_strength, total_strength,
                 row_number() over (order by tick) as rn, count(*) over () as n
            from colony_ticks where colony_id = %(c)s
        ) t
         where (n - rn) %% greatest(1, ceil(n / %(points)s::float8)::int) = 0
         order by tick
        """,
        {"c": colony_id, "points": SERIES_POINTS},
    )
    return [
        {
            "tick": int(r["tick"]),
            "size": int(r["size"]),
            "mean_strength": round(float(r["mean_strength"]), 4),
            "total_strength": (
                None if r["total_strength"] is None else round(float(r["total_strength"]), 4)
            ),
        }
        for r in await cur.fetchall()
    ]


async def _events(conn: store.Conn, colony_id: int) -> list[dict[str, Any]]:
    """The gallery's public history, newest first. A join or leave by a picture that is
    no longer live is left out, as the picture is."""
    cur = await conn.execute(
        """
        select e.kind, e.scheduled_at, i.slug, oc.serial as other_serial,
               oc.title as other_title
          from colony_events e
          left join images i on i.id = e.image_id and i.status = 'live'
          left join colonies oc on oc.id = e.other_colony_id and oc.serial is not null
               and oc.state = any(%(states)s)
         where e.colony_id = %(c)s and e.kind = any(%(kinds)s)
           and (e.kind <> all(%(picture)s) or i.id is not null)
         order by e.tick desc, e.id desc
        """,
        {
            "c": colony_id,
            "kinds": list(GALLERY_EVENTS),
            "picture": list(_PICTURE_EVENTS),
            "states": list(GALLERY_STATES),
        },
    )
    return [
        {
            "at": _iso(r["scheduled_at"]),
            "kind": r["kind"],
            "slug": r["slug"],
            "other": (
                None
                if r["other_serial"] is None
                else {
                    "serial": r["other_serial"],
                    "title": gallery_title(r["other_serial"], r["other_title"]),
                }
            ),
        }
        for r in await cur.fetchall()
    ]


@router.get("/api/galleries/{serial}")
async def gallery_detail(request: Request, serial: str) -> dict[str, Any]:
    n = parse_serial(serial)
    if n is None:
        raise _not_here()
    async with request.app.state.pool.connection() as conn:
        r = await gallery_row(conn, n)
        if r is None:
            raise _not_here()
        cid = int(r["id"])
        dead = r["state"] == "dead"
        if dead:
            members: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            shares: list[dict[str, Any]] = []
            former = await _former(conn, r["peak_members"])
        else:
            members, edges, shares = await _members(conn, cid, r["layer"])
            former = []
        cur = await conn.execute(
            "select serial, title from colonies where parent_id = %s and serial is not null"
            " and state = any(%s) order by serial",
            (cid, list(GALLERY_STATES)),
        )
        children = [
            {"serial": c["serial"], "title": gallery_title(c["serial"], c["title"])}
            for c in await cur.fetchall()
        ]
        covers = await _items(conn, [r["cover_image_id"]] if r["cover_image_id"] else [])
        out = {
            "serial": n,
            "title": gallery_title(n, r["title"]),
            "state": PUBLIC_STATES[r["state"]],
            "layer": public_kind(r["layer"]),
            "size": len(members) if not dead else _size(r),
            "peak": _peak(r),
            "formed_at": _iso(r["formed_at"]),
            "fruited_at": _iso(r["fruited_at"]),
            "faded_at": _iso(r["faded_at"]) if r["state"] == "fading" else None,
            "died_at": _iso(r["died_at"]),
            "cause": r["cause"],
            "age_days": _age(r),
            "cover": _thumb(covers.get(r["cover_image_id"])),
            "parent": await _named(conn, r["parent_id"]),
            "absorbed_by": await _named(conn, r["absorbed_by"]),
            "children": children,
            "members": members,
            "former_members": former,
            "edges": edges,
            "series": await _series(conn, cid),
            "events": await _events(conn, cid),
            "shares": shares,
        }
    return out


@router.get("/api/mycelium/log")
async def mycelium_log(request: Request, before: int | None = Query(None, ge=1)) -> dict[str, Any]:
    """Fruitings, deaths, joins and leaves of every gallery, newest first, a page at a
    time. `next` is the `before` for the following page, null on the last."""
    async with request.app.state.pool.connection() as conn:
        if not await galleries_public(conn):
            return {"events": [], "next": None}
        where = ""
        params: dict[str, Any] = {
            "kinds": list(LOG_EVENTS),
            "picture": list(_PICTURE_EVENTS),
            "states": list(GALLERY_STATES),
            "limit": LOG_PAGE + 1,
        }
        if before is not None:
            # Tick first: a layer replay can write old ticks' events after newer ones.
            where = (
                " and (e.tick, e.id) < (select tick, id from colony_events where id = %(before)s)"
            )
            params["before"] = before
        cur = await conn.execute(
            f"""
            select e.id, e.kind, e.scheduled_at, c.serial, c.title, i.slug,
                   oc.serial as other_serial, oc.title as other_title
              from colony_events e
              join colonies c on c.id = e.colony_id and c.serial is not null
                   and c.state = any(%(states)s)
              left join images i on i.id = e.image_id and i.status = 'live'
              left join colonies oc on oc.id = e.other_colony_id and oc.serial is not null
                   and oc.state = any(%(states)s)
             where e.kind = any(%(kinds)s)
               and (e.kind <> all(%(picture)s) or i.id is not null) {where}
             order by e.tick desc, e.id desc
             limit %(limit)s
            """.encode(),
            params,
        )
        rows = await cur.fetchall()
    more = len(rows) > LOG_PAGE
    rows = rows[:LOG_PAGE]
    events = [
        {
            "id": int(r["id"]),
            "at": _iso(r["scheduled_at"]),
            "kind": "died" if r["kind"] in DEATHS else r["kind"],
            "cause": r["kind"] if r["kind"] in DEATHS else None,
            "gallery": {"serial": r["serial"], "title": gallery_title(r["serial"], r["title"])},
            "slug": r["slug"],
            "other": (
                None
                if r["other_serial"] is None
                else {
                    "serial": r["other_serial"],
                    "title": gallery_title(r["other_serial"], r["other_title"]),
                }
            ),
        }
        for r in rows
    ]
    return {"events": events, "next": events[-1]["id"] if more else None}
