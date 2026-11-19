"""Collections in public: the list, a collection with its books, one book's slots, and
the leaf line on a picture.

Read-only. An unpublished collection does not exist here. Anchors, their vectors, the
tag hints and every score stay out: the site never explains how a leaf was filed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from penish.api.media import variants_for_many

router = APIRouter()

_LIVE_LEAF = """
  from collection_leaves l
  join images i on i.id = l.image_id
  join collection_books b on b.id = l.book_id
  where l.collection_id = %(cid)s and i.status = 'live' and i.published_at is not null
"""


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


def _head(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": r["slug"],
        "title": r["title"],
        "abbrev": r["abbrev"],
        "abbrev_latin": r["abbrev_latin"],
        "book_count": r["book_count"],
        "leaves_per_book": r["leaves_per_book"],
    }


async def _published(conn: Any, slug: str) -> dict[str, Any]:
    cur = await conn.execute("select * from collections where slug = %s and published", (slug,))
    row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        raise _not_here()
    return row


async def _leaves(conn: Any, cid: int, number: int | None = None) -> list[dict[str, Any]]:
    extra = " and b.number = %(n)s" if number is not None else ""
    cur = await conn.execute(
        f"""
        select b.number, l.position, i.slug, i.pub_sha, i.width, i.height, i.blurhash, i.palette
        {_LIVE_LEAF} {extra}
        order by b.number, l.position
        """.encode(),
        {"cid": cid, "n": number},
    )
    rows = list(await cur.fetchall())
    variants = variants_for_many((r["pub_sha"], r["width"], r["height"]) for r in rows)
    return [
        {
            "book": r["number"],
            "position": r["position"],
            "slug": r["slug"],
            "width": r["width"],
            "height": r["height"],
            "blurhash": r["blurhash"],
            "palette": r["palette"],
            "variants": variants[r["pub_sha"]],
        }
        for r in rows
    ]


async def any_published(conn: Any) -> bool:
    cur = await conn.execute("select exists (select 1 from collections where published) as e")
    row = await cur.fetchone()
    return bool(row and row["e"])


@router.get("/api/collections")
async def list_collections(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            """
            select c.*, (select count(*) from collection_leaves l join images i on i.id = l.image_id
                          where l.collection_id = c.id and i.status = 'live'
                            and i.published_at is not null) as n
              from collections c where c.published order by c.id
            """
        )
        rows = await cur.fetchall()
    return {
        "collections": [
            {**_head(r), "leaves": r["n"], "capacity": r["book_count"] * r["leaves_per_book"]}
            for r in rows
        ]
    }


@router.get("/api/collections/{slug}")
async def collection(request: Request, slug: str) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _published(conn, slug)
        cur = await conn.execute(
            "select number, title from collection_books where collection_id = %s order by number",
            (c["id"],),
        )
        books = list(await cur.fetchall())
        leaves = await _leaves(conn, c["id"])
    by_book: dict[int, list[dict[str, Any]]] = {}
    for lf in leaves:
        by_book.setdefault(lf.pop("book"), []).append(lf)
    return {
        **_head(c),
        "preamble": c["preamble"],
        "leaves": len(leaves),
        "capacity": c["book_count"] * c["leaves_per_book"],
        "books": [
            {"number": b["number"], "title": b["title"], "leaves": by_book.get(b["number"], [])}
            for b in books
        ],
    }


@router.get("/api/collections/{slug}/books/{number}")
async def book(request: Request, slug: str, number: int) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _published(conn, slug)
        cur = await conn.execute(
            "select number, title from collection_books where collection_id = %s and number = %s",
            (c["id"], number),
        )
        b = await cur.fetchone()
        if b is None:
            raise _not_here()
        leaves = await _leaves(conn, c["id"], number)
    slots: list[dict[str, Any] | None] = [None] * c["leaves_per_book"]
    for lf in leaves:
        lf.pop("book")
        slots[lf["position"] - 1] = lf
    return {"collection": _head(c), "number": b["number"], "title": b["title"], "slots": slots}


async def leaf_for(conn: Any, image_id: int) -> dict[str, Any] | None:
    """Where a picture sits in a published collection, and where it sat before its last
    move. The latest history row's `from` is that place, an unfiling included, so a leaf
    filed again shows where it used to be."""
    cur = await conn.execute(
        """
        select c.slug, c.abbrev, c.abbrev_latin, b.number, l.position,
               h.from_book, h.from_position
          from collection_leaves l
          join collections c on c.id = l.collection_id
          join collection_books b on b.id = l.book_id
          left join lateral (
            select from_book, from_position from collection_leaf_history h
             where h.image_id = l.image_id and h.collection_id = l.collection_id
             order by h.at desc, h.id desc limit 1
          ) h on true
         where l.image_id = %s and c.published
         order by c.id
         limit 1
        """,
        (image_id,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    previous = None
    if r["from_book"] is not None and (r["from_book"], r["from_position"]) != (
        r["number"],
        r["position"],
    ):
        previous = {"book": r["from_book"], "position": r["from_position"]}
    return {
        "collection": {"slug": r["slug"], "abbrev": r["abbrev"], "abbrev_latin": r["abbrev_latin"]},
        "book": r["number"],
        "position": r["position"],
        "previous": previous,
    }
