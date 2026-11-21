"""Collection jobs: filing a requested picture, embedding book anchors, re-sorting, judging
the leaves for the owner's re-judging, and the weekly re-sort for a collection that asks
for one.

Filing waits on publish rather than a timer: the go-live transaction enqueues it, and the
worker start sweep re-enqueues any request a crash left without a job.

The judge is asked before the filing takes the collection's lock, and its answer handed in:
a model call can take a minute, and every owner action on the collection waits on the lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from psycopg.types.json import Jsonb

from penish import runtime
from penish.collection import judge, store
from penish.collection.numerals import parse_line
from penish.collection.plan import Tuning
from penish.config import Settings
from penish.worker.ingest import DocumentEmbedder
from penish.worker.jobs import HardError, Job, Pool, SoftError

log = logging.getLogger(__name__)

WEEK = timedelta(days=7)
WEEKLY_CHECK = 3600.0
# Calls in flight at once while judging a whole collection: the owner is waiting on the
# page, and a few at a time stays well inside any rate limit.
JUDGE_AT_ONCE = 4


def _int(job: Job, key: str) -> int:
    raw = job.payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise HardError(f"bad {job.type} payload: {key}")
    try:
        return int(raw)
    except ValueError:
        raise HardError(f"bad {job.type} payload: {key}") from None


async def ensure_anchors(
    pool: Pool, collection_id: int, settings: Settings, embedder: DocumentEmbedder
) -> int:
    """Embed every anchor that has no vector under the current model. Returns how many."""
    async with pool.connection() as conn:
        stale = await store.stale_anchors(conn, collection_id, settings)
    if not stale:
        return 0
    out = await embedder.embed_documents([r["anchor_text"] for r in stale])
    vecs = np.asarray(out, dtype=np.float32)
    if vecs.shape != (len(stale), settings.embed_dim):
        raise HardError(f"anchor embeddings have shape {vecs.shape}")
    n = 0
    async with pool.connection() as conn:
        for r, v in zip(stale, vecs, strict=True):
            if await store.store_anchor(
                conn, collection_id, r["number"], r["anchor_text"], v, settings
            ):
                n += 1
    return n


async def judge_filing(
    pool: Pool,
    image_id: int,
    collection_id: int,
    settings: Settings,
    client: Any = None,
    within: float = judge.TIMEOUT,
) -> judge.Verdict | None:
    """The judge's book for a picture about to be filed, asked with no lock held. None when
    there is nothing to ask (an owner's book, a picture already filed); a Failed when the
    judge is off or could not answer, which the filing records beside the scores. Never
    raises: filing does not depend on the judge."""
    try:
        async with pool.connection() as conn:
            inputs = await store.filing_inputs(conn, image_id, collection_id, settings)
        if inputs is None:
            return None
        if not await runtime.get(pool, runtime.JUDGE_KEY, settings=settings):
            return judge.Failed("off")
        texts, books = inputs
        if not texts.any():
            return judge.Failed("no text")
        return await judge.judged(
            pool,
            collection_id,
            image_id,
            texts,
            books,
            settings=settings,
            client=client,
            within=within,
        )
    except Exception as e:
        log.exception("collection %s: judging image %s failed", collection_id, image_id)
        return judge.Failed("error", detail=str(e)[:200])


async def handle_file(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    client: Any = None,
) -> None:
    image_id = _int(job, "image_id")
    if "line" in job.payload:
        # A sidecar that was malformed on arrival, retried from the queue page. The line
        # cannot have changed, so this says the same thing again unless the collection
        # it names has since appeared.
        line = str(job.payload["line"])
        try:
            req = parse_line(line)
        except ValueError as e:
            raise HardError(f"collection sidecar: {e}") from e
        async with pool.connection() as conn:
            collection = await store.collection_by_slug(conn, req.collection)
            if collection is None:
                raise HardError(f"collection sidecar: no collection {req.collection!r}")
            try:
                await store.request(conn, collection, image_id, req.book, req.position)
            except store.Refused as e:
                raise HardError(f"collection sidecar: {e}") from e
        return

    collection_id = _int(job, "collection_id")
    await ensure_anchors(pool, collection_id, settings, embedder)
    verdict = await judge_filing(pool, image_id, collection_id, settings, client)
    async with pool.connection() as conn:
        result = await store.file_request(conn, image_id, collection_id, settings, verdict)
    if result == "waiting":
        # Publish enqueues this again, but an embedding still on its way is not a publish.
        raise SoftError(f"image {image_id} is not live and embedded yet")
    if result not in ("filed", "gone"):
        # Kept on the request for the collection page; retrying alone cannot change it.
        log.warning("collection_file %s: %s", image_id, result)


async def handle_anchor(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    n = await ensure_anchors(pool, _int(job, "collection_id"), settings, embedder)
    log.info("collection anchors embedded: %d", n)


async def handle_resort(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    collection_id = _int(job, "collection_id")
    await ensure_anchors(pool, collection_id, settings, embedder)
    async with pool.connection() as conn:
        collection = await store.collection_by_id(conn, collection_id)
        if collection is None:
            return
        moves = await store.resort(conn, collection, settings, dry_run=False)
    log.info("collection %s re-sorted: %d moves", collection_id, len(moves))


async def handle_judge(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    client: Any = None,
) -> None:
    """A judgement for every leaf the owner's re-judging will ask about, so its dry run can
    plan from kept answers alone. A kept judgement or refusal is not asked again."""
    collection_id = _int(job, "collection_id")
    async with pool.connection() as conn:
        collection = await store.collection_by_id(conn, collection_id)
        if collection is None:
            return
        state = await store.load(conn, collection, settings)
        asks, _ = await store.questions(conn, state)
    gate = asyncio.Semaphore(JUDGE_AT_ONCE)

    async def one(a: store.Question) -> judge.Verdict:
        async with gate:
            return await judge.judged(
                pool, collection_id, a.image_id, a.texts, a.books, settings=settings, client=client
            )

    got = await asyncio.gather(*(one(a) for a in asks))
    failed = sum(1 for v in got if isinstance(v, judge.Failed))
    log.info("collection %s judged: %d leaves, %d failed", collection_id, len(got), failed)


async def enqueue_unfiled(pool: Pool) -> int:
    """Requests for live pictures with no job waiting: a crash between go-live and the
    enqueue, or a picture republished since. Run at worker start."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key)
            select '{store.FILE_JOB}',
                   jsonb_build_object('image_id', r.image_id, 'collection_id', r.collection_id),
                   '{store.FILE_JOB}:' || r.image_id || ':' || r.collection_id
              from collection_requests r
              join images i on i.id = r.image_id
             where i.status = 'live' and r.error is null
             order by r.created_at
            on conflict do nothing
            """
        )
        return cur.rowcount


async def enqueue_anchors(pool: Pool, settings: Settings) -> int:
    """One anchor job per collection with an anchor not embedded under today's model, so
    the seed anchors and a model swap are made good without waiting for a filing."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key)
            select distinct '{store.ANCHOR_JOB}',
                   jsonb_build_object('collection_id', b.collection_id),
                   '{store.ANCHOR_JOB}:' || b.collection_id
              from collection_books b
             where b.anchor_text <> ''
               and (b.anchor_embedding is null or b.anchor_model is distinct from %(m)s
                    or b.anchor_dim is distinct from %(d)s)
            on conflict do nothing
            """,
            {"m": settings.embed_model, "d": settings.embed_dim},
        )
        return cur.rowcount


async def enqueue_unnoted(pool: Pool) -> int:
    """A conservator's note for every leaf with none, the job filing would have queued.
    Run at worker start, so leaves filed before notes existed, or whose job was lost, are
    made good. A note of either source keeps a leaf out, and so does any note job on
    record for it, done or failed: the call is paid, a leaf every try refused would be paid
    for again at each restart, and the owner can still ask on the collection page."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key)
            select '{store.NOTE_JOB}',
                   jsonb_build_object('image_id', l.image_id, 'collection_id', l.collection_id,
                                      'rewrite', false),
                   '{store.NOTE_JOB}:' || l.image_id || ':' || l.collection_id
              from collection_leaves l
             where l.note is null
               and not exists (select 1 from jobs j
                                where j.type = '{store.NOTE_JOB}'
                                  and j.payload->>'image_id' = l.image_id::text
                                  and j.payload->>'collection_id' = l.collection_id::text)
             order by l.collection_id, l.book, l.position
            on conflict do nothing
            """
        )
        return cur.rowcount


async def enqueue_weekly(pool: Pool, now: datetime | None = None) -> int:
    """A re-sort for each collection whose tuning asks for one weekly and is due."""
    now = now or datetime.now(UTC)
    n = 0
    async with pool.connection() as conn:
        cur = await conn.execute("select id, tuning, last_resort_at from collections")
        for r in await cur.fetchall():
            if not Tuning.from_json(r["tuning"]).weekly_resort:
                continue
            if r["last_resort_at"] is not None and now - r["last_resort_at"] < WEEK:
                continue
            cur2 = await conn.execute(
                "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
                " on conflict do nothing",
                (
                    store.RESORT_JOB,
                    Jsonb({"collection_id": r["id"]}),
                    f"{store.RESORT_JOB}:{r['id']}",
                ),
            )
            n += cur2.rowcount
    return n


async def run_weekly(pool: Pool, stop: asyncio.Event, interval: float = WEEKLY_CHECK) -> None:
    while not stop.is_set():
        try:
            n = await enqueue_weekly(pool)
            if n:
                log.info("enqueued %d weekly re-sorts", n)
        except Exception:
            log.exception("weekly re-sort check failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
