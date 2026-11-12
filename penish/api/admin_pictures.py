"""Admin pictures: list and search, detail, edits, regen, crop, delete, the clean original."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from penish.api import embed_client
from penish.api.admin import _gate, require_admin
from penish.api.media import variants_for, variants_for_many
from penish.config import get_settings
from penish.worker.admin_jobs import (
    ORIG_TYPES,
    find_original,
    normalize_fields,
    original_dims,
    regen_key,
    remove_pub,
)
from penish.worker.derive import clamp_rect
from penish.worker.ingest import EMBED_FIELD
from penish.worker.jobs import enqueue
from penish.worker.metrics import refresh_corpus_stats

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/pictures",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

KIND = "description"
SLUG = re.compile(r"^[a-z0-9-]{1,80}$")
Status = Literal["queued", "processing", "pending_enrichment", "live", "failed"]
Sort = Literal["new", "old", "votes", "lonely"]
_LONELY_K = 5


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


def _pool(request: Request) -> Any:
    return request.app.state.pool


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


# -- list -------------------------------------------------------------------

_SELECT = """
    select i.id, i.slug, i.status, i.pub_sha, i.width, i.height, i.created_at,
           (select t.body from image_texts t
             where t.image_id = i.id and t.field = 'caption') as caption,
           coalesce((select sum(v.value) from votes v where v.image_id = i.id), 0)::int as net
"""

# Mean raw distance to the k nearest live images, as the public loneliness metric.
_LONELY = f"""
    , (select avg(d) from (
         select e2.vec <=> e.vec as d
           from embeddings e2 join images i2 on i2.id = e2.image_id
          where e2.kind = e.kind and e2.model = e.model and e2.dim = e.dim
            and i2.status = 'live' and e2.image_id <> e.image_id
          order by d
          limit {_LONELY_K}) nn) as lonely
