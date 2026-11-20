"""Collections in public: the list, a collection with its books, and one book's slots.

Only published collections exist here; anything else is not here. Filing internals
(anchors, tag hints, scores) never leave the database through these routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from penish.api.media import variants_for_many
from penish.api.routes_images import grid_items
from penish.collection.store import record_pairs
from penish.mycelium.store import overgrowth_by_id

router = APIRouter(prefix="/api/collections")

# A leaf whose picture is not live keeps its slot, but nobody outside can see it.
_FILLED = """
    select l.book, l.position, i.id, i.slug, i.pub_sha, i.width, i.height, i.blurhash,
           i.palette
      from collection_leaves l
      join images i on i.id = l.image_id
     where l.collection_id = %s and i.status = 'live' and i.published_at is not null
"""


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


async def published(conn: Any, slug: str) -> dict[str, Any] | None:
    cur = await conn.execute("select * from collections where slug = %s and published", (slug,))
    row: dict[str, Any] | None = await cur.fetchone()
    return row


def _head(c: dict[str, Any], leaves: int) -> dict[str, Any]:
    return {
        "slug": c["slug"],
        "title": c["title"],
        "abbrev": c["abbrev"],
        "abbrev_latin": c["abbrev_latin"],
        "book_count": c["book_count"],
        "leaves_per_book": c["leaves_per_book"],
        "leaves": leaves,
        "capacity": c["book_count"] * c["leaves_per_book"],
    }


def _small(variants: list[dict[str, Any]]) -> str | None:
    webps = [v["webp"] for v in variants if v["webp"]]
    return webps[0] if webps else None


@router.get("")
async def list_collections(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            """
            select c.*,
                   (select count(*) from collection_leaves l join images i on i.id = l.image_id
                     where l.collection_id = c.id and i.status = 'live')::int as n
              from collections c
             where c.published
             order by c.id
            """
        )
        rows = await cur.fetchall()
    return {"collections": [_head(r, r["n"]) for r in rows]}


@router.get("/{slug}")
async def collection(request: Request, slug: str) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await published(conn, slug)
        if c is None:
            raise _not_here()
        cur = await conn.execute(
            "select number, title from collection_books where collection_id = %s order by number",
            (c["id"],),
        )
        books = await cur.fetchall()
        cur = await conn.execute(f"{_FILLED} order by l.book, l.position".encode(), (c["id"],))
        filled = await cur.fetchall()
    variants = variants_for_many((r["pub_sha"], r["width"], r["height"]) for r in filled)
    by_book: dict[int, list[dict[str, Any]]] = {}
    for r in filled:
        by_book.setdefault(r["book"], []).append(
            {
                "position": r["position"],
                "slug": r["slug"],
                "blurhash": r["blurhash"],
                "palette": r["palette"],
                "src": _small(variants[r["pub_sha"]]),
            }
        )
    return {
        **_head(c, len(filled)),
        "preamble": c["preamble"],
        # Owner text, stated as written: the gloss under the title, the catalogue record
        # as [label, value] pairs, and the dealer's description, quoted apart.
        "gloss": c["gloss"],
        "record": [list(p) for p in record_pairs(c["record"])],
        "vendor_text": c["vendor_text"],
        "books": [
            {"number": b["number"], "title": b["title"], "leaves": by_book.get(b["number"], [])}
            for b in books
        ],
    }


@router.get("/{slug}/books/{number}")
async def book(request: Request, slug: str, number: int) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await published(conn, slug)
        if c is None:
            raise _not_here()
        cur = await conn.execute(
            "select number, title from collection_books where collection_id = %s and number = %s",
            (c["id"], number),
        )
        b = await cur.fetchone()
        if b is None:
            raise _not_here()
        cur = await conn.execute(
            b"""
            select count(*)::int as n from collection_leaves l join images i on i.id = l.image_id
             where l.collection_id = %s and i.status = 'live'
            """,
            (c["id"],),
        )
        total = (await cur.fetchone() or {"n": 0})["n"]
        cur = await conn.execute(
            f"{_FILLED} and l.book = %s order by l.position".encode(), (c["id"], number)
        )
        filled = await cur.fetchall()
        og = await overgrowth_by_id(conn, [r["id"] for r in filled])
    # Every slot, empty ones as null, so the grid never closes up around a gap.
    slots: list[dict[str, Any] | None] = [None] * int(c["leaves_per_book"])
    for r, item in zip(filled, grid_items(filled, og), strict=True):
        if r["position"] <= len(slots):
            slots[r["position"] - 1] = {**item, "position": r["position"]}
    return {
        "collection": _head(c, total),
        "number": b["number"],
        "title": b["title"],
        "slots": slots,
    }
