"""Admin collections: the collection's own text and switches, the books' anchors and hints,
every leaf by position, owner moves and pins, unfiled requests, re-sorts, re-judging and
history.

Every write answers with the whole collection, so the page just swaps it in.

The loose leaves (book 0) come after the books as one more, with a title and leaves but no
anchor, hints or anchor state: the filer never scores them.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from penish import runtime, uploads
from penish.api.admin import _gate, require_admin
from penish.api.media import variants_for_many
from penish.collection import store
from penish.collection.judge import Judgement
from penish.collection.numerals import LOOSE, book_name
from penish.collection.plan import LIMITS, Tuning, tag_words
from penish.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/collections",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

_HISTORY = 20
# Filings looked back over for judge failures: enough to see a broken model, few enough
# that one fixed an hour ago stops showing soon after.
_JUDGED = 20


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


def _anchor_state(anchored: bool, waiting: bool) -> str:
    # A vector under another model is no vector to the filer, so it waits like a missing one.
    if anchored:
        return "embedded"
    return "queued" if waiting else "stale"


def _hint_leaves(hints: list[str], leaf_tags: list[list[str]]) -> dict[str, int]:
    """How many leaves each hint meets, matched as plan.tag_overlap matches them."""
    words = [tag_words(t) for t in leaf_tags]
    return {h: sum(1 for w in words if h.lower().strip() in w) for h in hints}


async def _history(conn: Any, collection_id: int, leaf: str | None) -> list[dict[str, Any]]:
    # A leaf is asked for by image id or by slug; an unknown one has no history.
    where = ""
    params: dict[str, Any] = {"c": collection_id, "n": _HISTORY}
    if leaf is not None and leaf.strip():
        leaf = leaf.strip()
        where = "and (h.image_id = %(id)s or i.slug = %(slug)s)"
        # ASCII only: `²` and Arabic-Indic digits are digits to str but not to int(). Nine
        # digits at most, so a long run is asked for as a slug and cannot overflow the id.
        is_id = leaf.isascii() and leaf.isdigit() and len(leaf) <= 9
        params.update(id=int(leaf) if is_id else None, slug=leaf)
    cur = await conn.execute(
        f"""
        select h.id, h.image_id, i.slug, h.from_book, h.from_position, h.to_book,
               h.to_position, h.reason, h.chosen_by, h.at
          from collection_leaf_history h join images i on i.id = h.image_id
         where h.collection_id = %(c)s {where} order by h.id desc limit %(n)s
        """.encode(),
        params,
    )
    return [{**h, "at": _iso(h["at"])} for h in await cur.fetchall()]


async def _judge_failures(conn: Any, collection_id: int) -> dict[str, int]:
    """Failed judgements among the latest filings, by kind. `off` is the owner's switch,
    not a failure."""
    cur = await conn.execute(
        """
        select e as error, count(*)::int as n
          from (select h.scores->'judge'->>'error' as e from collection_leaf_history h
                 where h.collection_id = %s and h.from_book is null and h.to_book is not null
                 order by h.id desc limit %s) f
         where e is not null and e <> 'off'
         group by e order by n desc, e
        """,
        (collection_id, _JUDGED),
    )
    return {r["error"]: r["n"] for r in await cur.fetchall()}


async def detail(conn: Any, c: dict[str, Any], leaf: str | None = None) -> dict[str, Any]:
    """The whole collection for the admin page. `leaf` (an image id or slug) narrows the
    history to one leaf."""
    settings = get_settings()
    cur = await conn.execute(
        """
        select number, title, anchor_text, tag_hints, anchor_embedded_at,
               anchor_embedding is not null and anchor_model = %s and anchor_dim = %s as anchored
          from collection_books where collection_id = %s order by number = %s, number
        """,
        (settings.embed_model, settings.embed_dim, c["id"], LOOSE),
    )
    books = list(await cur.fetchall())
    loose = next((b for b in books if b["number"] == LOOSE), None)
    cur = await conn.execute(
        "select exists (select 1 from jobs where type = %s and status in ('queued', 'running')"
        " and payload->>'collection_id' = %s) as waiting",
        (store.ANCHOR_JOB, str(c["id"])),
    )
    anchor_waiting = bool((await cur.fetchone() or {"waiting": False})["waiting"])
    cur = await conn.execute(
        f"""
        select {_PICTURE}, l.book, l.position, l.pinned, l.book_pinned, l.note, l.note_source,
               coalesce((select array_agg(t.tag) from tags t where t.image_id = l.image_id),
                        '{{}}') as tags
          from collection_leaves l join images i on i.id = l.image_id
         where l.collection_id = %s order by l.book, l.position
        """.encode(),
        (c["id"],),
    )
    leaves = list(await cur.fetchall())
    leaf_tags = [list(r["tags"]) for r in leaves]
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
    history = await _history(conn, c["id"], leaf)
    judge_failures = await _judge_failures(conn, c["id"])
    cur = await conn.execute(
        """
        select status::text as status, last_error from jobs
         where type = %s and payload->>'collection_id' = %s order by id desc limit 1
        """,
        (store.RESORT_JOB, str(c["id"])),
    )
    resort = await cur.fetchone()

    cur = await conn.execute(
        "select payload->>'image_id' as image_id from jobs where type = %s"
        " and status in ('queued', 'running') and payload->>'collection_id' = %s",
        (store.NOTE_JOB, str(c["id"])),
    )
    waiting = list(await cur.fetchall())
    noting = len(waiting)
    # By leaf, so the list can say which note is on its way; a leaf can hold two jobs (a
    # fill and a rewrite), which `notes_queued` counts twice and this counts once.
    noting_leaves = {r["image_id"] for r in waiting}

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
                "note": r["note"],
                "note_source": r["note_source"],
                "note_queued": str(r["id"]) in noting_leaves,
            }
        )
    # A live picture waiting to be filed is already listed under requests, with its retry.
    requested = {r["id"] for r in requests}
    arriving = [
        u
        for u in await uploads.stages(conn, settings, collection=c["slug"], only_unfiled=True)
        if u["image_id"] is None or u["image_id"] not in requested
    ]
    return {
        "id": c["id"],
        "slug": c["slug"],
        "title": c["title"],
        "abbrev": c["abbrev"],
        "abbrev_latin": c["abbrev_latin"],
        "preamble": c["preamble"],
        "gloss": c["gloss"],
        "record": [list(p) for p in store.record_pairs(c["record"])],
        "vendor_text": c["vendor_text"],
        "notes_queued": noting,
        "book_count": c["book_count"],
        "leaves_per_book": c["leaves_per_book"],
        # The loose leaves' title, what a picker offers beside the numerals; null with no row.
        "loose_title": None if loose is None else book_name(LOOSE, loose["title"]),
        "published": c["published"],
        "tuning": Tuning.from_json(c["tuning"]).to_json(),
        "tuning_default": Tuning().to_json(),
        "tuning_limits": {k: list(v) for k, v in LIMITS.items()},
        "last_resort_at": _iso(c["last_resort_at"]),
        "resort": resort,
        "books": [
            {
                "number": b["number"],
                "title": b["title"],
                "anchor_text": b["anchor_text"],
                "tag_hints": b["tag_hints"],
                "anchored": bool(b["anchored"]),
                # Null for the loose leaves, which have no anchor to embed.
                "anchor_state": None
                if b["number"] == LOOSE
                else _anchor_state(bool(b["anchored"]), anchor_waiting),
                # Null with a vector: embedded before the time was kept.
                "anchor_embedded_at": _iso(b["anchor_embedded_at"]),
                "hint_leaves": _hint_leaves(list(b["tag_hints"] or ()), leaf_tags),
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
        "uploads": [
            {
                "stem": u["stem"],
                "sha256": u["sha256"],
                "filename": u["filename"],
                "stage": u["stage"],
                "error": u["error"],
                "slug": u["slug"],
                "image_id": u["image_id"],
                "created_at": _iso(u["created_at"]),
            }
            for u in arriving
        ],
        "history": history,
        "judge_failures": judge_failures,
    }


async def _answer(request: Request, slug: str, leaf: str | None = None) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        return await detail(conn, await _collection(conn, slug), leaf)


# -- reading ---------------------------------------------------------------------


@router.get("")
async def list_collections(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            """
            select c.slug, c.title, c.abbrev, c.abbrev_latin, c.book_count, c.leaves_per_book,
                   c.published, b.number is not null as loose, b.title as loose_title
              from collections c
              left join collection_books b on b.collection_id = c.id and b.number = %s
             order by c.id
            """,
            (LOOSE,),
        )
        rows = await cur.fetchall()
    return {
        "collections": [
            {
                **{k: v for k, v in r.items() if k not in ("loose", "loose_title")},
                "loose_title": book_name(LOOSE, r["loose_title"]) if r["loose"] else None,
            }
            for r in rows
        ]
    }


@router.get("/{slug}")
async def get_collection(request: Request, slug: str, leaf: str | None = None) -> dict[str, Any]:
    return await _answer(request, slug, leaf)


# -- the collection and its books ---------------------------------------------------


class CollectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=200)
    abbrev: str | None = Field(None, min_length=1, max_length=20)
    abbrev_latin: str | None = Field(None, min_length=1, max_length=20)
    preamble: str | None = Field(None, max_length=20000)
    gloss: str | None = Field(None, max_length=2000)
    # Ordered [label, value] pairs; a value may hold several lines.
    record: list[tuple[str, str]] | None = Field(None, max_length=60)
    # Empty or null: no vendor's description is shown.
    vendor_text: str | None = Field(None, max_length=20000)
    published: bool | None = None
    tuning: dict[str, float | bool] | None = None


@router.patch("/{slug}")
async def edit_collection(request: Request, slug: str, body: CollectionEdit) -> dict[str, Any]:
    given = body.model_fields_set
    sets: list[str] = []
    params: list[Any] = []
    for f in ("title", "abbrev", "abbrev_latin", "preamble", "gloss", "published"):
        v = getattr(body, f)
        if f in given and v is not None:
            sets.append(f"{f} = %s")
            params.append(v.strip() if isinstance(v, str) else v)
    if "vendor_text" in given:
        sets.append("vendor_text = %s")
        params.append((body.vendor_text or "").strip() or None)
    if "record" in given and body.record is not None:
        rows: list[list[str]] = []
        for label, value in body.record:
            label = label.strip()
            # Each line trimmed, blank lines dropped: the page shows one line per line.
            value = "\n".join(x.strip() for x in value.splitlines() if x.strip())
            if not label and not value:
                continue
            if not label or len(label) > 100 or len(value) > 4000:
                raise HTTPException(status_code=422, detail="bad record")
            rows.append([label, value])
        sets.append("record = %s")
        params.append(Jsonb(rows))
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
        if number == LOOSE:
            # A title and nothing else: the filer never scores the loose leaves, and with no
            # numeral the title is all that names them.
            if (body.anchor_text or "").strip() or body.tag_hints:
                raise HTTPException(status_code=422, detail="no anchor here")
            if "title" in given and not (body.title or "").strip():
                raise HTTPException(status_code=422, detail="a title is needed")
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
                    " anchor_model = null, anchor_dim = null, anchor_embedded_at = null,"
                    " updated_at = now()"
                    " where collection_id = %s and number = %s",
                    (text, c["id"], number),
                )
                await store.enqueue_anchor_job(conn, c["id"])
    return await _answer(request, slug)


# -- leaves -----------------------------------------------------------------------


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 0 is the loose leaves.
    book: int | None = Field(None, ge=0)
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


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null or empty clears the note, and the leaf is the conservator's again.
    note: str | None = Field(None, max_length=store.NOTE_MAX)


@router.put("/{slug}/leaves/{image_id}/note")
async def put_note(request: Request, slug: str, image_id: int, body: NoteBody) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        if not await store.set_note(conn, c["id"], image_id, body.note):
            raise _not_here()
    return await _answer(request, slug)


@router.post("/{slug}/leaves/{image_id}/note")
async def write_note(request: Request, slug: str, image_id: int) -> dict[str, Any]:
    """Ask the conservator for this leaf. An owner note is left alone; a model note is
    written again."""
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        cur = await conn.execute(
            "select note_source from collection_leaves where image_id = %s and collection_id = %s",
            (image_id, c["id"]),
        )
        row = await cur.fetchone()
        if row is None:
            raise _not_here()
        if row["note_source"] == "owner":
            raise HTTPException(status_code=409, detail="the note is the owner's")
        await store.enqueue_note(conn, image_id, c["id"], rewrite=True)
    return await _answer(request, slug)


@router.post("/{slug}/notes")
async def write_missing_notes(request: Request, slug: str) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        n = await store.enqueue_missing_notes(conn, c["id"])
    log.info("collection %s: %d notes queued", slug, n)
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


class PlannedMove(BaseModel):
    # The dry run's moves come back as they were sent; their other fields are for display.
    image_id: int
    before: tuple[int, int]
    after: tuple[int, int]


class Resort(BaseModel):
    dry_run: bool = True
    # The dry run's answer, sent back to apply it.
    plan_id: str | None = Field(None, max_length=128)
    moves: list[PlannedMove] | None = Field(None, max_length=100_000)


@router.post("/{slug}/resort")
async def resort(request: Request, slug: str, body: Resort) -> dict[str, Any]:
    """A dry run answers with the plan and its id; sending both back applies exactly that
    plan, or refuses `changed` if the collection has moved on since. A dry run while an
    anchor waits for its vector is refused `anchors`, and the anchor is queued, so the
    next one a minute later can score every book."""
    settings = get_settings()
    async with request.app.state.pool.connection() as conn:
        c = await _collection(conn, slug)
        if body.dry_run:
            try:
                plan = await store.plan(conn, c, settings)
            except store.AnchorsWaiting as e:
                await store.enqueue_anchor_job(conn, c["id"])
                # Committed here: the refusal below would otherwise roll the job back.
                await conn.commit()
                raise _refused(e) from None
            except store.Refused as e:
                raise _refused(e) from None
            cur = await conn.execute(
                f"select {_PICTURE} from images i where i.id = any(%s)".encode(),
                ([m.image_id for m in plan.moves],),
            )
            pictures = {r["id"]: r for r in await cur.fetchall()}
            thumbs = _thumbs(list(pictures.values()))
            return {
                "plan_id": plan.id,
                "moves": [
                    {
                        "image_id": m.image_id,
                        "slug": pictures[m.image_id]["slug"] if m.image_id in pictures else None,
                        "thumb": thumbs.get(m.image_id),
                        "before": m.before,
                        "after": m.after,
                        "margin": plan.leads.get(m.image_id),
                    }
                    for m in plan.moves
                ],
            }
        if body.plan_id is None or body.moves is None:
            raise HTTPException(status_code=422, detail="no plan")
        try:
            await store.apply(
                conn,
                c,
                settings,
                body.plan_id,
                [(m.image_id, m.before, m.after) for m in body.moves],
            )
        except store.Refused as e:
            raise _refused(e) from None
    return await _answer(request, slug)


async def _pictures(conn: Any, image_ids: list[int]) -> tuple[dict[int, Any], dict[int, Any]]:
    cur = await conn.execute(
        f"select {_PICTURE} from images i where i.id = any(%s)".encode(), (image_ids,)
    )
    pictures = {r["id"]: r for r in await cur.fetchall()}
    return pictures, _thumbs(list(pictures.values()))


@router.post("/{slug}/judge")
async def judge(request: Request, slug: str, body: Resort) -> dict[str, Any]:
    """Every leaf the owner did not hold to a book, in the book the judge names for it. As
    the re-sort: a dry run answers the plan and its id, and both sent back apply exactly
    that plan or are refused `changed`. The judgements come from the worker, which holds
    the keys: a dry run with any leaf not yet judged is refused `judging` and the judging
    queued, so the same dry run a little later has them all. With filer.judge off it is
    refused `off` and nothing is queued."""
    settings = get_settings()
    pool = request.app.state.pool
    get = runtime.getter(pool, settings)
    async with pool.connection() as conn:
        c = await _collection(conn, slug)
        if body.dry_run:
            if not await get(runtime.JUDGE_KEY):
                # The dry run is what queues the paid asking, so the switch stops it here.
                raise HTTPException(status_code=409, detail="off")
            try:
                plan = await store.judge_plan(conn, c, settings, get)
            except (store.AnchorsWaiting, store.Judging) as e:
                if isinstance(e, store.Judging):
                    await store.enqueue_judge_job(conn, c["id"])
                else:
                    await store.enqueue_anchor_job(conn, c["id"])
                # Committed here: the refusal below would otherwise roll the job back.
                await conn.commit()
                raise _refused(e) from None
            except store.Refused as e:
                raise _refused(e) from None
            if plan.retry:
                # A failure that may not happen twice is shown as held, and asked again.
                await store.enqueue_judge_job(conn, c["id"])
            pictures, thumbs = await _pictures(
                conn, [m.image_id for m in plan.moves] + [h.image_id for h in plan.held]
            )

            def said(image_id: int, chosen_by: str | None) -> dict[str, Any]:
                v = plan.verdicts.get(image_id)
                if chosen_by != "judge" or not isinstance(v, Judgement):
                    return {"reason": None, "runner_up": None}
                return {"reason": v.reason, "runner_up": v.runner_up}

            def slug_of(image_id: int) -> str | None:
                return pictures[image_id]["slug"] if image_id in pictures else None

            return {
                "plan_id": plan.id,
                "moves": [
                    {
                        "image_id": m.image_id,
                        "slug": slug_of(m.image_id),
                        "thumb": thumbs.get(m.image_id),
                        "before": m.before,
                        "after": m.after,
                        "margin": plan.leads.get(m.image_id),
                        **said(m.image_id, m.chosen_by),
                    }
                    for m in plan.moves
                ],
                "held": [
                    {
                        "image_id": h.image_id,
                        "slug": slug_of(h.image_id),
                        "thumb": thumbs.get(h.image_id),
                        "before": h.before,
                        "book": h.book,
                        "why": h.why,
                    }
                    for h in plan.held
                ],
            }
        if body.plan_id is None or body.moves is None:
            raise HTTPException(status_code=422, detail="no plan")
        try:
            await store.apply_judged(
                conn,
                c,
                settings,
                get,
                body.plan_id,
                [(m.image_id, m.before, m.after) for m in body.moves],
            )
        except store.Refused as e:
            raise _refused(e) from None
    return await _answer(request, slug)


@router.get("/{slug}/explain")
async def explain(request: Request, slug: str, image_id: int) -> dict[str, Any]:
    """Why a leaf is in its book: every book's terms now, with the leaf left out of its own
    as a re-sort sees it, the scores kept when it was filed (with the judge's word when it
    decided), and the judgement kept for the leaf as it is now."""
    settings = get_settings()
    pool = request.app.state.pool
    async with pool.connection() as conn:
        c = await _collection(conn, slug)
        got = await store.explain(conn, c, image_id, settings, runtime.getter(pool, settings))
        if got is None:
            raise _not_here()
        cur = await conn.execute("select slug from images where id = %s", (image_id,))
        row = await cur.fetchone()
    return {**got, "slug": row["slug"] if row else None}


# -- the admin picture page -----------------------------------------------------------


async def for_picture(conn: Any, image_id: int) -> list[dict[str, Any]]:
    """Each collection with this picture's place in it, or its waiting request."""
    cur = await conn.execute(
        """
        select c.slug, c.title, c.abbrev_latin, c.book_count, c.leaves_per_book,
               lb.number is not null as loose, lb.title as loose_title,
               l.book, l.position, l.pinned, l.book_pinned, l.note, l.note_source, l.note_model,
               r.book as req_book, r.position as req_position, r.error as req_error,
               r.image_id is not null as requested
          from collections c
          left join collection_books lb on lb.collection_id = c.id and lb.number = %(loose)s
          left join collection_leaves l on l.collection_id = c.id and l.image_id = %(id)s
          left join collection_requests r on r.collection_id = c.id and r.image_id = %(id)s
         order by c.id
        """,
        {"id": image_id, "loose": LOOSE},
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
                "loose_title": book_name(LOOSE, r["loose_title"]) if r["loose"] else None,
                "leaf": None
                if r["book"] is None
                else {
                    "book": r["book"],
                    "position": r["position"],
                    "pinned": r["pinned"],
                    "book_pinned": r["book_pinned"],
                    "note": r["note"],
                    "note_source": r["note_source"],
                    "note_model": r["note_model"],
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
