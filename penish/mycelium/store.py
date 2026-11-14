"""The only module of the organism that touches Postgres."""

from __future__ import annotations

from collections.abc import Sequence

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from penish.mycelium import overgrowth
from penish.mycelium.tunables import DEFAULTS, Tunables


async def overgrowth_by_id(
    conn: AsyncConnection[DictRow], ids: Sequence[int], tunables: Tunables = DEFAULTS
) -> dict[int, float]:
    """Overgrowth in 0..1 per picture, rounded to 3 places; ids not found are absent.

    One query for the whole batch. Time is the database clock, the same one a view
    writes with, so a picture viewed in this instant reads exactly 0.
    """
    if not ids:
        return {}
    # The pace is one row computed once, not per picture.
    cur = await conn.execute(
        """
        with pace as (
          select (select count(*) from images
                   where status = 'live' and published_at is not null)::int as live,
                 (select coalesce(sum(n), 0) from view_days
                   where day > current_date - 7)::float8 as views
        )
        select i.id,
               extract(epoch from now() - coalesce(a.last_viewed_at, i.published_at))::float8
                 / 3600.0 as hours,
               coalesce(a.neighbor_level, 0)::float8 as neighbor_level,
               pace.live, pace.views
          from images i
          left join picture_attention a on a.image_id = i.id
          cross join pace
         where i.id = any(%(ids)s)
        """,
        {"ids": list(ids)},
    )
    rows = await cur.fetchall()
    if not rows:
        return {}
    # The last seven days, today included.
    vpd = overgrowth.views_per_day(rows[0]["views"])
    tau = overgrowth.tau_hours(rows[0]["live"], vpd, tunables)
    out: dict[int, float] = {}
    for r in rows:
        # Never seen and never published: nothing to have neglected yet.
        if r["hours"] is None:
            out[r["id"]] = 0.0
            continue
        base = overgrowth.base_from_hours(r["hours"], tau)
        lvl = overgrowth.level(base, r["neighbor_level"], tunables)
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