"""

_ORDER = {
    "new": "i.created_at desc, i.id desc",
    "old": "i.created_at asc, i.id asc",
    "votes": "net desc, i.created_at desc, i.id desc",
    "lonely": "lonely desc nulls last, i.id desc",
}


def _offset(cursor: str | None) -> int:
    # Offsets, not keysets: votes, loneliness and search rank all move under a cursor
    # anyway, and at a few thousand pictures the scan is free.
    if cursor is None or cursor == "":
        return 0
    if not cursor.isdigit() or len(cursor) > 9:
        raise HTTPException(status_code=400, detail="bad cursor")
    return int(cursor)


def _thumb(vs: list[dict[str, Any]]) -> str | None:
    webps = [v["webp"] for v in vs if v["webp"]]
    return webps[0] if webps else None


@router.get("")
async def list_pictures(
    request: Request,
    q: str = "",
    status: Status | Literal[""] = "",
    sort: Sort = "new",
    cursor: str | None = None,
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    offset = _offset(cursor)
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset, "kind": KIND}
    where = ""
    if status:
        where = "where i.status = %(status)s"
        params["status"] = status

    q = q.strip()
    if q:
        try:
            emb = await embed_client.embed_query_full(q)
        except embed_client.EmbedUnavailable:
            raise HTTPException(status_code=503, detail="search unavailable") from None
        vec = emb["vector"]
        params |= {
            "qvec": "[" + ",".join(repr(float(x)) for x in vec) + "]",
            "qmodel": emb.get("model") or get_settings().embed_model,
            "qdim": len(vec),
        }
        # Filtering on dim as well keeps a mislabelled row from making pgvector raise.
        sql = f"""
            {_SELECT}, e.vec <=> %(qvec)s::vector as dist
            from images i
            join embeddings e on e.image_id = i.id and e.kind = %(kind)s
                             and e.model = %(qmodel)s and e.dim = %(qdim)s
            {where}
            order by dist asc, i.id desc
            limit %(limit)s offset %(offset)s
        """
    else:
        extra, join = "", ""
        if sort == "lonely":
            extra = _LONELY
            join = (
                "left join embeddings e on e.image_id = i.id and e.kind = %(kind)s"
                " and e.model = %(model)s"
            )
            params["model"] = get_settings().embed_model
        sql = f"""
            {_SELECT} {extra}
            from images i {join}
            {where}
            order by {_ORDER[sort]}
            limit %(limit)s offset %(offset)s
        """

    async with _pool(request).connection() as conn:
        cur = await conn.execute(sql.encode(), params)
        rows = await cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    variants = variants_for_many([(r["pub_sha"], r["width"], r["height"]) for r in rows])
    items = [
        {
            "id": r["id"],
            "slug": r["slug"],
            "status": r["status"],
            "thumb": _thumb(variants[r["pub_sha"]]),
            "caption": r["caption"],
            "net": r["net"],
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    ]
    return {"items": items, "next_cursor": str(offset + limit) if more else None}


# -- detail -----------------------------------------------------------------


async def _detail(request: Request, image_id: int) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute("select * from images where id = %s", (image_id,))
        img = await cur.fetchone()
        if img is None:
            raise _not_here()
        cur = await conn.execute(
            "select field, body, source, model from image_texts where image_id = %s", (image_id,)
        )
        texts = {
            r["field"]: {"body": r["body"], "source": r["source"], "model": r["model"]}
            for r in await cur.fetchall()
        }
        cur = await conn.execute(
            "select tag, source, model from tags where image_id = %s order by tag", (image_id,)
        )
        tags = list(await cur.fetchall())
        cur = await conn.execute(
            "select slug from slug_aliases where image_id = %s order by slug", (image_id,)
        )
        aliases = [r["slug"] for r in await cur.fetchall()]
        cur = await conn.execute(
            """
            select count(*) filter (where value = 1)::int as up,
                   count(*) filter (where value = -1)::int as down
              from votes where image_id = %s
            """,
            (image_id,),
        )
        votes = await cur.fetchone()
        assert votes is not None
        cur = await conn.execute(
            """
            select count(*) filter (where status = 'pending')::int as pending,
                   count(*) filter (where status = 'approved')::int as approved,
                   count(*) filter (where status = 'rejected')::int as rejected
              from comments where image_id = %s
            """,
            (image_id,),
        )
        comments = await cur.fetchone()
        # Admin jobs carry image_id; ingest jobs only know their dir and stem.
        cur = await conn.execute(
            """
            select id, type, status, attempts, last_error, created_at, updated_at
              from jobs where payload->>'image_id' = %s
             order by id desc limit 5
            """,
            (str(image_id),),
        )
        jobs = [
            {**r, "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}
            for r in await cur.fetchall()
        ]

    original = None
    orig = find_original(get_settings().media_orig, img["sha256"])
    if orig is not None:
        dims = await asyncio.to_thread(original_dims, orig)
        if dims is not None:
            original = {"width": dims[0], "height": dims[1]}

    return {
        "id": img["id"],
        "slug": img["slug"],
        "status": img["status"],
        "sha256": img["sha256"],
        "pub_sha": img["pub_sha"],
        "stem": img["stem"],
        "width": img["width"],
        "height": img["height"],
        "blurhash": img["blurhash"],
        "palette": img["palette"],
        "crop": img["crop"],
        "original": original,
        "variants": variants_for(img["pub_sha"], img["width"], img["height"]),
        "caption": texts.get("caption"),
        "description": texts.get("description"),
        "analysis": texts.get("analysis"),
        "prompt": texts.get("prompt"),
        "tags": tags,
        "aliases": aliases,
        "votes": {"up": votes["up"], "down": votes["down"], "net": votes["up"] - votes["down"]},
        "comments": comments,
        "jobs": jobs,
        "created_at": _iso(img["created_at"]),
        "published_at": _iso(img["published_at"]),
    }


@router.get("/{image_id}")
async def picture(request: Request, image_id: int) -> dict[str, Any]:
    return await _detail(request, image_id)


# -- edit -------------------------------------------------------------------


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=20000)
    analysis: str | None = Field(None, max_length=20000)
    prompt: str | None = Field(None, max_length=20000)
    tags: list[str] | None = Field(None, max_length=50)
    slug: str | None = None


_OWNER_TEXT = """
    insert into image_texts (image_id, field, body, source, model, updated_at)
    values (%s, %s, %s, 'owner', null, now())
    on conflict (image_id, field) do update set
      body = excluded.body, source = 'owner', model = null, updated_at = now()
