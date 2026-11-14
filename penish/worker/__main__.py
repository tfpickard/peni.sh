"""Worker entrypoint: the inbox watcher, the job loop, the heartbeat, the mycelium tick and
the internal embed server, side by side in one process."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta
from pathlib import Path

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium.tick import run_loop as run_mycelium
from penish.worker import jobs
from penish.worker.admin_jobs import make_handlers
from penish.worker.categorize import enqueue_uncategorized, handle_categorize
from penish.worker.embed import Embedder
from penish.worker.embed_server import serve as serve_embed
from penish.worker.facet_jobs import enqueue_unsplit
from penish.worker.heartbeat import run_heartbeat
from penish.worker.ingest import handle_haiku, handle_ingest
from penish.worker.jobs import Job
from penish.worker.phrase_jobs import enqueue_unphrased
from penish.worker.watcher import InboxWatcher

log = logging.getLogger("penish.worker")


def _warm(embedder: Embedder) -> asyncio.Task[object]:
    # Loading the model takes tens of seconds; doing it now means the first
    # ingest does not pay for it while holding a job.
    async def go() -> object:
        try:
            await embedder.embed_documents(["warm"])
            log.info("embedder warm")
        except Exception:
            log.exception("embedder warmup failed; the first ingest will retry the load")
        return None

    return asyncio.create_task(go())


async def main() -> None:
    settings = get_settings()
    for d in (
        settings.inbox,
        settings.processing,
        settings.failed,
        settings.media_orig,
        settings.media_pub,
        settings.media_held,
    ):
        d.mkdir(parents=True, exist_ok=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    pool = make_pool(settings.database_url)
    await pool.open(wait=True)
    try:
        # This is the only worker process, so anything still running belongs to a
        # previous life of it; waiting out the usual 30 minutes would just stall those jobs.
        n = await jobs.recover_stale(pool, older_than=timedelta(0))
        if n:
            log.info("requeued %d jobs left running by a previous worker", n)

        embedder = Embedder()
        warm = _warm(embedder)

        async def enqueue(d: Path, stem: str) -> None:
            await jobs.enqueue(
                pool, "ingest", {"dir": str(d), "stem": stem}, dedupe_key=f"ingest:{d}"
            )

        async def has_job(d: Path) -> bool:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "select exists (select 1 from jobs where dedupe_key = %s"
                    " and status in ('queued', 'running')) as e",
                    (f"ingest:{d}",),
                )
                row = await cur.fetchone()
            return bool(row and row["e"])

        async def ingest(job: Job) -> None:
            await handle_ingest(job, pool=pool, settings=settings, embedder=embedder)

        async def haiku(job: Job) -> None:
            await handle_haiku(job, pool=pool, settings=settings)

        async def categorize(job: Job) -> None:
            await handle_categorize(job, pool=pool, settings=settings)

        n = await enqueue_uncategorized(pool)
        if n:
            log.info("enqueued %d pictures to categorize", n)
        n = await enqueue_unsplit(pool, settings)
        if n:
            log.info("enqueued %d pictures to split into facets", n)
        n = await enqueue_unphrased(pool, settings)
        if n:
            log.info("enqueued %d pictures to embed offers and needs", n)

        watcher = InboxWatcher(settings, enqueue, has_job=has_job)
        await watcher.recover()
        log.info("worker up; watching %s", settings.inbox)
        await asyncio.gather(
            watcher.run(stop),
            jobs.run_loop(
                pool,
                {
                    "ingest": ingest,
                    "haiku": haiku,
                    "categorize": categorize,
                    **make_handlers(pool, settings, embedder),
                },
                stop=stop,
                concurrency=2,
            ),
            run_heartbeat(pool, stop, version=settings.git_sha),
            run_mycelium(pool, stop),
            serve_embed(embedder, stop, port=settings.embed_server_port),
        )
        warm.cancel()
    finally:
        await pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
