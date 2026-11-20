"""Ingest orchestration: one processing dir in, one live image out.

Every step is keyed by sha256 and checks what already exists before writing, so a
job that died at any point can simply be run again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
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

from penish import uploads
from penish.collection import store as collection
from penish.config import Settings
from penish.mycelium import store as mycelium
from penish.storage import LocalStorage
from penish.text import slugify
from penish.worker import facet_jobs, nudity
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
from penish.worker.nudity import Detector, detect
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


def _held(key: str) -> str:
    # Settings.media_held sits beside pub under the same storage root.
    return "held/" + key.removeprefix("pub/")


def _have(storage: LocalStorage, keys: list[str], hidden: bool) -> bool:
    """Every key present where the picture needs it. An unpublished picture's files may
    be held, or still in pub when a shown picture shares its pub_sha."""
    return all(storage.exists(k) or (hidden and storage.exists(_held(k))) for k in keys)


def _put(storage: LocalStorage, key: str, data: bytes, hidden: bool) -> None:
    # An unpublished picture is healed into the holding dir: writing to pub would serve
    # it again at its old URL.
    if not _have(storage, [key], hidden):
        storage.put_bytes(_held(key) if hidden else key, data)


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


async def _write_media(
    storage: LocalStorage, decoded: Decoded, settings: Settings, hidden: bool = False
) -> int:
    # Only for images still displayed as uploaded, where pub_sha == sha256.
    sha, img = decoded.sha256, decoded.image
    size = await _ensure_orig(storage, decoded)
    if not _have(storage, _all_keys(sha, decoded.width), hidden):
        derivs = await asyncio.to_thread(make_derivatives, img, sha, settings.watermark)
        for d in derivs:
            _put(storage, d.key, d.data, hidden)
    return size


async def record_served(conn: Conn, image_id: int, pub_sha: str | None, width: int | None) -> None:
    """Note a ladder the site served for this picture. Cloudflare keeps it as immutable
    long after a crop, a heal or an unpublish stops naming it, and the purge list an
    unpublished picture shows is built from these rows."""
    if not pub_sha or not width:
        return
    await conn.execute(
        "insert into served_media (image_id, pub_sha, width) values (%s, %s, %s)"
        " on conflict do nothing",
        (image_id, pub_sha, width),
    )


async def _rebuild(
    conn: Conn,
    storage: LocalStorage,
    decoded: Decoded,
    row: dict[str, Any],
    settings: Settings,
    hidden: bool = False,
) -> tuple[str, int]:
    data = await asyncio.to_thread(storage.path(_orig_key(decoded)).read_bytes)
    pub = await asyncio.to_thread(rebuild_public, data, row["crop"], settings.watermark)
    for d in pub.derivatives:
        _put(storage, d.key, d.data, hidden)
    if row["status"] == "live" and pub.pub_sha != row["pub_sha"]:
        await record_served(conn, int(row["id"]), row["pub_sha"], row["width"])
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
    reembed = field == EMBED_FIELD and body != prev_body
    async with conn.transaction():
        if reembed:
            # Before the text row: a facet write and an admin edit take the state row and
            # then the analysis row, so the other order deadlocks with them. Locked rather
            # than marked: an owner analysis may turn the model's write away.
            await mycelium.read_state(conn, lock=True)
        cur = await conn.execute(_UPSERT_TEXT, (image_id, field, body, source, model))
        if cur.rowcount > 0 and reembed:
            # No column records which text an embedding came from, so dropping it in the
            # same transaction is what guarantees a crash can never leave a stale vector.
            await conn.execute(
                "delete from embeddings where image_id = %s and kind = %s", (image_id, KIND)
            )
            await facet_jobs.invalidate_split(conn, image_id)
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
            # The detector's tags stay: they answer a different question than the sidecar.
            await conn.execute(
                "delete from tags where image_id = %s and source <> 'detect'", (image_id,)
            )
            await conn.execute(
                "insert into tags (image_id, tag, source, model)"
                " select %s, t, 'owner', null from unnest(%s::text[]) t"
                " on conflict (image_id, tag) do update set source = 'owner', model = null",
                (image_id, tags),
            )
    return caption_changed


async def _missing(conn: Conn, image_id: int) -> set[str]:
    have = set(await _texts(conn, image_id))
    want = {f for f in ("caption", "description", "analysis") if f not in have}
    # The detector runs first and tags a nude picture; that is not the describer's tagging.
    cur = await conn.execute(
        "select exists (select 1 from tags where image_id = %s and source <> 'detect') as t",
        (image_id,),
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
                     where id = %s and status not in ('live', 'unpublished')
                    """,
                    (slug, surprise, image_id),
                )
                went_live = cur.rowcount > 0
                if went_live:
                    # Same transaction as the status flip: jobs.enqueue would use its own
                    # connection, and a crash between the two would lose the haiku forever.
                    for kind in (
                        "haiku",
                        "categorize",
                        facet_jobs.EXTRACT_JOB,
                        facet_jobs.SPLIT_JOB,
                    ):
                        await conn.execute(
                            "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
                            " on conflict do nothing",
                            (kind, Jsonb({"image_id": image_id}), f"{kind}:{image_id}"),
                        )
                    # Filing waits for publish; a request written at ingest is filed now.
                    await conn.execute(collection.ENQUEUE_FOR_IMAGE, (image_id,))
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


