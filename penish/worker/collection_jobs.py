"""Worker side of collections: filing a requested picture, embedding book anchors, and
the full re-sort, on demand or weekly when the owner has switched that on.

Membership is never decided here. A filing job only acts on a request the owner made.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

import numpy as np

from penish import runtime
from penish.collections import store
from penish.config import Settings
from penish.worker.ingest import DocumentEmbedder
from penish.worker.jobs import HardError, Job, Pool, SoftError, enqueue

log = logging.getLogger(__name__)

WEEK = timedelta(days=7)
# How often the weekly loop looks; the re-sort itself is due at most once a week.
WEEKLY_CHECK_SECONDS = 3600.0


async def embed_anchors(
    pool: Pool, settings: Settings, embedder: DocumentEmbedder, cid: int | None = None
) -> int:
    """Embed every anchor whose text has no vector under the current model."""
    async with pool.connection() as conn:
        stale = await store.stale_anchors(conn, settings, cid)
    if not stale:
        return 0
    out = await embedder.embed_documents([r["anchor_text"] for r in stale])
    async with pool.connection() as conn:
        for r, v in zip(stale, out, strict=True):
            vec = np.asarray(v, dtype=np.float32)
            if vec.shape != (settings.embed_dim,):
                raise HardError(f"anchor embedding has shape {vec.shape}")
            await store.store_anchor(
                conn, int(r["id"]), r["anchor_text"], vec, settings.embed_model
            )
    log.info("embedded %d collection anchors", len(stale))
    return len(stale)


async def handle_file(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    # The marker a malformed sidecar leaves: already failed, and a retry says the same.
    if job.payload.get("error"):
        raise HardError(str(job.payload["error"]))
    image_id = int(job.payload["image_id"])
    cid = int(job.payload["collection_id"])
    async with pool.connection() as conn:
        c = await store.get(conn, cid=cid)
    if c is None:
        log.info("collection %s is gone; nothing to file", cid)
        return
    # Before filing: an empty book with no anchor vector cannot be scored at all.
    await embed_anchors(pool, settings, embedder, cid)
    p = await store.load_params(pool, settings)
    try:
        async with pool.connection() as conn, conn.transaction():
            await store.file_one(conn, c, image_id, settings, p)
    except store.NotReady as e:
        raise SoftError(str(e)) from e
    except store.FilingError as e:
        async with pool.connection() as conn:
            await store.set_error(conn, c, image_id, str(e))
        raise HardError(str(e)) from e


async def handle_anchor(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    cid = job.payload.get("collection_id")
    await embed_anchors(pool, settings, embedder, int(cid) if cid is not None else None)


async def handle_resort(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    cid = int(job.payload["collection_id"])
    async with pool.connection() as conn:
        c = await store.get(conn, cid=cid)
    if c is None:
        return
    await embed_anchors(pool, settings, embedder, cid)
    p = await store.load_params(pool, settings)
    async with pool.connection() as conn, conn.transaction():
        moves = await store.resort(conn, c, settings, p, dry_run=False)
    log.info("re-sorted %s: %d moves", c.slug, len(moves))


async def enqueue_due_resorts(pool: Pool) -> int:
    """A re-sort for every collection a week past its last one, if the owner wants them."""
    if not await runtime.get(pool, "collections.weekly_resort"):
        return 0
    n = 0
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from collections where resorted_at is null or resorted_at < now() - %s",
            (WEEK,),
        )
        due = [int(r["id"]) for r in await cur.fetchall()]
    for cid in due:
        got = await enqueue(
            pool, store.RESORT_JOB, {"collection_id": cid}, dedupe_key=f"{store.RESORT_JOB}:{cid}"
        )
        n += got is not None
    return n


async def run_weekly(pool: Pool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n = await enqueue_due_resorts(pool)
            if n:
                log.info("enqueued %d weekly collection re-sorts", n)
        except Exception:
            # A database hiccup must not end the loop; the next hour tries again.
            log.exception("weekly re-sort check failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=WEEKLY_CHECK_SECONDS)


async def enqueue_stale_anchors(pool: Pool, settings: Settings) -> int:
    async with pool.connection() as conn:
        stale = await store.stale_anchors(conn, settings)
    if not stale:
        return 0
    got = await enqueue(pool, store.ANCHOR_JOB, {}, dedupe_key=store.ANCHOR_JOB)
    return int(got is not None)