"""


def _clean_tags(raw: list[str]) -> list[str]:
    # Same normalization as a tags sidecar.
    tags = list(dict.fromkeys(t.strip().lower() for t in raw if t.strip()))
    if any(len(t) > 60 for t in tags):
        raise HTTPException(status_code=422, detail="tag too long")
    return tags


async def _set_slug(conn: Any, img: dict[str, Any], slug: str) -> None:
    if not SLUG.fullmatch(slug):
        raise HTTPException(status_code=422, detail="bad slug")
    if slug == img["slug"]:
        return
    if img["status"] != "live" or img["slug"] is None:
        # Publishing assigns a slug from the caption and would overwrite this one.
        raise HTTPException(status_code=409, detail="not live")
    cur = await conn.execute(
        """
        select exists (select 1 from images where slug = %(s)s and id <> %(id)s)
            or exists (select 1 from slug_aliases where slug = %(s)s and image_id <> %(id)s)
            as taken
        """,
        {"s": slug, "id": img["id"]},
    )
    row = await cur.fetchone()
    if row and row["taken"]:
        raise HTTPException(status_code=409, detail="taken")
    await conn.execute(
        "delete from slug_aliases where slug = %s and image_id = %s", (slug, img["id"])
    )
    await conn.execute(
        "insert into slug_aliases (slug, image_id) values (%s, %s) on conflict (slug) do nothing",
        (img["slug"], img["id"]),
    )
    await conn.execute(
        "update images set slug = %s, updated_at = now() where id = %s", (slug, img["id"])
    )


@router.patch("/{image_id}")
async def edit_picture(request: Request, image_id: int, body: Edit) -> dict[str, Any]:
    given = body.model_fields_set
    try:
        async with _pool(request).connection() as conn, conn.transaction():
            cur = await conn.execute(
                "select id, slug, status from images where id = %s for update", (image_id,)
            )
            img = await cur.fetchone()
            if img is None:
                raise _not_here()
            # Only an explicit slug edit renames. Unlike an owner caption sidecar at ingest,
            # a caption typed here leaves the slug alone, so fixing a typo never moves a URL.
            if "slug" in given and body.slug is not None:
                await _set_slug(conn, img, body.slug)

            cur = await conn.execute(
                "select field, body from image_texts where image_id = %s", (image_id,)
            )
            prev = {r["field"]: r["body"] for r in await cur.fetchall()}
            for field in ("caption", "description", "analysis", "prompt"):
                if field not in given:
                    continue
                text = (getattr(body, field) or "").strip()
                if not text:
                    if field != "prompt":
                        raise HTTPException(status_code=422, detail=f"empty {field}")
                    await conn.execute(
                        "delete from image_texts where image_id = %s and field = 'prompt'",
                        (image_id,),
                    )
                    continue
                await conn.execute(_OWNER_TEXT, (image_id, field, text))
                # Only the analysis feeds the vector; a description edit leaves it alone.
                if field == EMBED_FIELD and prev.get(field) != text:
                    # Dropped with the text, as ingest does, so no vector outlives its words;
                    # the job rides the same transaction so a crash cannot lose it.
                    await conn.execute(
                        "delete from embeddings where image_id = %s and kind = %s",
                        (image_id, KIND),
                    )
                    await conn.execute(
                        "insert into jobs (type, payload, dedupe_key) values ('regen', %s, %s)"
                        " on conflict do nothing",
                        (
                            Jsonb({"image_id": image_id, "fields": ["embedding"]}),
                            regen_key(image_id, ["embedding"]),
                        ),
                    )

            if "tags" in given and body.tags is not None:
                tags = _clean_tags(body.tags)
                await conn.execute("delete from tags where image_id = %s", (image_id,))
                await conn.execute(
                    "insert into tags (image_id, tag, source, model)"
                    " select %s, t, 'owner', null from unnest(%s::text[]) t",
                    (image_id, tags),
                )
    except psycopg.errors.UniqueViolation:
        # Another rename claimed the slug between the check and the write.
        raise HTTPException(status_code=409, detail="taken") from None
    return await _detail(request, image_id)


# -- jobs -------------------------------------------------------------------


async def _require(request: Request, image_id: int) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select id, sha256, pub_sha, status, crop from images where id = %s", (image_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()
    if row is None:
        raise _not_here()
    return row


class RegenBody(BaseModel):
    fields: list[str] = Field(min_length=1, max_length=6)


@router.post("/{image_id}/regen")
async def regen_picture(request: Request, image_id: int, body: RegenBody) -> dict[str, Any]:
    try:
        fields = normalize_fields(body.fields)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown field") from None
    await _require(request, image_id)
    job_id = await enqueue(
        _pool(request),
        "regen",
        {"image_id": image_id, "fields": fields, "force_owner": True},
        dedupe_key=regen_key(image_id, fields, force_owner=True),
    )
    return {"job_id": job_id, "queued": job_id is not None}


class CropBody(BaseModel):
    rect: dict[str, Any] | None


@router.post("/{image_id}/crop")
async def crop_picture(request: Request, image_id: int, body: CropBody) -> dict[str, Any]:
    row = await _require(request, image_id)
    rect = None
    if body.rect is not None:
        orig = find_original(get_settings().media_orig, row["sha256"])
        dims = await asyncio.to_thread(original_dims, orig) if orig else None
        if dims is None:
            raise HTTPException(status_code=409, detail="no original")
        try:
            # Checked here so a bad rect is a 422 now, not a failed job later. A rect
            # covering the whole frame comes back None and is an uncrop.
            rect = clamp_rect(body.rect, *dims)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select exists (select 1 from jobs where type = 'crop'"
            " and status in ('queued', 'running') and payload->>'image_id' = %s) as p",
            (str(image_id),),
        )
        pending = await cur.fetchone()
    if rect == row["crop"] and not (pending and pending["p"]):
        # Rebuilding the same framing would only rename the files and pay for a describe.
        return {"job_id": None, "queued": False}
    # No dedupe key: a later crop must not collapse into an earlier one. The job skips
    # itself when a newer crop of the same image exists.
    job_id = await enqueue(_pool(request), "crop", {"image_id": image_id, "rect": rect})
    return {"job_id": job_id, "queued": job_id is not None}


# -- delete -----------------------------------------------------------------


async def _delete(pool: Any, image_id: int) -> tuple[str, list[str]] | None:
    """Remove one picture and its files. Its status and embedding models, or None if gone.

    Corpus stats are left to the caller, so a bulk delete refreshes them once.
    """
    settings = get_settings()
    async with pool.connection() as conn:
        cur = await conn.execute("select sha256 from images where id = %s", (image_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    sha = row["sha256"]
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        # The ingest and crop lock: neither can be writing files for this sha while they
        # are removed, and a re-drop arriving now waits until the removal is done.
        await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (sha,))
        try:
            async with conn.transaction():
                cur = await conn.execute(
                    "select distinct model from embeddings where image_id = %s and kind = %s",
                    (image_id, KIND),
                )
                models = [r["model"] for r in await cur.fetchall()]
                # Cascades to texts, tags, embeddings, aliases, votes and comments. Haikus
                # are set null and stay in the deck.
                cur = await conn.execute(
                    "delete from images where id = %s returning pub_sha, status", (image_id,)
                )
                gone = await cur.fetchone()
                if gone is None:
                    return None
                cur = await conn.execute(
                    "select exists (select 1 from images where pub_sha = %s) as shared",
                    (gone["pub_sha"],),
                )
                shared = await cur.fetchone()

            def unlink() -> None:
                for ext in ORIG_TYPES:
                    (settings.media_orig / f"{sha}{ext}").unlink(missing_ok=True)
                if not (shared and shared["shared"]):
                    remove_pub(settings.media_pub, gone["pub_sha"])

            # The rows are already gone; a stuck file must not hide that from the caller.
            try:
                await asyncio.to_thread(unlink)
            except OSError as e:
                log.warning("delete: image %s files left behind: %s", image_id, e)
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (sha,))
                await conn.set_autocommit(False)
    return gone["status"], models


async def _refresh(pool: Any, models: set[str]) -> None:
    for model in models:
        await refresh_corpus_stats(pool, kind=KIND, model=model)


@router.delete("/{image_id}")
async def delete_picture(request: Request, image_id: int) -> dict[str, bool]:
    pool = _pool(request)
    gone = await _delete(pool, image_id)
    if gone is None:
        raise _not_here()
    if gone[0] == "live":
        await _refresh(pool, set(gone[1]))
    return {"ok": True}


# -- bulk -------------------------------------------------------------------

# What each bulk action regenerates. Redescribe is the detail page's "regenerate all".
BULK_REGEN: dict[str, list[str]] = {
    "redescribe": ["caption", "description", "analysis", "tags"],
    "retag": ["tags"],
    "rehaiku": ["haiku"],
    "reembed": ["embedding"],
}
BULK_MAX = 500


class BulkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["delete", "redescribe", "retag", "rehaiku", "reembed"]
    ids: list[int] = Field(min_length=1, max_length=BULK_MAX)


@router.post("/bulk")
async def bulk(request: Request, body: BulkBody) -> dict[str, Any]:
    pool = _pool(request)
    ids = list(dict.fromkeys(body.ids))
    if body.action == "delete":
        done: list[int] = []
        live_models: set[str] = set()
        try:
            for image_id in ids:
                gone = await _delete(pool, image_id)
                if gone is None:
                    continue
                done.append(image_id)
                if gone[0] == "live":
                    live_models.update(gone[1])
        finally:
            # Once for the batch, since each refresh rescans every vector; and also when
            # a later delete fails, because the earlier ones are committed.
            await _refresh(pool, live_models)
        return {"action": "delete", "done": done}
    fields = BULK_REGEN[body.action]
    async with pool.connection() as conn:
        cur = await conn.execute("select id from images where id = any(%s)", (ids,))
        found = {r["id"] for r in await cur.fetchall()}
    queued: list[int] = []
    for image_id in ids:
        if image_id not in found:
            continue
        # Unlike the per-picture button, a batch leaves owner-written text alone: one
        # click should not undo edits across a hundred pictures.
        job_id = await enqueue(
            pool,
            "regen",
            {"image_id": image_id, "fields": fields},
            dedupe_key=regen_key(image_id, fields),
        )
        if job_id is not None:
            queued.append(image_id)
    return {"action": body.action, "done": [i for i in ids if i in found], "queued": queued}


# -- original ---------------------------------------------------------------


@router.get("/{image_id}/original")
async def original(request: Request, image_id: int) -> FileResponse:
    row = await _require(request, image_id)
    path = find_original(get_settings().media_orig, row["sha256"])
    if path is None:
        raise _not_here()
    return FileResponse(
        path, media_type=ORIG_TYPES[path.suffix], headers={"Cache-Control": "no-store"}
    )
