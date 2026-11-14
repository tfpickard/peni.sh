"""Jobs the admin panel enqueues: per-image regen and crop, bulk re-embed and re-describe,
haiku growth and mycelium genesis.

Same contract as ingest: SoftError retries, HardError fails, and an image deleted while its
job waited is a quiet no-op rather than an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pyvips
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.mycelium import genesis
from penish.mycelium import store as mycelium
from penish.storage import LocalStorage
from penish.worker.derive import (
    Public,
    UndecodableImage,
    clamp_rect,
    load_and_validate,
    rebuild_public,
)
from penish.worker.describe import Described, DescribeError, describe
from penish.worker.grow import Stats, grow_into, summary
from penish.worker.haiku import haiku_from
from penish.worker.ingest import (
    EMBED_FIELD,
    KIND,
    Conn,
    Describer,
    DocumentEmbedder,
    HaikuWriter,
    _describe_bytes,
    _texts,
)
from penish.worker.jobs import HardError, Job, Pool, SoftError
from penish.worker.metrics import refresh_corpus_stats

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[None]]
HandlerFactory = Callable[[Pool, Settings, DocumentEmbedder], Handler]
Grower = Callable[..., Awaitable[Stats]]

REGEN_FIELDS = ("caption", "description", "analysis", "tags", "haiku", "embedding")
_TEXTS = ("caption", "description", "analysis")
_DESCRIBED = (*_TEXTS, "tags")
# What a crop invalidates: everything derived from the pixels or from the new analysis.
_AFTER_CROP = frozenset({*_DESCRIBED, "embedding"})
ORIG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
MAX_GROW = 200


# -- shared with the api ----------------------------------------------------


def normalize_fields(fields: Iterable[Any]) -> list[str]:
    """Regen fields in canonical order, so equal requests share one dedupe key."""
    got = set()
    for f in fields:
        if f not in REGEN_FIELDS:
            raise ValueError(f"unknown field {f!r}")
        got.add(f)
    if not got:
        raise ValueError("no fields")
    return [f for f in REGEN_FIELDS if f in got]


def regen_key(image_id: int, fields: Iterable[str], force_owner: bool = False) -> str:
    key = f"regen:{image_id}:{','.join(normalize_fields(fields))}"
    # Separate key space: an explicit regenerate must not collapse into a queued bulk job
    # that would leave the owner text alone.
    return key + ":owner" if force_owner else key


def find_original(orig_dir: Path, sha256: str) -> Path | None:
    # Fixed candidates instead of a glob: no pattern built from data, and the dotted
    # temp files storage writes mid-rename can never match.
    for ext in ORIG_TYPES:
        p = orig_dir / f"{sha256}{ext}"
        if p.is_file():
            return p
    return None


def original_dims(path: Path) -> tuple[int, int] | None:
    """Header-only read. The stored original was autorotated and stripped at ingest, so
    the header dims are the pixel dims crop rects are measured in."""
    try:
        img = pyvips.Image.new_from_file(str(path))
    except pyvips.Error:
        return None
    return int(img.width), int(img.height)


def remove_pub(pub_dir: Path, pub_sha: str) -> int:
    n = 0
    for p in pub_dir.glob(f"{pub_sha}-*"):
        p.unlink(missing_ok=True)
        n += 1
    return n


# -- helpers ----------------------------------------------------------------


async def _row(conn: Conn, image_id: int) -> dict[str, Any] | None:
    cur = await conn.execute("select * from images where id = %s", (image_id,))
    row: dict[str, Any] | None = await cur.fetchone()
    return row


def _describe_input(path: Path, crop: Mapping[str, Any] | None) -> bytes:
    img = load_and_validate(path.read_bytes()).image
    if crop:
        rect = clamp_rect(crop, img.width, img.height)
        if rect is not None:
            img = img.crop(rect["x"], rect["y"], rect["w"], rect["h"])
    return _describe_bytes(img)


def _soft_or_hard(prefix: str, e: DescribeError) -> Exception:
    return (SoftError if e.retryable else HardError)(f"{prefix}: {e}")


async def _describe(
    pool: Pool,
    row: dict[str, Any],
    want: set[str],
    settings: Settings,
    describer: Describer,
) -> Described:
    orig = find_original(settings.media_orig, row["sha256"])
    if orig is None:
        raise HardError(f"original for image {row['id']} is missing")
    try:
        # The displayed pixels, crop included: a description of what was cropped away
        # would be wrong.
        data = await asyncio.to_thread(_describe_input, orig, row["crop"])
    except (UndecodableImage, ValueError) as e:
        raise HardError(f"describe input: {e}") from e
    try:
        return await describer(data, "image/webp", want=want, settings=settings, pool=pool)
    except DescribeError as e:
        raise _soft_or_hard("describe", e) from e


_MODEL_TEXT = """
    insert into image_texts (image_id, field, body, source, model, updated_at)
    values (%s, %s, %s, 'model', %s, now())
    on conflict (image_id, field) do update set
      body = excluded.body, source = excluded.source, model = excluded.model,
      updated_at = now()
