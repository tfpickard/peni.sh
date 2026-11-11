from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from penish.config import Settings
from penish.worker.watcher import InboxWatcher

SETTLE = 0.3


def _contents(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, dict[str, bytes]]] = []
        self.called = asyncio.Event()

    async def __call__(self, d: Path, stem: str) -> None:
        # Snapshot contents at enqueue time: that is what the ingest job would read.
        self.calls.append((d, stem, _contents(d)))
        self.called.set()

    async def first(self) -> None:
        await asyncio.wait_for(self.called.wait(), 5)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
async def running(settings: Settings, rec: Recorder) -> AsyncIterator[InboxWatcher]:
    w = InboxWatcher(settings, rec, settle_seconds=SETTLE)
    await w.recover()
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    try:
        yield w
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_slow_write_and_late_sidecar(running: InboxWatcher, rec: Recorder) -> None:
    inbox = running.inbox
    chunks = [os.urandom(64 * 1024) for _ in range(3)]
    # scp-style: final name, written in place, pauses shorter than the settle time.
    with (inbox / "x.png").open("wb") as f:  # noqa: ASYNC230 -- tiny local writes
        for c in chunks:
            f.write(c)
            f.flush()
            await asyncio.sleep(SETTLE / 3)
    await asyncio.sleep(SETTLE / 2)
    (inbox / "x_caption.txt").write_text("late")

    await rec.first()
    await asyncio.sleep(SETTLE * 4)
    assert len(rec.calls) == 1
    d, stem, files = rec.calls[0]
    assert stem == "x"
    assert d.parent == running.processing
    assert files == {"x.png": b"".join(chunks), "x_caption.txt": b"late"}
    assert list(inbox.iterdir()) == []


async def test_lone_image_claimed_once(running: InboxWatcher, rec: Recorder) -> None:
    (running.inbox / "solo.jpg").write_bytes(b"jpg")
    await rec.first()
    await asyncio.sleep(SETTLE * 4)
    assert [(c[1], c[2]) for c in rec.calls] == [("solo", {"solo.jpg": b"jpg"})]


async def test_temp_dotfile_never_claimed(running: InboxWatcher, rec: Recorder) -> None:
    tmp = running.inbox / ".x.png.AbC123"
    tmp.write_bytes(b"partial")
    (running.inbox / "x.png~").write_bytes(b"backup")
    await asyncio.sleep(SETTLE * 6)
    assert rec.calls == []
    assert tmp.exists()


async def test_sidecar_waits_for_image(running: InboxWatcher, rec: Recorder) -> None:
    (running.inbox / "late_caption.txt").write_text("c")
    await asyncio.sleep(SETTLE * 4)
    assert rec.calls == []
    (running.inbox / "late.png").write_bytes(b"p")
    await rec.first()
    assert set(rec.calls[0][2]) == {"late.png", "late_caption.txt"}


async def test_old_orphan_sidecar_goes_to_failed(settings: Settings, rec: Recorder) -> None:
    w = InboxWatcher(settings, rec, settle_seconds=SETTLE, orphan_seconds=3600)
    side = w.inbox / "gone_tags.txt"
    side.write_text("a")
    old = time.time() - 7200
    os.utime(side, (old, old))
    await w.recover()
    assert rec.calls == []
    assert sorted(p.name for p in (w.failed / "gone").iterdir()) == ["gone.err", "gone_tags.txt"]


async def test_recover_reenqueues_orphan_dirs(settings: Settings, rec: Recorder) -> None:
    w = InboxWatcher(settings, rec, settle_seconds=SETTLE)
    orphan = w.processing / "aaaa"
    orphan.mkdir()
    (orphan / "o.png").write_bytes(b"o")
    (orphan / "o_caption.txt").write_text("c")
    queued = w.processing / "bbbb"
    queued.mkdir()
    (queued / "q.png").write_bytes(b"q")
    empty = w.processing / "cccc"
    empty.mkdir()

    async def has_job(d: Path) -> bool:
        return d == queued

    w.has_job = has_job
    await w.recover()
    assert [(c[0], c[1]) for c in rec.calls] == [(orphan, "o")]
    assert not empty.exists()


async def test_recover_claims_settled_inbox_without_events(
    settings: Settings, rec: Recorder
) -> None:
    w = InboxWatcher(settings, rec, settle_seconds=SETTLE)
    img = w.inbox / "while_down.webp"
    img.write_bytes(b"w")
    old = time.time() - 60
    os.utime(img, (old, old))
    await w.recover()
    assert [c[1] for c in rec.calls] == ["while_down"]


async def test_enqueue_failure_leaves_dir(settings: Settings) -> None:
    async def boom(d: Path, stem: str) -> None:
        raise RuntimeError("db down")

    w = InboxWatcher(settings, boom, settle_seconds=SETTLE)
    img = w.inbox / "z.png"
    img.write_bytes(b"z")
    old = time.time() - 60
    os.utime(img, (old, old))
    await w.recover()
    dirs = list(w.processing.iterdir())
    assert len(dirs) == 1 and (dirs[0] / "z.png").exists()
