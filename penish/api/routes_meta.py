from typing import Any

from fastapi import APIRouter, Request

from penish.config import get_settings

router = APIRouter()


async def random_haiku(request: Request) -> dict[str, Any] | None:
    # random() over a full scan is fine while the deck is a few hundred rows. A gallery's
    # haiku is shown on its gallery and nowhere else.
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, l1, l2, l3 from haikus where active and colony_id is null"
            " order by random() limit 1"
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "lines": [row["l1"], row["l2"], row["l3"]]}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, bool]:
    async with request.app.state.pool.connection() as conn:
        await conn.execute("select 1")
    return {"ok": True}


@router.get("/api/haiku")
async def haiku(request: Request) -> dict[str, Any]:
    h = await random_haiku(request)
    if h is None:
        # Only reachable if the seed deck was deactivated; the client falls back to its own copy.
        return {"id": None, "lines": []}
    return h


# The latest change to a published collection this week whose leaf is still where the
# change put it: a later move or an unfiling would make the line false, so the join to
# collection_leaves on the destination drops it. At one moment a filing outranks the
# shifts it caused in its book.
_CHANGED = """
    select c.slug as collection, c.abbrev, c.abbrev_latin, i.slug, h.to_book, h.to_position,
           h.from_book, h.from_position
      from collection_leaf_history h
      join collections c on c.id = h.collection_id and c.published
      join collection_leaves l on l.image_id = h.image_id and l.collection_id = h.collection_id
                              and l.book = h.to_book and l.position = h.to_position
      join images i on i.id = h.image_id
     where h.at > now() - interval '7 days'
       and i.status = 'live' and i.published_at is not null
       and (h.from_book, h.from_position) is distinct from (h.to_book, h.to_position)
     order by h.at desc, (h.from_book is null) desc, h.id desc
     limit 1
"""


async def collection_change(conn: Any) -> dict[str, Any] | None:
    """`{collection: {slug, abbrev, abbrev_latin}, book, position, slug, from}`, `from`
    being the leaf's place before (null for a filing), or None for a quiet week."""
    cur = await conn.execute(_CHANGED)
    r = await cur.fetchone()
    if r is None:
        return None
    return {
        "collection": {
            "slug": r["collection"],
            "abbrev": r["abbrev"],
            "abbrev_latin": r["abbrev_latin"],
        },
        "book": r["to_book"],
        "position": r["to_position"],
        "slug": r["slug"],
        "from": None
        if r["from_book"] is None
        else {"book": r["from_book"], "position": r["from_position"]},
    }


@router.get("/api/stats")
async def stats(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("select count(*) as n from images where status = 'live'")
        pictures = (await cur.fetchone() or {"n": 0})["n"]
        cur = await conn.execute(
            "select temperature from corpus_stats where kind = 'description' and model = %s",
            (get_settings().embed_model,),
        )
        row = await cur.fetchone()
        changed = await collection_change(conn)
    out = {"pictures": pictures, "temperature": row["temperature"] if row else None}
    # Present only in a week something changed, so a quiet week reads as it always has.
    if changed is not None:
        out["changed"] = changed
    return out