"""
# The guard repeats the caller's source check atomically, so an owner edit that lands
# while the describer runs still wins.
_GUARD = " where image_texts.source <> 'owner'"


async def _apply_described(
    conn: Conn, image_id: int, d: Described, want: set[str], force: set[str]
) -> bool:
    """Write the wanted fields; True if the analysis text changed. `force` is the subset the
    admin explicitly handed back to the model, so owner text there is overwritten."""
    changed = False
    async with conn.transaction():
        cur = await conn.execute(
            "select body from image_texts where image_id = %s and field = %s",
            (image_id, EMBED_FIELD),
        )
        prev = await cur.fetchone()
        for field in _TEXTS:
            body = getattr(d, field)
            if field not in want or not body:
                continue
            sql = _MODEL_TEXT if field in force else _MODEL_TEXT + _GUARD
            cur = await conn.execute(sql, (image_id, field, body, d.model))
            if field == EMBED_FIELD and cur.rowcount > 0 and (prev is None or prev["body"] != body):
                # Same transaction as the text, as in ingest: no stale vector survives a crash.
                await conn.execute(
                    "delete from embeddings where image_id = %s and kind = %s", (image_id, KIND)
                )
                await mycelium.mark_dirty(conn)
                changed = True
        if "tags" in want and d.tags:
            if "tags" in force:
                await conn.execute("delete from tags where image_id = %s", (image_id,))
            else:
                await conn.execute(
                    "delete from tags where image_id = %s and source = 'model'", (image_id,)
                )
            await conn.execute(
                """
                insert into tags (image_id, tag, source, model)
                select %(id)s, t, 'model', %(m)s from unnest(%(tags)s::text[]) t
                where not exists (select 1 from tags where image_id = %(id)s and source = 'owner')
                on conflict do nothing
                """,
                {"id": image_id, "m": d.model, "tags": d.tags},
            )
    return changed


async def _embed(pool: Pool, image_id: int, settings: Settings, embedder: DocumentEmbedder) -> None:
    async with pool.connection() as conn:
        texts = await _texts(conn, image_id)
        cur = await conn.execute(
            "select model from embeddings where image_id = %s and kind = %s", (image_id, KIND)
        )
        prev = await cur.fetchone()
    if EMBED_FIELD not in texts:
        log.info("regen: image %s has no analysis; nothing to embed", image_id)
        return
    body = texts[EMBED_FIELD]["body"]
    out = await embedder.embed_documents([body])
    vec = np.asarray(out[0], dtype=np.float32)
    if vec.shape != (settings.embed_dim,):
        raise HardError(f"embedding has shape {vec.shape}, want ({settings.embed_dim},)")
    async with pool.connection() as conn:
        # Conditional on the text still being what was embedded: an owner edit during the
        # embed enqueues a regen that dedupes into this running job, so nothing else would
        # replace a vector written for the old text.
        cur = await conn.execute(
            """
            insert into embeddings (image_id, kind, vec, model, dim, created_at)
            select %(id)s, %(kind)s, %(vec)s, %(model)s, %(dim)s, now()
             where exists (select 1 from image_texts where image_id = %(id)s
                             and field = %(field)s and body = %(body)s)
            on conflict (image_id, kind) do update set
              vec = excluded.vec, model = excluded.model, dim = excluded.dim, created_at = now()
            """,
            {
                "id": image_id,
                "kind": KIND,
                "vec": vec,
                "model": settings.embed_model,
                "dim": settings.embed_dim,
                "field": EMBED_FIELD,
                "body": body,
            },
        )
        if cur.rowcount == 0:
            if await _row(conn, image_id) is None:
                return
            raise SoftError("analysis changed while embedding")
        # Same transaction as the vector, committed when the connection is returned.
        await mycelium.mark_dirty(conn)
    await refresh_corpus_stats(pool, kind=KIND, model=settings.embed_model)
    if prev is not None and prev["model"] != settings.embed_model:
        # The row moved spaces, so the old space's mean and n are now stale too.
        await refresh_corpus_stats(pool, kind=KIND, model=prev["model"])


async def _haiku(pool: Pool, image_id: int, settings: Settings, writer: HaikuWriter) -> None:
    async with pool.connection() as conn:
        texts = await _texts(conn, image_id)
    if "description" not in texts:
        log.info("regen: image %s has no description; no haiku", image_id)
        return
    try:
        l1, l2, l3 = await writer(texts["description"]["body"], settings=settings, pool=pool)
    except DescribeError as e:
        raise _soft_or_hard("haiku", e) from e
    async with pool.connection() as conn:
        await conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id) values (%s, %s, %s, 'model', %s)"
            " on conflict do nothing",
            (l1, l2, l3, image_id),
        )


# -- regen ------------------------------------------------------------------


async def regen(
    pool: Pool,
    image_id: int,
    fields: set[str],
    *,
    force_owner: bool,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer = describe,
    writer: HaikuWriter = haiku_from,
) -> None:
    async with pool.connection() as conn:
        row = await _row(conn, image_id)
        if row is None:
            log.info("regen: image %s is gone; nothing to do", image_id)
            return
        texts = await _texts(conn, image_id)
        cur = await conn.execute(
            "select exists (select 1 from tags where image_id = %s and source = 'owner') as o",
            (image_id,),
        )
        tag_row = await cur.fetchone()
    owner_tags = bool(tag_row and tag_row["o"])

    def owned(f: str) -> bool:
        return bool(texts.get(f, {}).get("source") == "owner")

    want: set[str] = set()
    for f in _TEXTS:
        if f in fields and (force_owner or not owned(f)):
            want.add(f)
    if "tags" in fields and (force_owner or not owner_tags):
        want.add("tags")
    # The describer writes both in one call, and a new description over an old analysis
    # would leave the two describing different crops or prompts. An owner analysis is
    # only replaced when asked for by name.
    if "description" in want and not owned("analysis"):
        want.add("analysis")
    force = (want & fields) if force_owner else set()

    reembed = "embedding" in fields
    if want:
        described = await _describe(pool, row, want, settings, describer)
        async with pool.connection() as conn:
            if await _apply_described(conn, image_id, described, want, force):
                reembed = True
    if reembed:
        await _embed(pool, image_id, settings, embedder)
    if "haiku" in fields:
        await _haiku(pool, image_id, settings, writer)


async def handle_regen(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer = describe,
    writer: HaikuWriter = haiku_from,
) -> None:
    try:
        image_id = int(job.payload["image_id"])
        fields = set(normalize_fields(job.payload.get("fields") or []))
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad regen payload: {e}") from e
    await regen(
        pool,
        image_id,
        fields,
        force_owner=bool(job.payload.get("force_owner")),
        settings=settings,
        embedder=embedder,
        describer=describer,
        writer=writer,
    )


# -- crop -------------------------------------------------------------------


def _rebuild(
    path: Path, rect: Mapping[str, Any] | None, watermark: bool
) -> tuple[Public, dict[str, int] | None]:
    data = path.read_bytes()
    clamped = None
    if rect is not None:
        dims = original_dims(path)
        if dims is None:
            raise UndecodableImage("original is unreadable")
        clamped = clamp_rect(rect, *dims)
    return rebuild_public(data, clamped, watermark), clamped


async def _superseded(conn: Conn, job: Job, image_id: int) -> bool:
    # Two workers can hold two crops of one image at once; whichever gets the lock first,
    # only the newest request may land.
    cur = await conn.execute(
        "select exists (select 1 from jobs where type = 'crop' and id > %s"
        " and status <> 'failed' and payload->>'image_id' = %s) as s",
        (job.id, str(image_id)),
    )
    row = await cur.fetchone()
    return bool(row and row["s"])


async def _crop_locked(
    conn: Conn,
    job: Job,
    image_id: int,
    rect: Mapping[str, Any] | None,
    settings: Settings,
) -> bool:
    if await _superseded(conn, job, image_id):
        log.info("crop job %s for image %s superseded by a newer one", job.id, image_id)
        return False
    # Re-read under the lock: ingest and delete take the same one, so pub_sha is stable now.
    row = await _row(conn, image_id)
    if row is None:
        return False
    orig = find_original(settings.media_orig, row["sha256"])
    if orig is None:
        raise HardError(f"original for image {image_id} is missing")
    try:
        pub, clamped = await asyncio.to_thread(_rebuild, orig, rect, settings.watermark)
    except (UndecodableImage, ValueError) as e:
        raise HardError(f"crop: {e}") from e

    storage = LocalStorage(settings.media_pub.parent)
    for d in pub.derivatives:
        if not storage.exists(d.key):
            storage.put_bytes(d.key, d.data)

    old = str(row["pub_sha"])
    async with conn.transaction():
        await conn.execute(
            """
            update images set pub_sha = %s, width = %s, height = %s, blurhash = %s, palette = %s,
                   crop = %s, updated_at = now()
             where id = %s
            """,
            (
                pub.pub_sha,
                pub.width,
                pub.height,
                pub.blurhash,
                Jsonb(pub.palette),
                Jsonb(clamped) if clamped is not None else None,
                image_id,
            ),
        )
        cur = await conn.execute(
            "select exists (select 1 from images where pub_sha = %s and id <> %s) as shared",
            (old, image_id),
        )
        shared = await cur.fetchone()
    # After the commit, so a crash in between leaks files rather than breaking the page.
    if old != pub.pub_sha and not (shared and shared["shared"]):
        n = await asyncio.to_thread(remove_pub, settings.media_pub, old)
        log.info("image %s cropped; removed %d files of %s", image_id, n, old[:10])
    return True


async def handle_crop(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer = describe,
    writer: HaikuWriter = haiku_from,
) -> None:
    try:
        image_id = int(job.payload["image_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad crop payload: {e}") from e
    rect = job.payload.get("rect")
    if rect is not None and not isinstance(rect, dict):
        raise HardError("crop rect must be an object or null")

    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        row = await _row(conn, image_id)
        if row is None:
            log.info("crop: image %s is gone; nothing to do", image_id)
            await conn.set_autocommit(False)
            return
        sha = row["sha256"]
        # The ingest lock: a re-drop of the same bytes rewrites pub_sha too.
        await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (sha,))
        try:
            applied = await _crop_locked(conn, job, image_id, rect, settings)
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (sha,))
                await conn.set_autocommit(False)
    if not applied:
        return
    # Owner text stays: it was written about the picture, not about its framing.
    await regen(
        pool,
        image_id,
        set(_AFTER_CROP),
        force_owner=False,
        settings=settings,
        embedder=embedder,
        describer=describer,
        writer=writer,
    )


# -- bulk -------------------------------------------------------------------


async def _enqueue_each(pool: Pool, fields: list[str], where: str) -> int:
    fields = normalize_fields(fields)
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key)
            select 'regen', jsonb_build_object('image_id', i.id, 'fields', %(fields)s::jsonb),
                   'regen:' || i.id || ':' || %(suffix)s
              from images i
             where {where}
             order by i.id
            on conflict do nothing
            """.encode(),  # noqa: S608 -- where is a fixed literal from this module
            {"fields": Jsonb(fields), "suffix": ",".join(fields)},
        )
        return cur.rowcount