# -- uploads --------------------------------------------------------------


async def _upload_on(conn: Conn, what: str, stem: str, write: Awaitable[None]) -> None:
    # The uploads row is a record of the ingest, not part of it: losing one is logged,
    # never the reason a picture fails.
    try:
        await write
    except Exception:
        log.exception("ingest %s: could not %s the upload row", stem, what)


async def _upload(
    pool: Pool, what: str, stem: str, write: Callable[[Conn], Awaitable[None]]
) -> None:
    # Its own pool connection, committed on its own: written before the picture is
    # decoded, it has to outlive whatever happens to the ingest after it.
    try:
        async with pool.connection() as conn:
            await write(conn)
    except Exception:
        log.exception("ingest %s: could not %s the upload row", stem, what)


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
    # A re-drop of a published picture leaves its status alone: one taken down by the
    # admin stays down, whatever happens to its files and text here.
    if row["status"] not in ("live", "unpublished"):
        await conn.execute(
            "update images set status = 'processing', updated_at = now() where id = %s",
            (row["id"],),
        )
    return row, True


async def _set_status(conn: Conn, image_id: int, status: str) -> None:
    # A live image stays live: a failed reprocess must not unpublish it. An unpublished
    # one stays unpublished: pending_enrichment would put it back in line for go-live.
    await conn.execute(
        "update images set status = %s, updated_at = now()"
        " where id = %s and status not in ('live', 'unpublished')",
        (status, image_id),
    )


