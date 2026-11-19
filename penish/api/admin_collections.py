"""Admin for collections: the collection's own text, its books' anchors and hints, the
leaves (pin, move, unfile), requests waiting or failed, the re-sort, and the history.

Moves and re-sorts run here, in the request, through the same store the worker files
with: they need no model, only vectors already in the database. Filing a new picture is
the worker's, since an anchor may need embedding first.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from penish import runtime
from penish.api.admin import _gate, require_admin
from penish.api.admin_ops import _iso, _thumbs
from penish.collections import parse_number, store
from penish.config import get_settings
from penish.worker.jobs import enqueue

router = APIRouter(
    prefix="/api/admin/collections",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

HISTORY = 50
TUNING = (*runtime.COLLECTION_TUNABLES, "collections.weekly_resort")


def _pool(request: Request) -> Any:
    return request.app.state.pool


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


async def _collection(conn: Any, slug: str) -> store.Collection:
    c = await store.get(conn, slug=slug)
    if c is None:
        raise _not_here()
    return c


def _number(raw: int | str | None) -> int | None:
    """A book or leaf from a form: arabic or roman, blank for none."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return parse_number(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def _pos(p: tuple[int, int] | None) -> dict[str, int] | None:
    return None if p is None else {"book": p[0], "position": p[1]}


