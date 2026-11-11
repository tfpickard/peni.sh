"""Worker liveness for the admin system page."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from psycopg.types.json import Jsonb

from penish.worker.jobs import Pool

log = logging.getLogger(__name__)

INTERVAL = 30.0


async def beat(pool: Pool, *, version: str, name: str = "worker") -> None:
    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) as n from jobs where status = 'queued'")
        row = await cur.fetchone()
        queued = int(row["n"]) if row else 0
        await conn.execute(
            "insert into heartbeats (name, at, info) values (%s, now(), %s)"
            " on conflict (name) do update set at = excluded.at, info = excluded.info",
            (name, Jsonb({"version": version, "queue": queued})),
        )


async def run_heartbeat(
    pool: Pool, stop: asyncio.Event, *, version: str, interval: float = INTERVAL
) -> None:
    while not stop.is_set():
        try:
            await beat(pool, version=version)
        except Exception:
            # A missed beat is what the system page is for; it must not take the worker down.
            log.exception("heartbeat failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