async def handle_ingest(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    describer: Describer = describe,
    detector: Detector = detect,
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
    # Before the decode, so a file that never becomes a picture still leaves its hash and
    # the collection it was meant for. The decoder hashes the same bytes again later.
    upload_sha = hashlib.sha256(data).hexdigest()
    await _upload(
        pool,
        "record",
        group.stem,
        lambda c: uploads.record(
            c,
            stem=group.stem,
            sha256=upload_sha,
            filename=group.image.name,
            size=len(data),
            collection_line=group.values.get("collection"),
            source="rsync",
        ),
    )
    try:
        decoded = await asyncio.to_thread(load_and_validate, data)
    except UndecodableImage as e:
        reason = f"{group.image.name}: {e}"
        _move_to_failed(directory, group.stem, settings, reason, group.unknown)
        await _upload(
            pool,
            "fail",
            group.stem,
            lambda c: uploads.fail(c, stem=group.stem, sha256=upload_sha, error=reason),
        )
        raise HardError(str(e)) from e
    sha = decoded.sha256

    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        # Two drops of the same bytes can be claimed by both workers at once; they
        # would race on the same media temp files and slug, so serialize per sha.
        await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (sha,))
        try:
            await _ingest_locked(
                conn,
                pool,
                group,
                decoded,
                data,
                directory,
                settings,
                embedder,
                describer,
                detector,
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
    detector: Detector,
) -> None:
    sha = decoded.sha256
    row, existed = await _image_row(conn, sha, group.stem)
    image_id = int(row["id"])
    was_live = row["status"] == "live"
    # Unpublished: its files are held and it must never go live from here.
    hidden = row["status"] == "unpublished"
    storage = LocalStorage(settings.media_pub.parent)
    pub_sha = str(row["pub_sha"])
    # Width of the displayed pixels, which is what names the derivative ladder.
    pub_width = decoded.width

    if pub_sha != sha:
        # Cropped (or otherwise rebuilt) by an admin job: the displayed pixels are not the
        # upload's, so ingest must not lay the uncropped ladder back down. It only heals.
        await _ensure_orig(storage, decoded)
        pub_width = int(row["width"])
        if not _have(storage, _all_keys(pub_sha, pub_width), hidden):
            pub_sha, pub_width = await _rebuild(conn, storage, decoded, row, settings, hidden)
    elif (
        not existed
        or row["width"] is None
        or not _have(storage, _all_keys(sha, decoded.width), hidden)
    ):
        size = await _write_media(storage, decoded, settings, hidden)
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
    line = group.values.get("collection")
    note = group.values.get("note")
    if "note" in group.sidecars and note is None:
        log.warning("ingest: image %s note sidecar is empty or not utf-8; dropped", image_id)
    if line is not None:
        # Before the publish gate, so the request is waiting when the picture goes live.
        await collection.request_from_line(conn, image_id, line, note)
    elif note is not None:
        # A note belongs to a leaf, and without a collection line there is none to file.
        log.warning("ingest: image %s has a note but no collection; dropped", image_id)

    img_bytes: bytes | None = None
    kinds = await nudity.kinds_for(conn, image_id)
    if kinds is None:
        # Before the describer, which writes nude pictures differently. A re-drop of a
        # picture already checked keeps its result, as it keeps its text.
        img_bytes = await asyncio.to_thread(_describe_bytes, decoded.image)
        try:
            found = await detector(img_bytes, "image/webp", settings=settings, pool=pool)
        except DescribeError as e:
            if e.retryable:
                await _set_status(conn, image_id, "pending_enrichment")
                raise SoftError(f"nudity: {e}") from e
            # A refusal says the same thing next time; the picture goes up unchecked and
            # the worker start sweep or the admin can try again.
            log.warning("ingest: nudity check for image %s failed: %s", image_id, e)
        else:
            await nudity.store(conn, image_id, found)
            kinds = found.kinds

    want = await _missing(conn, image_id)
    if want:
        if img_bytes is None:
            img_bytes = await asyncio.to_thread(_describe_bytes, decoded.image)
        try:
            described = await describer(
                img_bytes,
                "image/webp",
                want=want,
                settings=settings,
                pool=pool,
                nudity=kinds or (),
            )
        except DescribeError as e:
            if e.retryable:
                await _set_status(conn, image_id, "pending_enrichment")
                raise SoftError(f"describe: {e}") from e
            await _set_status(conn, image_id, "failed")
            reason = f"describe: {e}"
            # Left in processing/ it would be re-enqueued and re-described on every restart.
            _move_to_failed(directory, group.stem, settings, reason, group.unknown)
            await _upload_on(
                conn,
                "fail",
                group.stem,
                uploads.fail(conn, stem=group.stem, sha256=sha, error=reason),
            )
            raise HardError(reason) from e
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
    media_ok = _have(storage, _webp_keys(pub_sha, pub_width), hidden)
    ok_texts = "description" in texts and EMBED_FIELD in texts
    if not (media_ok and ok_texts and emb and emb["e"]):
        await _set_status(conn, image_id, "pending_enrichment")
        raise SoftError(
            f"publish gate not met: media={media_ok} description={'description' in texts}"
            f" analysis={EMBED_FIELD in texts} embedding={bool(emb and emb['e'])}"
        )

    caption = texts.get("caption")
    base = _slug_base(caption["body"] if caption else None, sha)
    if not (was_live or hidden):
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

    # Through the publish gate: whatever stopped this upload before no longer stands.
    await _upload_on(conn, "clear", group.stem, uploads.clear(conn, stem=group.stem, sha256=sha))
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
        kinds = await nudity.kinds_for(conn, image_id)
    if row is None:
        log.info("haiku: image %s has no description (deleted?); skipping", image_id)
        return
    try:
        h = await writer(row["body"], settings=settings, pool=pool, nudity=kinds or ())
    except DescribeError as e:
        if e.retryable:
            raise SoftError(f"haiku: {e}") from e
        raise HardError(f"haiku: {e}") from e
    await insert_haiku(pool, image_id, h)