async def _pictures(conn: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    cur = await conn.execute(
        "select id, slug, status::text as status, pub_sha, width, height from images"
        " where id = any(%s)",
        (ids,),
    )
    rows = list(await cur.fetchall())
    thumbs = _thumbs(rows)
    return {
        int(r["id"]): {
            "image_id": int(r["id"]),
            "slug": r["slug"],
            "status": r["status"],
            "thumb": thumbs.get(r["pub_sha"]) if r["status"] == "live" else None,
        }
        for r in rows
    }


async def _moves(conn: Any, moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pics = await _pictures(conn, [m["image_id"] for m in moves])
    return [
        {
            **pics.get(m["image_id"], {"image_id": m["image_id"], "slug": None}),
            "from": _pos(m["from"]),
            "to": _pos(m["to"]),
            "reason": m["reason"],
        }
        for m in moves
    ]


async def _tuning(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    return {k: {"value": await runtime.get(pool, k), "default": runtime.default(k)} for k in TUNING}


async def _detail(conn: Any, c: store.Collection) -> dict[str, Any]:
    settings = get_settings()
    books = await store.books(conn, c)
    cur = await conn.execute(
        "select l.image_id, b.number, l.position, l.pinned_book, l.pinned_position, l.filed_at"
        " from collection_leaves l join collection_books b on b.id = l.book_id"
        " where l.collection_id = %s order by b.number, l.position",
        (c.id,),
    )
    leaves = list(await cur.fetchall())
    cur = await conn.execute(
        "select image_id, book, position, error, created_at from collection_requests"
        " where collection_id = %s order by created_at desc",
        (c.id,),
    )
    requests = list(await cur.fetchall())
    cur = await conn.execute(
        "select image_id, from_book, from_position, to_book, to_position, reason, at"
        " from collection_leaf_history where collection_id = %s order by at desc, id desc"
        " limit %s",
        (c.id, HISTORY),
    )
    history = list(await cur.fetchall())
    ids = {int(r["image_id"]) for r in (*leaves, *requests, *history)}
    pics = await _pictures(conn, sorted(ids))

    def pic(iid: int) -> dict[str, Any]:
        return pics.get(iid, {"image_id": iid, "slug": None, "status": None, "thumb": None})

    return {
        "slug": c.slug,
        "title": c.title,
        "abbrev": c.abbrev,
        "abbrev_latin": c.abbrev_latin,
        "preamble": c.preamble,
        "published": c.published,
        "book_count": c.book_count,
        "leaves_per_book": c.leaves_per_book,
        "resorted_at": _iso(c.resorted_at),
        "books": [
            {
                "number": b["number"],
                "title": b["title"],
                "anchor_text": b["anchor_text"],
                "tag_hints": list(b["tag_hints"] or []),
                # The filer can only use an anchor embedded under the current model.
                "anchored": b["anchor_vec"] is not None
                and b["anchor_model"] == settings.embed_model,
                "leaves": [
                    {
                        **pic(int(r["image_id"])),
                        "position": r["position"],
                        "pinned_book": r["pinned_book"],
                        "pinned_position": r["pinned_position"],
                    }
                    for r in leaves
                    if r["number"] == b["number"]
                ],
            }
            for b in books
        ],
        "requests": [
            {
                **pic(int(r["image_id"])),
                "book": r["book"],
                "position": r["position"],
                "error": r["error"],
                "created_at": _iso(r["created_at"]),
            }
            for r in requests
        ],
        "history": [
            {
                **pic(int(r["image_id"])),
                "from": None
                if r["from_book"] is None
                else {"book": r["from_book"], "position": r["from_position"]},
                "to": None
                if r["to_book"] is None
                else {"book": r["to_book"], "position": r["to_position"]},
                "reason": r["reason"],
                "at": _iso(r["at"]),
            }
            for r in history
        ],
    }


@router.get("")
async def list_collections(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cs = await store.all_collections(conn)
        out = [await _detail(conn, c) for c in cs]
    return {"collections": out, "tuning": await _tuning(request)}


@router.get("/picture/{image_id}")
async def for_picture(image_id: int, request: Request) -> dict[str, Any]:
    """Every collection, with this picture's leaf or request in each, for its editor."""
    async with _pool(request).connection() as conn:
        cs = await store.all_collections(conn)
        cur = await conn.execute(
            "select l.collection_id, b.number, l.position, l.pinned_book, l.pinned_position"
            " from collection_leaves l join collection_books b on b.id = l.book_id"
            " where l.image_id = %s",
            (image_id,),
        )
        leaves = {int(r["collection_id"]): r for r in await cur.fetchall()}
        cur = await conn.execute(
            "select collection_id, book, position, error from collection_requests"
            " where image_id = %s",
            (image_id,),
        )
        reqs = {int(r["collection_id"]): r for r in await cur.fetchall()}
    out = []
    for c in cs:
        lf, rq = leaves.get(c.id), reqs.get(c.id)
        out.append(
            {
                "slug": c.slug,
                "title": c.title,
                "abbrev_latin": c.abbrev_latin,
                "book_count": c.book_count,
                "leaves_per_book": c.leaves_per_book,
                "leaf": None
                if lf is None
                else {
                    "book": lf["number"],
                    "position": lf["position"],
                    "pinned_book": lf["pinned_book"],
                    "pinned_position": lf["pinned_position"],
                },
                "request": None
                if rq is None
                else {"book": rq["book"], "position": rq["position"], "error": rq["error"]},
            }
        )
    return {"collections": out}


class CollectionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    abbrev: str | None = Field(None, min_length=1, max_length=20)
    abbrev_latin: str | None = Field(None, min_length=1, max_length=20)
    preamble: str | None = Field(None, max_length=20_000)
    published: bool | None = None


@router.patch("/{slug}")
async def patch_collection(slug: str, body: CollectionPatch, request: Request) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    async with _pool(request).connection() as conn:
        c = await _collection(conn, slug)
        for k, v in fields.items():
            # k is one of the model's own field names, never request text.
            await conn.execute(
                f"update collections set {k} = %s, updated_at = now() where id = %s".encode(),
                (v.strip() if isinstance(v, str) else v, c.id),
            )
        c2 = await _collection(conn, slug)
        return await _detail(conn, c2)


class BookPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    anchor_text: str | None = Field(None, max_length=10_000)
    tag_hints: list[str] | None = Field(None, max_length=50)


@router.patch("/{slug}/books/{number}")
async def patch_book(slug: str, number: int, body: BookPatch, request: Request) -> dict[str, Any]:
    sent = body.model_fields_set
    async with _pool(request).connection() as conn:
        c = await _collection(conn, slug)
        cur = await conn.execute(
            "select id, anchor_text from collection_books where collection_id = %s and number = %s",
            (c.id, number),
        )
        b = await cur.fetchone()
        if b is None:
            raise _not_here()
        if "title" in sent:
            # Blank clears it: the page then shows the numeral alone.
            title = (body.title or "").strip() or None
            await conn.execute(
                "update collection_books set title = %s, updated_at = now() where id = %s",
                (title, b["id"]),
            )
        if body.tag_hints is not None:
            hints = list(dict.fromkeys(h.strip().lower() for h in body.tag_hints if h.strip()))
            await conn.execute(
                "update collection_books set tag_hints = %s, updated_at = now() where id = %s",
                (hints, b["id"]),
            )
        reembed = body.anchor_text is not None and body.anchor_text.strip() != b["anchor_text"]
        if reembed:
            assert body.anchor_text is not None
            # The old vector goes with the old text, so the filer never scores against a
            # description that is no longer there.
            await conn.execute(
                "update collection_books set anchor_text = %s, anchor_vec = null,"
                " anchor_model = null, updated_at = now() where id = %s",
                (body.anchor_text.strip(), b["id"]),
            )
        detail = await _detail(conn, c)
    if reembed:
        await enqueue(
            _pool(request),
            store.ANCHOR_JOB,
            {"collection_id": c.id},
            dedupe_key=f"{store.ANCHOR_JOB}:{c.id}",
        )
    return detail


class Placement(BaseModel):
    book: int | str | None = None
    position: int | str | None = None


async def _image(conn: Any, image_id: int) -> dict[str, Any]:
    cur = await conn.execute(
        "select id, status::text as status from images where id = %s", (image_id,)
    )
    row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        raise _not_here()
    return row


@router.put("/{slug}/pictures/{image_id}")
async def place(slug: str, image_id: int, body: Placement, request: Request) -> dict[str, Any]:
    """Put a picture in the collection. Not yet a leaf: a request the worker files once
    it is live. Already a leaf: with a book, the owner's move, done now; without one it
    stays where it is."""
    book, position = _number(body.book), _number(body.position)
    pool = _pool(request)
    async with pool.connection() as conn:
        c = await _collection(conn, slug)
        img = await _image(conn, image_id)
        try:
            store.check_numbers(c, book, position)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        cur = await conn.execute(
            "select 1 from collection_leaves where image_id = %s and collection_id = %s",
            (image_id, c.id),
        )
        filed = await cur.fetchone() is not None
        if filed:
            moves: list[dict[str, Any]] = []
            if book is not None:
                try:
                    async with conn.transaction():
                        moves = await store.move(conn, c, image_id, book, position, get_settings())
                except store.FilingError as e:
                    raise HTTPException(status_code=409, detail=str(e)) from None
            return {"state": "filed", "moves": await _moves(conn, moves)}
        await store.request(conn, c, image_id, book, position)
        if img["status"] == "live":
            await store.enqueue_for_image(conn, image_id)
    return {"state": "requested", "moves": []}


@router.delete("/{slug}/pictures/{image_id}")
async def remove(slug: str, image_id: int, request: Request) -> dict[str, bool]:
    async with _pool(request).connection() as conn:
        c = await _collection(conn, slug)
        async with conn.transaction():
            found = await store.unfile(conn, c, image_id)
    if not found:
        raise _not_here()
    return {"ok": True}


class Pin(BaseModel):
    pinned: bool


@router.post("/{slug}/pictures/{image_id}/pin")
async def pin(slug: str, image_id: int, body: Pin, request: Request) -> dict[str, bool]:
    async with _pool(request).connection() as conn:
        c = await _collection(conn, slug)
        async with conn.transaction():
            found = await store.pin(conn, c, image_id, body.pinned)
    if not found:
        raise _not_here()
    return {"ok": True}


@router.post("/{slug}/requests/{image_id}/retry")
async def retry(slug: str, image_id: int, request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        c = await _collection(conn, slug)
        cur = await conn.execute(
            "update collection_requests set error = null, updated_at = now()"
            " where image_id = %s and collection_id = %s",
            (image_id, c.id),
        )
        if cur.rowcount == 0:
            raise _not_here()
        n = await store.enqueue_for_image(conn, image_id)
    return {"queued": n > 0}


class Resort(BaseModel):
    dry_run: bool = True


@router.post("/{slug}/resort")
async def resort(slug: str, body: Resort, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    p = await store.load_params(pool)
    async with pool.connection() as conn:
        c = await _collection(conn, slug)
        async with conn.transaction():
            moves = await store.resort(conn, c, get_settings(), p, dry_run=body.dry_run)
        return {"dry_run": body.dry_run, "moves": await _moves(conn, moves)}
