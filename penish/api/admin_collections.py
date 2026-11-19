"""Admin collections: the collection's own text and switches, the books' anchors and hints,
every leaf by position, owner moves and pins, unfiled requests, re-sorts and history.

Every write answers with the whole collection, so the page just swaps it in.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from penish.api.admin import _gate, require_admin
from penish.api.media import variants_for_many
from penish.collection import store
from penish.collection.plan import LIMITS, Tuning
from penish.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/collections",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

_HISTORY = 50


def original_url(image_id: int) -> str:
    # The admin pictures route that serves the clean original, for a picture held back.
    return f"/api/admin/pictures/{image_id}/original"


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


async def _collection(conn: Any, slug: str) -> dict[str, Any]:
    c = await store.collection_by_slug(conn, slug)
    if c is None:
        raise _not_here()
    return c


def _thumbs(rows: list[dict[str, Any]]) -> dict[int, str | None]:
    variants = variants_for_many(
        (r["pub_sha"], r["width"], r["height"]) for r in rows if r["pub_sha"]
    )
    out: dict[int, str | None] = {}
    for r in rows:
        # An unpublished picture's derivatives are held where nothing serves them.
        if r["status"] == "unpublished" or not r["pub_sha"]:
            out[r["id"]] = original_url(r["id"])
            continue
        webps = [v["webp"] for v in variants.get(r["pub_sha"], []) if v["webp"]]
        out[r["id"]] = webps[0] if webps else None
    return out


_PICTURE = "i.id, i.slug, i.status::text as status, i.pub_sha, i.width, i.height"


async def detail(conn: Any, c: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    cur = await conn.execute(
        """
        select number, title, anchor_text, tag_hints,
               anchor_embedding is not null and anchor_model = %s and anchor_dim = %s as anchored
          from collection_books where collection_id = %s order by number
        """,
        (settings.embed_model, settings.embed_dim, c["id"]),
    )
    books = list(await cur.fetchall())
    cur = await conn.execute(
        f"""
        select {_PICTURE}, l.book, l.position, l.pinned, l.book_pinned
          from collection_leaves l join images i on i.id = l.image_id
         where l.collection_id = %s order by l.book, l.position
        """.encode(),
        (c["id"],),
    )
    leaves = list(await cur.fetchall())
    cur = await conn.execute(
        f"""
        select {_PICTURE}, r.book, r.position, r.error, r.created_at,
               (select j.status::text from jobs j
                 where j.type = %s and j.payload->>'image_id' = r.image_id::text
                   and j.payload->>'collection_id' = r.collection_id::text
                 order by j.id desc limit 1) as job
          from collection_requests r join images i on i.id = r.image_id
         where r.collection_id = %s order by r.created_at, r.image_id
        """.encode(),
        (store.FILE_JOB, c["id"]),
    )
    requests = list(await cur.fetchall())
    cur = await conn.execute(
        """
        select h.id, h.image_id, i.slug, h.from_book, h.from_position, h.to_book,
               h.to_position, h.reason, h.at
          from collection_leaf_history h join images i on i.id = h.image_id
         where h.collection_id = %s order by h.id desc limit %s
        """,
        (c["id"], _HISTORY),
    )
    history = await cur.fetchall()
    cur = await conn.execute(
        """
        select status::text as status, last_error from jobs
         where type = %s and payload->>'collection_id' = %s order by id desc limit 1
        """,
        (store.RESORT_JOB, str(c["id"])),
    )
    resort = await cur.fetchone()

    thumbs = _thumbs([*leaves, *requests])
    by_book: dict[int, list[dict[str, Any]]] = {}
    for r in leaves:
        by_book.setdefault(r["book"], []).append(
            {
                "image_id": r["id"],
                "slug": r["slug"],
                "status": r["status"],
                "thumb": thumbs[r["id"]],
                "position": r["position"],
                "pinned": r["pinned"],
                "book_pinned": r["book_pinned"],
            }
        )
    return {
        "id": c["id"],
        "slug": c["slug"],
        "title": c["title"],
        "abbrev": c["abbrev"],
        "abbrev_latin": c["abbrev_latin"],
        "preamble": c["preamble"],
        "book_count": c["book_count"],
        "leaves_per_book": c["leaves_per_book"],
        "published": c["published"],
        "tuning": Tuning.from_json(c["tuning"]).to_json(),
        "tuning_default": Tuning().to_json(),
        "last_resort_at": _iso(c["last_resort_at"]),
        "resort": resort,
        "books": [
            {
                "number": b["number"],
                "title": b["title"],
                "anchor_text": b["anchor_text"],
                "tag_hints": b["tag_hints"],
                "anchored": bool(b["anchored"]),
                "leaves": by_book.get(b["number"], []),
            }
            for b in books
        ],
        "requests": [
            {
                "image_id": r["id"],
                "slug": r["slug"],
                "status": r["status"],
                "thumb": thumbs[r["id"]],
                "book": r["book"],
                "position": r["position"],
                "error": r["error"],
                "job": r["job"],
                "created_at": _iso(r["created_at"]),
            }
            for r in requests
        ],
        "history": [{**h, "at": _iso(h["at"])} for h in history],
    }


async def _answer(request: Request, slug: str) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        return await detail(conn, await _collection(conn, slug))


# -- reading ---------------------------------------------------------------------


@router.get("")
async def list_collections(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select slug, title, abbrev, abbrev_latin, book_count, leaves_per_book, published"
            " from collections order by id"
        )
        return {"collections": list(await cur.fetchall())}


@router.get("/{slug}")
async def get_collection(request: Request, slug: str) -> dict[str, Any]:
    return await _answer(request, slug)


# -- the collection and its books ---------------------------------------------------


class CollectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=200)
    abbrev: str | None = Field(None, min_length=1, max_length=20)
    abbrev_latin: str | None = Field(None, min_length=1, max_length=20)
    preamble: str | None = Field(None, max_length=20000)
    published: bool | None = None
    tuning: dict[str, float | bool] | None = None


@router.patch("/{slug}")
async def edit_collection(request: Request, slug: str, body: CollectionEdit) -> dict[str, Any]:
    given = body.model_fields_set
    sets: list[str] = []
    params: list[Any] = []
    for f in ("title", "abbrev", "abbrev_latin", "preamble", "published"):
        v = getattr(body, f)
        if f in given and v is not None:
            sets.append(f"{f} = %s")
            params.append(v.strip() if isinstance(v, str) else v)
    if "tuning" in given and body.tuning is not None:
        for k, v in body.tuning.items():
            if k == "weekly_resort":
                if not isinstance(v, bool):
                    raise HTTPException(status_code=422, detail=f"bad {k}")
            elif k not in LIMITS or isinstance(v, bool) or not LIMITS[k][0] <= v <= LIMITS[k][1]:
                raise HTTPException(status_code=422, detail=f"bad {k}")
        sets.append("tuning = tuning || %s")
        params.append(Jsonb(body.tuning))
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        if sets:
            await conn.execute(
                f"update collections set {', '.join(sets)}, updated_at = now() where id = %s",
                (*params, c["id"]),
            )
    return await _answer(request, slug)


class BookEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=200)
    anchor_text: str | None = Field(None, max_length=20000)
    tag_hints: list[str] | None = Field(None, max_length=40)


@router.patch("/{slug}/books/{number}")
async def edit_book(request: Request, slug: str, number: int, body: BookEdit) -> dict[str, Any]:
    given = body.model_fields_set
    async with request.app.state.pool.connection() as conn, conn.transaction():
        c = await _collection(conn, slug)
        cur = await conn.execute(
            "select anchor_text from collection_books where collection_id = %s and number = %s"
            " for update",
            (c["id"], number),
        )
        b = await cur.fetchone()
        if b is None:
            raise _not_here()
        if "title" in given:
            # Empty means untitled: the page then shows only the numeral.
            title = (body.title or "").strip() or None
            await conn.execute(
                "update collection_books set title = %s, updated_at = now()"
                " where collection_id = %s and number = %s",
                (title, c["id"], number),
            )
        if "tag_hints" in given and body.tag_hints is not None:
            hints = list(dict.fromkeys(t.strip().lower() for t in body.tag_hints if t.strip()))
            await conn.execute(
                "update collection_books set tag_hints = %s, updated_at = now()"
                " where collection_id = %s and number = %s",
                (hints, c["id"], number),
            )
        if "anchor_text" in given and body.anchor_text is not None:
            text = body.anchor_text.strip()
            if text != b["anchor_text"]:
                # Dropped with the text so the filer never uses a vector for words that are
                # gone; the job rides the same transaction so a crash cannot lose it.
                await conn.execute(
                    "update collection_books set anchor_text = %s, anchor_embedding = null,"
                    " anchor_model = null, anchor_dim = null, updated_at = now()"
                    " where collection_id = %s and number = %s",
                    (text, c["id"], number),
                )
                await conn.execute(
                    "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
                    " on conflict do nothing",
                    (
                        store.ANCHOR_JOB,
                        Jsonb({"collection_id": c["id"]}),
                        f"{store.ANCHOR_JOB}:{c['id']}",
                    ),
                )
    return await _answer(request, slug)


# -- leaves -----------------------------------------------------------------------


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book: int | None = Field(None, ge=1)
    position: int | None = Field(None, ge=1)


def _refused(e: store.Refused) -> HTTPException:
    return HTTPException(status_code=409, detail=str(e))


@router.put("/{slug}/leaves/{image_id}")
async def put_leaf(request: Request, slug: str, image_id: int, body: Place) -> dict[str, Any]:
    """A filed picture with a book given moves there, pinned. An unfiled one is marked,
    and the filer takes it from there (pinned to whatever was given)."""
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        cur = await conn.execute("select id from images where id = %s", (image_id,))
        if await cur.fetchone() is None:
            raise _not_here()
        cur = await conn.execute(
            "select 1 from collection_leaves where image_id = %s and collection_id = %s",
            (image_id, c["id"]),
        )
        filed = await cur.fetchone() is not None
        try:
            if filed and body.book is not None:
                await store.place(conn, c, image_id, body.book, body.position, get_settings())
            elif not filed:
                async with conn.transaction():
                    await store.request(conn, c, image_id, body.book, body.position)
        except store.Refused as e:
            raise _refused(e) from None
    return await _answer(request, slug)


@router.delete("/{slug}/leaves/{image_id}")
async def delete_leaf(request: Request, slug: str, image_id: int) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        if not await store.unfile(conn, c["id"], image_id):
            raise _not_here()
    return await _answer(request, slug)


class Pin(BaseModel):
    pinned: bool


@router.post("/{slug}/leaves/{image_id}/pin")
async def pin_leaf(request: Request, slug: str, image_id: int, body: Pin) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        try:
            await store.set_pin(conn, c, image_id, body.pinned, get_settings())
        except store.Refused as e:
            raise _refused(e) from None
    return await _answer(request, slug)


@router.post("/{slug}/requests/{image_id}/retry")
async def retry_request(request: Request, slug: str, image_id: int) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn, conn.transaction():
        c = await _collection(conn, slug)
        cur = await conn.execute(
            "update collection_requests set error = null, updated_at = now()"
            " where image_id = %s and collection_id = %s",
            (image_id, c["id"]),
        )
        if cur.rowcount == 0:
            raise _not_here()
        await store.enqueue_file(conn, image_id, c["id"])
    return await _answer(request, slug)


# -- re-sort ------------------------------------------------------------------------


class Resort(BaseModel):
    dry_run: bool = True


@router.post("/{slug}/resort")
async def resort(request: Request, slug: str, body: Resort) -> dict[str, Any]:
    """A dry run plans against the vectors as they stand and answers with the moves. The
    real one is a job, so the worker can embed any anchor still waiting first."""
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        if body.dry_run:
            moves = await store.resort(conn, c, get_settings(), dry_run=True)
            cur = await conn.execute(
                "select id, slug from images where id = any(%s)", ([m.image_id for m in moves],)
            )
            slugs = {r["id"]: r["slug"] for r in await cur.fetchall()}
            return {
                "moves": [
                    {
                        "image_id": m.image_id,
                        "slug": slugs.get(m.image_id),
                        "before": m.before,
                        "after": m.after,
                    }
                    for m in moves
                ]
            }
        await conn.execute(
            "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
            " on conflict do nothing",
            (store.RESORT_JOB, Jsonb({"collection_id": c["id"]}), f"{store.RESORT_JOB}:{c['id']}"),
        )
    return await _answer(request, slug)


# -- the admin picture page -----------------------------------------------------------


async def for_picture(conn: Any, image_id: int) -> list[dict[str, Any]]:
    """Each collection with this picture's place in it, or its waiting request."""
    cur = await conn.execute(
        """
        select c.slug, c.title, c.abbrev_latin, c.book_count, c.leaves_per_book,
               l.book, l.position, l.pinned, l.book_pinned,
               r.book as req_book, r.position as req_position, r.error as req_error,
               r.image_id is not null as requested
          from collections c
          left join collection_leaves l on l.collection_id = c.id and l.image_id = %(id)s
          left join collection_requests r on r.collection_id = c.id and r.image_id = %(id)s
         order by c.id
        """,
        {"id": image_id},
    )
    out = []
    for r in await cur.fetchall():
        out.append(
            {
                "slug": r["slug"],
                "title": r["title"],
                "abbrev_latin": r["abbrev_latin"],
                "book_count": r["book_count"],
                "leaves_per_book": r["leaves_per_book"],
                "leaf": None
                if r["book"] is None
                else {
                    "book": r["book"],
                    "position": r["position"],
                    "pinned": r["pinned"],
                    "book_pinned": r["book_pinned"],
                },
                "request": None
                if not r["requested"]
                else {
                    "book": r["req_book"],
                    "position": r["req_position"],
                    "error": r["req_error"],
                },
            }
        )
    return out