# A first deploy on a site with many haiku-less pictures must not launch an unbounded batch
# of paid calls; the rest wait for the next worker start.
SWEEP_HAIKU_CAP = 25


async def enqueue_unwritten(pool: Pool, cap: int = SWEEP_HAIKU_CAP) -> int:
    """Live pictures that never had their haiku written get the job going live would have
    queued. Run at worker start, so a picture whose haiku job was lost is made good. At
    most `cap` per call, oldest first.

    Any haiku job on record in any status keeps a picture out, done included. A job can
    finish with no row behind it (the text was already in the deck, or the owner deleted
    the haiku), and queueing it again would pay for the same answer at every restart. A
    failed job is a picture every model refused, and it waits for the owner's retry on
    the queue page."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key)
            select 'haiku', jsonb_build_object('image_id', i.id), 'haiku:' || i.id
              from images i
             where i.status = 'live'
               and exists (select 1 from image_texts t
                            where t.image_id = i.id and t.field = 'description')
               and not exists (select 1 from haikus h where h.image_id = i.id)
               and not exists (select 1 from jobs j
                                where j.type = 'haiku' and j.dedupe_key = 'haiku:' || i.id)
               and not exists (select 1 from jobs j
                                where j.type = 'haiku' and j.payload->>'image_id' = i.id::text)
             order by i.id
             limit %s
            on conflict do nothing
            """,
            (cap,),
        )
        queued = cur.rowcount
        cur = await conn.execute(
            """
            select count(*)::int as n from images i
             where i.status = 'live'
               and exists (select 1 from image_texts t
                            where t.image_id = i.id and t.field = 'description')
               and not exists (select 1 from haikus h where h.image_id = i.id)
               and not exists (select 1 from jobs j
                                where j.type = 'haiku' and j.dedupe_key = 'haiku:' || i.id)
               and not exists (select 1 from jobs j
                                where j.type = 'haiku' and j.payload->>'image_id' = i.id::text)
            """
        )
        row = await cur.fetchone()
    remaining = int(row["n"]) if row else 0
    if queued or remaining:
        log.info("haiku sweep: queued %d, %d remain for the next start", queued, remaining)
    return queued


async def insert_haiku(pool: Pool, image_id: int, h: tuple[str, str, str]) -> None:
    """A model haiku for a picture, with the model that wrote it when the writer says."""
    model = getattr(h, "model", None)
    l1, l2, l3 = h
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id, model)"
            " values (%s, %s, %s, 'model', %s, %s) on conflict do nothing",
            (l1, l2, l3, image_id, model),
        )
    if cur.rowcount == 0:
        # The deck already holds these three lines, so the job ends done with no haiku for
        # this picture. Said here so a picture without one is explainable from the log.
        log.warning(
            "haiku for image %s written by %s was already in the deck; none stored", image_id, model
        )
        return
    log.info("haiku for image %s written by %s", image_id, model)
