"""Worker liveness for the admin system page."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from psycopg.types.json import Jsonb

from penish.config import get_settings
from penish.worker.jobs import Pool
from penish.worker.providers import keyed

log = logging.getLogger(__name__)

INTERVAL = 30.0


async def beat(
    pool: Pool, *, version: str, name: str = "worker", providers: list[str] | None = None
) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) as n from jobs where status = 'queued'")
        row = await cur.fetchone()
        queued = int(row["n"]) if row else 0
        info: dict[str, object] = {"version": version, "queue": queued}
        # Keys live in the worker's env, not the api's, so the admin learns which providers
        # can be called from here. Names only.
        if providers is not None:
            info["providers"] = providers
        await conn.execute(
            "insert into heartbeats (name, at, info) values (%s, now(), %s)"
            " on conflict (name) do update set at = excluded.at, info = excluded.info",
            (name, Jsonb(info)),
        )


async def run_heartbeat(
    pool: Pool, stop: asyncio.Event, *, version: str, interval: float = INTERVAL
) -> None:
    providers = keyed(get_settings())
    while not stop.is_set():
        try:
            await beat(pool, version=version, providers=providers)
        except Exception:
            # A missed beat is what the system page is for; it must not take the worker down.
            log.exception("heartbeat failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
