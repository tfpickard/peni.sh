"""Inbox watcher: wait for a stem's files to settle, then claim them into processing/.

watchfiles reports added/modified/deleted, never close_write, so "done writing"
is inferred: a stem is ready once nothing has touched any of its files for
settle_seconds and every file's size and mtime match across two checks in a row.
That also covers scp, which writes in place under the final name.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from watchfiles import awatch

from penish.config import Settings
from penish.worker.sidecars import stem_of

log = logging.getLogger(__name__)

Enqueue = Callable[[Path, str], Awaitable[None]]
HasJob = Callable[[Path], Awaitable[bool]]

# Sidecars whose image never shows up are parked in failed/ after this long.
ORPHAN_SECONDS = 3600.0

Snapshot = frozenset[tuple[str, int, int]]


class Clock(Protocol):
    # Wall time, not monotonic: it is compared against file mtimes.
    def time(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class _Files:
    names: list[str]
    snapshot: Snapshot
    newest_mtime: float
    has_image: bool


class InboxWatcher:
    def __init__(
        self,
        settings: Settings,
        enqueue: Enqueue,
        *,
        has_job: HasJob | None = None,
        clock: Clock | None = None,
        settle_seconds: float | None = None,
        orphan_seconds: float = ORPHAN_SECONDS,
    ) -> None:
        self.inbox = settings.inbox
        self.processing = settings.processing
        self.failed = settings.failed
        self.enqueue = enqueue
        self.has_job = has_job
        self.clock: Clock = clock or SystemClock()
        # Settings.settle_seconds is an int; tests need sub-second settling.
        self.settle = float(settings.settle_seconds if settle_seconds is None else settle_seconds)
        self.orphan_seconds = orphan_seconds
        self.tick = max(self.settle / 3, 0.05)
        self._last_event: dict[str, float] = {}
        self._prev: dict[str, Snapshot] = {}
        for d in (self.inbox, self.processing, self.failed):
            d.mkdir(parents=True, exist_ok=True)

    # -- startup ---------------------------------------------------------

    async def recover(self) -> None:
        """Re-enqueue orphaned processing dirs, then claim whatever already settled in the inbox.

        Call before run(). Stems still being written are left for run() to pick up.
        """
        for d in sorted(self.processing.iterdir()):
            if not d.is_dir():
                continue
            stem = _dir_stem(d)
            if stem is None:
                # A crash between mkdir and the first rename leaves an empty dir;
                # the files are still in the inbox and will be claimed from there.
                if not any(d.iterdir()):
                    d.rmdir()
                    continue
                stem = d.name
            if self.has_job is not None and await self.has_job(d):
                continue
            log.info("re-enqueueing orphan %s (%s)", d.name, stem)
            await self._enqueue(d, stem)

        # Two passes give every stem its two size/mtime checks; settling by
        # mtime age means files that landed while we were down need no event.
        await self.scan()
        await self.clock.sleep(self.tick)
        await self.scan()

    # -- steady state ----------------------------------------------------

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._watch(stop))
            tg.create_task(self._poll(stop))

    async def _watch(self, stop: asyncio.Event) -> None:
        # No watch_filter: stem_of is the single source of truth for what counts.
        # Events only ever delay a claim; scan() decides, so a missed event is harmless.
        async for changes in awatch(
            self.inbox, stop_event=stop, recursive=False, watch_filter=None
        ):
            now = self.clock.time()
            for _, path in changes:
                stem, _kind = stem_of(path)
                if stem is not None:
                    self._last_event[stem] = now

    async def _poll(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.scan()
            except Exception:
                # A transient fs error must not kill the watcher; the next tick retries.
                log.exception("inbox scan failed")
            await self.clock.sleep(self.tick)

    async def scan(self) -> None:
        """One settle check over every stem in the inbox."""
        groups = self._inbox_groups()
        now = self.clock.time()
        for stem in list(self._prev):
            if stem not in groups:
                self._forget(stem)
        for stem, files in groups.items():
            prev = self._prev.get(stem)
            self._prev[stem] = files.snapshot
            last_touch = max(self._last_event.get(stem, -math.inf), files.newest_mtime)
            age = now - last_touch
            if not files.has_image:
                if age >= self.orphan_seconds:
                    self._fail_orphan(stem, files.names)
                continue
            if age >= self.settle and prev == files.snapshot:
                await self._claim(stem, files.names)

    def _inbox_groups(self) -> dict[str, _Files]:
        names: dict[str, list[str]] = defaultdict(list)
        has_image: dict[str, bool] = defaultdict(bool)
        for entry in os.scandir(self.inbox):
            stem, kind = stem_of(entry.name)
            if stem is None or not entry.is_file(follow_symlinks=False):
                continue
            names[stem].append(entry.name)
            has_image[stem] |= kind == "image"
        out: dict[str, _Files] = {}
        for stem, ns in names.items():
            stats: list[tuple[str, int, int]] = []
            for n in ns:
                try:
                    st = os.stat(self.inbox / n)
                except FileNotFoundError:
                    # Renamed away mid-scan; the next tick sees the new layout.
                    continue
                stats.append((n, st.st_size, st.st_mtime_ns))
            if not stats:
                continue
            out[stem] = _Files(
                names=[s[0] for s in stats],
                snapshot=frozenset(stats),
                newest_mtime=max(s[2] for s in stats) / 1e9,
                has_image=has_image[stem],
            )
        return out

    async def _claim(self, stem: str, names: list[str]) -> None:
        dest = self.processing / str(uuid.uuid4())
        dest.mkdir()
        for n in names:
            os.rename(self.inbox / n, dest / n)
        self._forget(stem)
        log.info("claimed %s into %s", stem, dest.name)
        await self._enqueue(dest, stem)

    async def _enqueue(self, d: Path, stem: str) -> None:
        try:
            await self.enqueue(d, stem)
        except Exception:
            # The dir stays in processing/; recover() re-enqueues it on next start.
            log.exception("enqueue failed for %s (%s)", d.name, stem)

    def _fail_orphan(self, stem: str, names: list[str]) -> None:
        dest = self.failed / stem
        if dest.exists():
            dest = self.failed / f"{stem}-{uuid.uuid4().hex[:8]}"
        dest.mkdir(parents=True)
        for n in names:
            os.rename(self.inbox / n, dest / n)
        (dest / f"{stem}.err").write_text(
            f"no image arrived within {self.orphan_seconds:.0f}s of the sidecars\n"
        )
        self._forget(stem)
        log.warning("sidecars for %s had no image; moved to %s", stem, dest)

    def _forget(self, stem: str) -> None:
        self._prev.pop(stem, None)
        self._last_event.pop(stem, None)


def _dir_stem(d: Path) -> str | None:
    fallback = None
    for p in sorted(d.iterdir()):
        stem, kind = stem_of(p.name)
        if kind == "image":
            return stem
        fallback = fallback or stem
    return fallback
