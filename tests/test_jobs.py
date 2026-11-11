from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from penish.worker import jobs
from penish.worker.jobs import Job, SoftError


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


async def _row(p: Any, job_id: int) -> dict[str, Any]:
    async with p.connection() as conn:
        cur = await conn.execute("select * from jobs where id = %s", (job_id,))
        return cast(dict[str, Any], await cur.fetchone())


async def test_enqueue_claim_once(p: Any) -> None:
    jid = await jobs.enqueue(p, "ingest", {"stem": "a"})
    job = await jobs.claim(p, ["ingest"])
    assert job is not None and job.id == jid
    assert job.payload == {"stem": "a"} and job.attempts == 1
    assert await jobs.claim(p, None) is None


async def test_claim_filters_by_type(p: Any) -> None:
    await jobs.enqueue(p, "haiku", {})
    assert await jobs.claim(p, ["ingest"]) is None
    assert await jobs.claim(p, ["haiku"]) is not None


async def test_concurrent_claimers_one_winner(p: Any) -> None:
    await jobs.enqueue(p, "ingest", {})
    results = await asyncio.gather(jobs.claim(p, None), jobs.claim(p, None))
    assert sum(r is not None for r in results) == 1


async def test_future_run_after_not_claimed(p: Any) -> None:
    await jobs.enqueue(p, "ingest", {}, run_after=datetime.now(UTC) + timedelta(hours=1))
    assert await jobs.claim(p, None) is None


async def test_fail_retry_schedules_backoff(p: Any) -> None:
    jid = await jobs.enqueue(p, "ingest", {})
    job = await jobs.claim(p, None)
    assert job is not None and job.attempts == 1
    await jobs.fail(p, job, "boom", retry=True, rng=lambda: 0.0)
    row = await _row(p, cast(int, jid))
    assert row["status"] == "queued" and row["last_error"] == "boom"
    async with p.connection() as conn:
        cur = await conn.execute(
            "select extract(epoch from run_after - now()) as s from jobs where id = %s", (jid,)
        )
        secs = float((await cur.fetchone())["s"])
    assert 110 < secs <= 120


async def test_fail_no_retry_and_max_attempts(p: Any) -> None:
    jid = cast(int, await jobs.enqueue(p, "ingest", {}))
    await jobs.fail(p, Job(jid, "ingest", {}, 1, None), "bad", retry=False)
    assert (await _row(p, jid))["status"] == "failed"
    jid2 = cast(int, await jobs.enqueue(p, "ingest", {}))
    await jobs.fail(p, Job(jid2, "ingest", {}, jobs.MAX_ATTEMPTS, None), "bad", retry=True)
    assert (await _row(p, jid2))["status"] == "failed"


def test_backoff_schedule_and_cap() -> None:
    assert jobs.backoff(1, lambda: 0.0) == timedelta(minutes=2)
    assert jobs.backoff(3, lambda: 0.0) == timedelta(minutes=8)
    assert jobs.backoff(9, lambda: 0.0) == timedelta(hours=6)
    assert jobs.backoff(1000, lambda: 0.0) == timedelta(hours=6)
    assert jobs.backoff(1, lambda: 1.0) == timedelta(minutes=2) * 1.1


async def test_recover_stale(p: Any) -> None:
    jid = cast(int, await jobs.enqueue(p, "ingest", {}))
    fresh = cast(int, await jobs.enqueue(p, "ingest", {}))
    await jobs.claim(p, None)
    await jobs.claim(p, None)
    async with p.connection() as conn:
        await conn.execute(
            "update jobs set locked_at = now() - interval '1 hour' where id = %s", (jid,)
        )
    assert await jobs.recover_stale(p) == 1
    assert (await _row(p, jid))["status"] == "queued"
    assert (await _row(p, fresh))["status"] == "running"


async def test_dedupe(p: Any) -> None:
    assert await jobs.enqueue(p, "ingest", {}, dedupe_key="k") is not None
    assert await jobs.enqueue(p, "ingest", {}, dedupe_key="k") is None
    job = await jobs.claim(p, None)
    assert job is not None
    await jobs.complete(p, job.id)
    # Finished jobs do not block a rerun.
    assert await jobs.enqueue(p, "ingest", {}, dedupe_key="k") is not None


async def test_run_loop_processes_then_stops(p: Any) -> None:
    for i in range(3):
        await jobs.enqueue(p, "ingest", {"i": i})
    stop = asyncio.Event()
    seen: list[int] = []

    async def handler(job: Job) -> None:
        seen.append(job.id)
        if len(seen) == 3:
            stop.set()

    await asyncio.wait_for(
        jobs.run_loop(p, {"ingest": handler}, stop=stop, idle_sleep=0.05), timeout=10
    )
    assert len(seen) == 3
    async with p.connection() as conn:
        cur = await conn.execute("select count(*) as n from jobs where status = 'done'")
        assert (await cur.fetchone())["n"] == 3


async def test_run_loop_soft_error_requeues(p: Any) -> None:
    jid = cast(int, await jobs.enqueue(p, "ingest", {}))
    stop = asyncio.Event()

    async def handler(job: Job) -> None:
        stop.set()
        raise SoftError("later")

    await asyncio.wait_for(
        jobs.run_loop(p, {"ingest": handler}, stop=stop, idle_sleep=0.05, concurrency=1),
        timeout=10,
    )
    row = await _row(p, jid)
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "later" in row["last_error"]
