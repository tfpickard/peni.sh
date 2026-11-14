"""Ingest orchestration: one processing dir in, one live image out.

Every step is keyed by sha256 and checks what already exists before writing, so a
job that died at any point can simply be run again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import psycopg
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.mycelium import store as mycelium
from penish.storage import LocalStorage
from penish.text import slugify
from penish.worker.derive import (
    WIDTHS,
    Decoded,
    UndecodableImage,
    blurhash_of,
    clean_original,
    load_and_validate,
    make_derivatives,
    palette_of,
    rebuild_public,
)
from penish.worker.describe import Described, DescribeError, describe
from penish.worker.haiku import haiku_from
from penish.worker.jobs import HardError, Job, Pool, SoftError
from penish.worker.metrics import refresh_corpus_stats, surprise_at_arrival
from penish.worker.sidecars import Group, GroupError, parse_group

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Describer = Callable[..., Awaitable[Described]]
HaikuWriter = Callable[..., Awaitable[tuple[str, str, str]]]

# The embedding kind predates the analysis field; the vectors under it are made from the
# analysis text, and renaming the kind would orphan every stored vector and stats row.
KIND = "description"
# The text the vector is made from. The public description is not it: it is written to
# look past the subject, so it would put images near the wrong neighbors.
EMBED_FIELD = "analysis"
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Claude downsamples anything larger, so sending more only costs upload time.
_DESCRIBE_EDGE = 1568
_SLUG_RETRIES = 5


class DocumentEmbedder(Protocol):
    async def embed_documents(self, texts: list[str]) -> np.ndarray: ...


# -- failures -------------------------------------------------------------


def _move_to_failed(
    directory: Path, stem: str, settings: Settings, reason: str, unknown: list[str]
) -> Path:
    stem = Path(stem).name or directory.name
    lines = [reason]
    if unknown:
        lines += ["", "unknown files:", *(f"  {n}" for n in unknown)]
    # Written before the move so a crash in between still leaves the reason with the files.
    (directory / f"{stem}.err").write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings.failed.mkdir(parents=True, exist_ok=True)
    dest = settings.failed / stem
    n = 1
    while dest.exists():
        n += 1
        dest = settings.failed / f"{stem}-{n}"
    os.rename(directory, dest)
    log.warning("ingest of %s failed (%s); moved to %s", stem, reason, dest)
    return dest


# -- media ----------------------------------------------------------------


def _widths(src_w: int) -> list[int]:
    # Mirrors make_derivatives' ladder so presence can be checked without encoding.
    return [w for w in WIDTHS if w <= src_w] or [src_w]


def _webp_keys(sha: str, src_w: int) -> list[str]:
    return [f"pub/{sha}-{w}.webp" for w in _widths(src_w)]


def _all_keys(sha: str, src_w: int) -> list[str]:
    return [f"pub/{sha}-{w}.{fmt}" for w in _widths(src_w) for fmt in ("avif", "webp")]


def _describe_bytes(img: Any) -> bytes:
    small = img.thumbnail_image(_DESCRIBE_EDGE, height=_DESCRIBE_EDGE, size="down")
    out = small.write_to_buffer(".webp", Q=85)
    assert isinstance(out, bytes)
    return out


def _orig_key(decoded: Decoded) -> str:
    return f"orig/{decoded.sha256}.{_EXT[decoded.mime]}"


async def _ensure_orig(storage: LocalStorage, decoded: Decoded) -> int:
    key = _orig_key(decoded)
    if not storage.exists(key):
        data = await asyncio.to_thread(clean_original, decoded.image, decoded.mime)
        storage.put_bytes(key, data)
    return storage.path(key).stat().st_size


async def _write_media(storage: LocalStorage, decoded: Decoded, settings: Settings) -> int:
    # Only for images still displayed as uploaded, where pub_sha == sha256.
    sha, img = decoded.sha256, decoded.image
    size = await _ensure_orig(storage, decoded)
    if not all(storage.exists(k) for k in _all_keys(sha, decoded.width)):
        derivs = await asyncio.to_thread(make_derivatives, img, sha, settings.watermark)
        for d in derivs:
            if not storage.exists(d.key):
                storage.put_bytes(d.key, d.data)
    return size


async def _rebuild(
    conn: Conn, storage: LocalStorage, decoded: Decoded, row: dict[str, Any], settings: Settings
) -> tuple[str, int]:
    data = await asyncio.to_thread(storage.path(_orig_key(decoded)).read_bytes)
    pub = await asyncio.to_thread(rebuild_public, data, row["crop"], settings.watermark)
    for d in pub.derivatives:
        if not storage.exists(d.key):
            storage.put_bytes(d.key, d.data)
    await conn.execute(
        """
        update images set pub_sha = %s, width = %s, height = %s, blurhash = %s, palette = %s,
               updated_at = now()
         where id = %s
        """,
        (pub.pub_sha, pub.width, pub.height, pub.blurhash, Jsonb(pub.palette), row["id"]),
    )
    return pub.pub_sha, pub.width


# -- text -----------------------------------------------------------------


_UPSERT_TEXT = """
    insert into image_texts (image_id, field, body, source, model, updated_at)
    values (%s, %s, %s, %s, %s, now())
    on conflict (image_id, field) do update set
      body = excluded.body, source = excluded.source, model = excluded.model,
      updated_at = now()
    where image_texts.source <> 'owner' or excluded.source = 'owner'