async def handle_reembed_all(job: Job, *, pool: Pool) -> None:
    # Every image with an analysis, live or not: it costs no API money, and search
    # covers non-live images too.
    n = await _enqueue_each(
        pool,
        ["embedding"],
        "exists (select 1 from image_texts t where t.image_id = i.id and t.field = 'analysis')",
    )
    log.info("reembed_all: enqueued %d", n)


async def handle_redescribe_all(job: Job, *, pool: Pool) -> None:
    # Live only: every call is paid for, and images still in ingest get described there.
    # regen itself skips owner fields, so model-sourced text is all this touches.
    n = await _enqueue_each(pool, list(_DESCRIBED), "i.status = 'live'")
    log.info("redescribe_all: enqueued %d", n)


async def handle_grow_haikus(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder | None = None,
    grower: Grower = grow_into,
) -> None:
    n = job.payload.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_GROW:
        raise HardError(f"grow_haikus n must be 1 to {MAX_GROW}")
    try:
        stats = await grower(pool, n, settings=settings, embedder=embedder)
    except DescribeError as e:
        raise _soft_or_hard("grow", e) from e
    except ValueError as e:
        raise HardError(f"grow: {e}") from e
    log.info("grow_haikus: %s", summary(stats))


# -- wiring -----------------------------------------------------------------


def _regen_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await handle_regen(job, pool=pool, settings=settings, embedder=embedder)

    return go


def _crop_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await handle_crop(job, pool=pool, settings=settings, embedder=embedder)

    return go


def _reembed_all_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await handle_reembed_all(job, pool=pool)

    return go


def _redescribe_all_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await handle_redescribe_all(job, pool=pool)

    return go


def _grow_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await handle_grow_haikus(job, pool=pool, settings=settings, embedder=embedder)

    return go


def _genesis_handler(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> Handler:
    async def go(job: Job) -> None:
        await genesis.handle(job, pool=pool, settings=settings)

    return go


HANDLERS: dict[str, HandlerFactory] = {
    "regen": _regen_handler,
    "crop": _crop_handler,
    "reembed_all": _reembed_all_handler,
    "redescribe_all": _redescribe_all_handler,
    "grow_haikus": _grow_handler,
    genesis.JOB: _genesis_handler,
}


def make_handlers(pool: Pool, settings: Settings, embedder: DocumentEmbedder) -> dict[str, Handler]:
    return {name: factory(pool, settings, embedder) for name, factory in HANDLERS.items()}
