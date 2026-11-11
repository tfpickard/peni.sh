"""Postgres-backed job queue: claim with SKIP LOCKED, retry with capped exponential backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

MAX_ATTEMPTS = 12
BACKOFF_CAP = timedelta(hours=6)
_JITTER = 0.10


class SoftError(Exception):
    """Transient failure: the job is retried with backoff."""


class HardError(Exception):
    """Permanent failure: retrying cannot help."""


@dataclass
class Job:
    id: int
    type: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None


def backoff(attempts: int, rng: Callable[[], float] = random.random) -> timedelta:
    # Clamp the exponent first so a huge attempts value cannot build a giant int.
    base = min(timedelta(minutes=2 ** min(attempts, 20)), BACKOFF_CAP)
    # Jitter spreads out jobs that failed together (e.g. an API outage) so they
    # do not all retry in the same instant.
    return base * (1 + _JITTER * rng())


async def enqueue(
    pool: Pool,
    type: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> int | None:
    # The partial unique index on dedupe_key turns a duplicate into a no-op.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into jobs (type, payload, dedupe_key, run_after)"
            " values (%s, %s, %s, coalesce(%s, now()))"
            " on conflict do nothing returning id",
            (type, Jsonb(payload), dedupe_key, run_after),
        )
        row = await cur.fetchone()
    return None if row is None else int(row["id"])


async def claim(pool: Pool, types: list[str] | None = None) -> Job | None:
    # One statement: the row lock from the subselect is held until the update commits,
    # so concurrent claimers skip each other's candidate instead of blocking.
    type_filter = "and type = any(%(types)s)" if types is not None else ""
    sql = f"""
        update jobs set status = 'running', locked_at = now(),
               attempts = attempts + 1, updated_at = now()
        where id = (
            select id from jobs
            where status = 'queued' and run_after <= now() {type_filter}
            order by run_after, id
            for update skip locked
            limit 1
        )
        returning id, type, payload, attempts, last_error
    """  # noqa: S608 -- type_filter is a fixed literal
    async with pool.connection() as conn:
        cur = await conn.execute(sql, {"types": types})
        row = await cur.fetchone()
    if row is None:
        return None
    return Job(
        id=row["id"],
        type=row["type"],
        payload=row["payload"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


async def complete(pool: Pool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set status = 'done', locked_at = null, updated_at = now() where id = %s",
            (job_id,),
        )


async def fail(
    pool: Pool,
    job: Job,
    error: str,
    *,
    retry: bool,
    rng: Callable[[], float] = random.random,
) -> None:
    async with pool.connection() as conn:
        if retry and job.attempts < MAX_ATTEMPTS:
            await conn.execute(
                "update jobs set status = 'queued', locked_at = null, last_error = %s,"
                " run_after = now() + %s, updated_at = now() where id = %s",
                (error, backoff(job.attempts, rng), job.id),
            )
        else:
            await conn.execute(
                "update jobs set status = 'failed', locked_at = null, last_error = %s,"
                " updated_at = now() where id = %s",
                (error, job.id),
            )


async def recover_stale(pool: Pool, older_than: timedelta = timedelta(minutes=30)) -> int:
    # A SIGKILLed worker never gets to call fail(), so its rows would stay running forever.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "update jobs set status = 'queued', locked_at = null, updated_at = now()"
            " where status = 'running' and locked_at < now() - %s",
            (older_than,),
        )
        return cur.rowcount


async def _run_one(pool: Pool, job: Job, handler: Callable[[Job], Awaitable[None]]) -> None:
    try:
        await handler(job)
    except HardError as e:
        log.error("job %s (%s) failed permanently: %s", job.id, job.type, e, exc_info=True)
        await fail(pool, job, repr(e), retry=False)
    except Exception as e:
        # SoftError and anything unexpected: a bug in a handler should not drop the job.
        log.error("job %s (%s) failed, will retry: %s", job.id, job.type, e, exc_info=True)
        await fail(pool, job, repr(e), retry=True)
    else:
        await complete(pool, job.id)


async def _worker(
    pool: Pool,
    handlers: dict[str, Callable[[Job], Awaitable[None]]],
    stop: asyncio.Event,
    idle_sleep: float,
) -> None:
    types = list(handlers)
    while not stop.is_set():
        job: Job | None = None
        try:
            job = await claim(pool, types)
            if job is not None:
                await _run_one(pool, job, handlers[job.type])
                continue
        except Exception:
            # DB hiccups must not kill the loop; fall through to the sleep and retry.
            log.exception("job loop error")
        # Waiting on the stop event (not sleep) makes shutdown immediate.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=idle_sleep)


async def run_loop(
    pool: Pool,
    handlers: dict[str, Callable[[Job], Awaitable[None]]],
    *,
    stop: asyncio.Event,
    idle_sleep: float = 1.0,
    concurrency: int = 2,
) -> None:
    await asyncio.gather(*(_worker(pool, handlers, stop, idle_sleep) for _ in range(concurrency)))