"""


async def _texts(conn: Conn, image_id: int) -> dict[str, dict[str, Any]]:
    cur = await conn.execute(
        "select field, body, source from image_texts where image_id = %s", (image_id,)
    )
    return {r["field"]: r for r in await cur.fetchall()}


async def _write_text(
    conn: Conn,
    image_id: int,
    field: str,
    body: str,
    source: str,
    model: str | None,
    prev_body: str | None,
) -> None:
    async with conn.transaction():
        cur = await conn.execute(_UPSERT_TEXT, (image_id, field, body, source, model))
        if cur.rowcount > 0 and field == EMBED_FIELD and body != prev_body:
            # No column records which text an embedding came from, so dropping it in the
            # same transaction is what guarantees a crash can never leave a stale vector.
            await conn.execute(
                "delete from embeddings where image_id = %s and kind = %s", (image_id, KIND)
            )
            await mycelium.mark_dirty(conn)


async def _apply_owner(conn: Conn, image_id: int, group: Group) -> bool:
    """Write owner sidecars; True if the caption text changed."""
    current = await _texts(conn, image_id)
    caption_changed = False
    for field in ("caption", "description", "analysis", "prompt"):
        body = group.values.get(field)
        if not body:
            continue
        prev = current.get(field)
        if prev and prev["source"] == "owner" and prev["body"] == body:
            continue
        await _write_text(
            conn, image_id, field, body, "owner", None, prev["body"] if prev else None
        )
        if field == "caption" and (prev is None or prev["body"] != body):
            caption_changed = True
    tags = group.values.get("tags")
    if tags:
        async with conn.transaction():
            await conn.execute("delete from tags where image_id = %s", (image_id,))
            await conn.execute(
                "insert into tags (image_id, tag, source, model)"
                " select %s, t, 'owner', null from unnest(%s::text[]) t",
                (image_id, tags),
            )
    return caption_changed


async def _missing(conn: Conn, image_id: int) -> set[str]:
    have = set(await _texts(conn, image_id))
    want = {f for f in ("caption", "description", "analysis") if f not in have}
    cur = await conn.execute(
        "select exists (select 1 from tags where image_id = %s) as t", (image_id,)
    )
    row = await cur.fetchone()
    if not (row and row["t"]):
        want.add("tags")
    return want


async def _apply_model(conn: Conn, image_id: int, d: Described) -> None:
    for field in ("caption", "description", "analysis"):
        body = getattr(d, field)
        if body:
            await _write_text(conn, image_id, field, body, "model", d.model, None)
    if d.tags:
        await conn.execute(
            """
            insert into tags (image_id, tag, source, model)
            select %(id)s, t, 'model', %(m)s from unnest(%(tags)s::text[]) t
            where not exists (select 1 from tags where image_id = %(id)s and source = 'owner')
            on conflict do nothing
            """,
            {"id": image_id, "m": d.model, "tags": d.tags},
        )


# -- embedding ------------------------------------------------------------


async def _ensure_embedding(
    conn: Conn, image_id: int, group: Group, settings: Settings, embedder: DocumentEmbedder
) -> None:
    cur = await conn.execute(
        "select model from embeddings where image_id = %s and kind = %s", (image_id, KIND)
    )
    row = await cur.fetchone()
    if row is not None and row["model"] == settings.embed_model:
        return
    texts = await _texts(conn, image_id)
    # Without an analysis there is nothing faithful to embed. Falling back to an owner
    # description would file the image by whatever it chose to look at, so the publish
    # gate keeps it pending until the describer can write one.
    if EMBED_FIELD not in texts:
        return

    vec: np.ndarray | None = None
    side = group.values.get("embedding")
    if side is not None:
        if side.model == settings.embed_model and side.dim == settings.embed_dim:
            vec = np.asarray(side.vector, dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            # The corpus mean averages raw vectors, so an unnormalized sidecar would skew it.
            if norm > 0:
                vec = vec / norm
        else:
            log.info(
                "ignoring embedding sidecar for image %s: %s/%d, want %s/%d",
                image_id,
                side.model,
                side.dim,
                settings.embed_model,
                settings.embed_dim,
            )
    if vec is None:
        out = await embedder.embed_documents([texts[EMBED_FIELD]["body"]])
        vec = np.asarray(out[0], dtype=np.float32)
    if vec.shape != (settings.embed_dim,):
        raise HardError(f"embedding has shape {vec.shape}, want ({settings.embed_dim},)")
    async with conn.transaction():
        # The state row first, as the tick takes it, so the two never wait on each other.
        await mycelium.mark_dirty(conn)
        await conn.execute(
            """
            insert into embeddings (image_id, kind, vec, model, dim, created_at)
            values (%s, %s, %s, %s, %s, now())
            on conflict (image_id, kind) do update set
              vec = excluded.vec, model = excluded.model, dim = excluded.dim, created_at = now()
            """,
            (image_id, KIND, vec, settings.embed_model, settings.embed_dim),
        )


# -- publish --------------------------------------------------------------


async def _free_slug(conn: Conn, base: str, image_id: int) -> str:
    # slugify only emits [a-z0-9-], so base is safe inside a LIKE pattern.
    cur = await conn.execute(
        """
        select slug from images
         where (slug = %(b)s or slug like %(p)s) and id <> %(id)s
        union
        select slug from slug_aliases
         where (slug = %(b)s or slug like %(p)s) and image_id <> %(id)s
        """,
        {"b": base, "p": base + "-%", "id": image_id},
    )
    taken = {r["slug"] for r in await cur.fetchall()}
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def _slug_base(caption: str | None, sha: str) -> str:
    return slugify(caption or "") or sha[:10]


def _slug_fits(slug: str | None, base: str) -> bool:
    if slug is None:
        return False
    return slug == base or re.fullmatch(re.escape(base) + r"-\d+", slug) is not None


async def _go_live(conn: Conn, image_id: int, base: str, surprise: float | None) -> bool:
    """Publish a not-yet-live image; True if this call is the one that did it."""
    for _ in range(_SLUG_RETRIES):
        try:
            async with conn.transaction():
                # First, as the tick takes the state row first: a new picture changes who
                # may grow toward whom, and this waits out a tick in progress.
                await mycelium.mark_dirty(conn)
                slug = await _free_slug(conn, base, image_id)
                await conn.execute(
                    "delete from slug_aliases where slug = %s and image_id = %s",
                    (slug, image_id),
                )
                cur = await conn.execute(
                    """
                    update images set slug = %s, status = 'live', published_at = now(),
                           surprise = coalesce(surprise, %s), updated_at = now()
                     where id = %s and status <> 'live'
                    """,
                    (slug, surprise, image_id),
                )
                went_live = cur.rowcount > 0
                if went_live:
                    # Same transaction as the status flip: jobs.enqueue would use its own
                    # connection, and a crash between the two would lose the haiku forever.
                    for kind in ("haiku", "categorize"):
                        await conn.execute(
                            "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
                            " on conflict do nothing",
                            (kind, Jsonb({"image_id": image_id}), f"{kind}:{image_id}"),
                        )
                    # A new picture starts as just seen, not as neglected since upload.
                    # A row left from an earlier stint (or an older backfill) would carry
                    # a stale clock, so going live resets it; views stay counted.
                    await conn.execute(
                        "insert into picture_attention (image_id, last_viewed_at)"
                        " values (%s, now()) on conflict (image_id)"
                        " do update set last_viewed_at = now()",
                        (image_id,),
                    )
            return went_live
        except psycopg.errors.UniqueViolation:
            # Another image claimed the same slug between our check and the update.
            continue
    raise SoftError(f"could not find a free slug for {base!r}")


async def _reslug(conn: Conn, image_id: int, old: str | None, base: str) -> None:
    for _ in range(_SLUG_RETRIES):
        try:
            async with conn.transaction():
                slug = await _free_slug(conn, base, image_id)
                if slug == old:
                    return
                if old is not None:
                    await conn.execute(
                        "insert into slug_aliases (slug, image_id) values (%s, %s)"
                        " on conflict (slug) do nothing",
                        (old, image_id),
                    )
                await conn.execute(
                    "delete from slug_aliases where slug = %s and image_id = %s",
                    (slug, image_id),
                )
                await conn.execute(
                    "update images set slug = %s, updated_at = now() where id = %s",
                    (slug, image_id),
                )
            log.info("image %s renamed %s -> %s", image_id, old, slug)
            return
        except psycopg.errors.UniqueViolation:
            continue
    raise SoftError(f"could not find a free slug for {base!r}")


# -- handlers -------------------------------------------------------------


async def _image_row(conn: Conn, sha: str, stem: str) -> tuple[dict[str, Any], bool]:
    cur = await conn.execute(
        "insert into images (sha256, pub_sha, stem, status) values (%s, %s, %s, 'processing')"
        " on conflict (sha256) do nothing returning *",
        (sha, sha, stem),
    )
    row = await cur.fetchone()
    if row is not None:
        return row, False
    cur = await conn.execute("select * from images where sha256 = %s", (sha,))
    row = await cur.fetchone()
    assert row is not None
    if row["status"] != "live":
        await conn.execute(
            "update images set status = 'processing', updated_at = now() where id = %s",
            (row["id"],),
        )
    return row, True


async def _set_status(conn: Conn, image_id: int, status: str) -> None:
    # A live image stays live: a failed reprocess must not unpublish it.
    await conn.execute(
        "update images set status = %s, updated_at = now() where id = %s and status <> 'live'",
        (status, image_id),
    )


async def handle_ingest(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer = describe,
) -> None:
    directory = Path(job.payload["dir"])
    stem = str(job.payload.get("stem") or directory.name)
    if not await asyncio.to_thread(directory.is_dir):
        # Already published (dir removed) or already moved to failed/ by an earlier run.
        log.info("ingest %s: %s is gone, nothing to do", stem, directory)
        return

    try:
        group = parse_group(directory)
    except GroupError as e:
        _move_to_failed(directory, stem, settings, e.reason, e.unknown)
        raise HardError(e.reason) from e
    if group.unknown:
        log.warning("ingest %s: ignoring unknown files %s", stem, group.unknown)

    data = await asyncio.to_thread(group.image.read_bytes)
    try:
        decoded = await asyncio.to_thread(load_and_validate, data)
    except UndecodableImage as e:
        _move_to_failed(directory, group.stem, settings, f"{group.image.name}: {e}", group.unknown)
        raise HardError(str(e)) from e
    sha = decoded.sha256

    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        # Two drops of the same bytes can be claimed by both workers at once; they
        # would race on the same media temp files and slug, so serialize per sha.
        await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (sha,))
        try:
            await _ingest_locked(
                conn, pool, group, decoded, data, directory, settings, embedder, describer
            )
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (sha,))
                await conn.set_autocommit(False)


async def _ingest_locked(
    conn: Conn,
    pool: Pool,
    group: Group,
    decoded: Decoded,
    data: bytes,
    directory: Path,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer,
) -> None:
    sha = decoded.sha256
    row, existed = await _image_row(conn, sha, group.stem)
    image_id = int(row["id"])
    was_live = row["status"] == "live"
    storage = LocalStorage(settings.media_pub.parent)
    pub_sha = str(row["pub_sha"])
    # Width of the displayed pixels, which is what names the derivative ladder.
    pub_width = decoded.width

    if pub_sha != sha:
        # Cropped (or otherwise rebuilt) by an admin job: the displayed pixels are not the
        # upload's, so ingest must not lay the uncropped ladder back down. It only heals.
        await _ensure_orig(storage, decoded)
        pub_width = int(row["width"])
        if not all(storage.exists(k) for k in _all_keys(pub_sha, pub_width)):
            pub_sha, pub_width = await _rebuild(conn, storage, decoded, row, settings)
    elif (
        not existed
        or row["width"] is None
        or not all(storage.exists(k) for k in _all_keys(sha, decoded.width))
    ):
        size = await _write_media(storage, decoded, settings)
        bh = await asyncio.to_thread(blurhash_of, decoded.image)
        palette = await asyncio.to_thread(palette_of, decoded.image)
        await conn.execute(
            """
            update images set width = %s, height = %s, bytes = %s, mime = %s,
                   blurhash = %s, palette = %s, updated_at = now()
             where id = %s
            """,
            (decoded.width, decoded.height, size, decoded.mime, bh, Jsonb(palette), image_id),
        )

    caption_changed = await _apply_owner(conn, image_id, group)

    want = await _missing(conn, image_id)
    if want:
        img_bytes = await asyncio.to_thread(_describe_bytes, decoded.image)
        try:
            described = await describer(
                img_bytes, "image/webp", want=want, settings=settings, pool=pool
            )
        except DescribeError as e:
            if e.retryable:
                await _set_status(conn, image_id, "pending_enrichment")
                raise SoftError(f"describe: {e}") from e
            await _set_status(conn, image_id, "failed")
            # Left in processing/ it would be re-enqueued and re-described on every restart.
            _move_to_failed(directory, group.stem, settings, f"describe: {e}", group.unknown)
            raise HardError(f"describe: {e}") from e
        await _apply_model(conn, image_id, described)

    await _ensure_embedding(conn, image_id, group, settings, embedder)

    # -- publish gate
    texts = await _texts(conn, image_id)
    cur = await conn.execute(
        "select exists (select 1 from embeddings where image_id = %s and kind = %s"
        " and model = %s) as e",
        (image_id, KIND, settings.embed_model),
    )
    emb = await cur.fetchone()
    media_ok = all(storage.exists(k) for k in _webp_keys(pub_sha, pub_width))
    ok_texts = "description" in texts and EMBED_FIELD in texts
    if not (media_ok and ok_texts and emb and emb["e"]):
        await _set_status(conn, image_id, "pending_enrichment")
        raise SoftError(
            f"publish gate not met: media={media_ok} description={'description' in texts}"
            f" analysis={EMBED_FIELD in texts} embedding={bool(emb and emb['e'])}"
        )

    caption = texts.get("caption")
    base = _slug_base(caption["body"] if caption else None, sha)
    if not was_live:
        surprise = await surprise_at_arrival(pool, image_id, kind=KIND, model=settings.embed_model)
        if await _go_live(conn, image_id, base, surprise):
            log.info("image %s (%s) is live", image_id, sha[:10])
    elif (
        caption
        and caption["source"] == "owner"
        and (caption_changed or not _slug_fits(row["slug"], base))
    ):
        # Checking the fit too, not just this run's change, covers a crash between
        # the caption write and the rename. Model captions never rename a live image.
        await _reslug(conn, image_id, row["slug"], base)

    await refresh_corpus_stats(pool, kind=KIND, model=settings.embed_model)
    await asyncio.to_thread(shutil.rmtree, directory)


async def handle_haiku(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    writer: HaikuWriter = haiku_from,
) -> None:
    image_id = int(job.payload["image_id"])
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select body from image_texts where image_id = %s and field = 'description'",
            (image_id,),
        )
        row = await cur.fetchone()
    if row is None:
        log.info("haiku: image %s has no description (deleted?); skipping", image_id)
        return
    try:
        l1, l2, l3 = await writer(row["body"], settings=settings, pool=pool)
    except DescribeError as e:
        if e.retryable:
            raise SoftError(f"haiku: {e}") from e
        raise HardError(f"haiku: {e}") from e
    async with pool.connection() as conn:
        await conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id) values (%s, %s, %s, 'model', %s)"
            " on conflict do nothing",
            (l1, l2, l3, image_id),
        )
