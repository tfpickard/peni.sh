"""Phase 1 acceptance criteria, end to end through `python -m penish.worker`-style startup.

The worker runs as a real subprocess against the test database and a tmp DATA_DIR;
only the describer, haiku writer and embedder are faked (see fake_worker.py).
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
import pyvips
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FAKE_WORKER = ROOT / "tests" / "integration" / "fake_worker.py"
TIMEOUT = 30.0


def png(seed: int, w: int = 600, h: int = 400) -> bytes:
    arr = np.random.default_rng(seed).integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    out = pyvips.Image.new_from_memory(arr.tobytes(), w, h, 3, "uchar").write_to_buffer(".png")
    assert isinstance(out, bytes)
    return out


class Env:
    def __init__(self, dsn: str, data: Path) -> None:
        self.dsn = dsn
        self.data = data
        (data / "inbox").mkdir(parents=True, exist_ok=True)
        (data / "tmp").mkdir(parents=True, exist_ok=True)

    @property
    def inbox(self) -> Path:
        return self.data / "inbox"

    def drop(self, name: str, body: bytes | str) -> None:
        # Temp then rename on the same filesystem, the way rsync lands a file.
        tmp = self.data / "tmp" / name
        if isinstance(body, str):
            tmp.write_text(body)
        else:
            tmp.write_bytes(body)
        os.rename(tmp, self.inbox / name)

    def q(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            return list(conn.execute(sql, args).fetchall())

    def describe_calls(self) -> int:
        log = self.data / "describe_calls.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def image(self, stem: str) -> dict[str, Any] | None:
        rows = self.q("select * from images where stem = %s", stem)
        return rows[0] if rows else None

    def texts(self, image_id: int) -> dict[str, dict[str, Any]]:
        rows = self.q("select field, body, source from image_texts where image_id = %s", image_id)
        return {r["field"]: r for r in rows}

    def ingests_done(self) -> int:
        return int(
            self.q("select count(*) as n from jobs where type = 'ingest' and status = 'done'")[0][
                "n"
            ]
        )

    def processing_empty(self) -> bool:
        d = self.data / "processing"
        return d.is_dir() and not any(d.iterdir())


def wait_for(cond: Callable[[], bool], what: str, timeout: float = TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@contextmanager
def worker(env: Env, **extra: str) -> Iterator[subprocess.Popen[bytes]]:
    child_env = {
        **os.environ,
        "DATABASE_URL": env.dsn,
        "DATA_DIR": str(env.data),
        "SETTLE_SECONDS": "1",
        "PYTHONPATH": str(ROOT),
        # Ephemeral, so a dev box with something on 8001 does not matter.
        "EMBED_SERVER_PORT": "0",
        **extra,
    }
    log = (env.data / "worker.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_WORKER)], env=child_env, cwd=ROOT, stdout=log, stderr=log
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.close()


@pytest.fixture
def env(clean_db: str, tmp_path: Path) -> Env:
    return Env(clean_db, tmp_path)


def wait_live(env: Env, stem: str) -> dict[str, Any]:
    def live() -> bool:
        img = env.image(stem)
        return img is not None and img["status"] == "live"

    wait_for(live, f"{stem} to go live")
    img = env.image(stem)
    assert img is not None
    return img


def test_lone_image_goes_live_with_model_text(env: Env) -> None:
    with worker(env):
        env.drop("x.png", png(1))
        img = wait_live(env, "x")
        wait_for(env.processing_empty, "processing dir to empty")

        texts = env.texts(img["id"])
        assert texts["caption"]["source"] == "model"
        assert texts["caption"]["body"].startswith("Fake caption")
        assert texts["description"]["source"] == "model"
        assert texts["description"]["body"].startswith("Fake description")
        tags = env.q("select tag, source from tags where image_id = %s", img["id"])
        assert len(tags) == 6 and {t["source"] for t in tags} == {"model"}

        sha = img["sha256"]
        pub = env.data / "media" / "pub"
        assert list(pub.glob(f"{sha}-*.webp")) and list(pub.glob(f"{sha}-*.avif"))
        assert list((env.data / "media" / "orig").glob(f"{sha}.*"))
        assert env.describe_calls() == 1


def test_owner_sidecars_win_and_skip_the_describer(env: Env) -> None:
    with worker(env):
        env.drop("y_caption.txt", "Owner caption")
        env.drop("y_description.txt", "Owner description of the thing.")
        env.drop("y_tags.txt", "red\nblue\ngreen\n")
        env.drop("y.png", png(2))
        img = wait_live(env, "y")

        texts = env.texts(img["id"])
        assert texts["caption"] == {
            "field": "caption",
            "body": "Owner caption",
            "source": "owner",
        }
        assert texts["description"]["source"] == "owner"
        tags = env.q("select tag, source from tags where image_id = %s order by tag", img["id"])
        assert [t["tag"] for t in tags] == ["blue", "green", "red"]
        assert {t["source"] for t in tags} == {"owner"}
        assert env.describe_calls() == 0


def test_redrop_is_idempotent_and_new_caption_updates(env: Env) -> None:
    with worker(env):
        env.drop("x.png", png(1))
        first = wait_live(env, "x")
        wait_for(lambda: env.ingests_done() == 1, "first ingest job")

        env.drop("x.png", png(1))
        wait_for(lambda: env.ingests_done() == 2, "re-drop ingest job")
        wait_for(env.processing_empty, "processing dir to empty")
        assert len(env.q("select 1 from images")) == 1
        assert env.describe_calls() == 1

        env.drop("x_caption.txt", "Brand new words")
        env.drop("x.png", png(1))
        wait_for(lambda: env.ingests_done() == 3, "caption re-drop ingest job")
        wait_for(env.processing_empty, "processing dir to empty")

        assert len(env.q("select 1 from images")) == 1
        texts = env.texts(first["id"])
        assert texts["caption"]["body"] == "Brand new words"
        assert texts["caption"]["source"] == "owner"
        img = env.image("x")
        assert img is not None and img["slug"] == "brand-new-words"
        aliases = env.q("select slug from slug_aliases where image_id = %s", first["id"])
        assert [a["slug"] for a in aliases] == [first["slug"]]


def test_corrupt_file_lands_in_failed(env: Env) -> None:
    with worker(env):
        env.drop("z.png", b"\x89PNG\r\n\x1a\nnot really an image")
        err = env.data / "failed" / "z" / "z.err"
        wait_for(err.is_file, "z.err to appear")
        wait_for(env.processing_empty, "processing dir to empty")
        assert err.read_text().strip()
        assert (env.data / "failed" / "z" / "z.png").is_file()
        assert env.q("select 1 from images") == []


def test_sigkill_mid_job_then_restart_completes_once(env: Env) -> None:
    with worker(env, FAKE_DESCRIBE_SLEEP="5") as slow:
        env.drop("w.png", png(3))
        wait_for(
            lambda: bool(env.q("select 1 from jobs where type = 'ingest' and status = 'running'")),
            "ingest job to be running",
        )
        wait_for(lambda: env.describe_calls() >= 1, "describer to be entered")
        slow.send_signal(signal.SIGKILL)
        slow.wait(timeout=10)
    assert env.image("w") is None or env.image("w")["status"] != "live"  # type: ignore[index]

    with worker(env):
        img = wait_live(env, "w")
        wait_for(lambda: env.ingests_done() == 1, "ingest job to finish")
        wait_for(env.processing_empty, "processing dir to empty")
    assert len(env.q("select 1 from images")) == 1
    assert env.q("select count(*) as n from jobs where type = 'ingest'")[0]["n"] == 1
    assert env.texts(img["id"])["description"]["body"].startswith("Fake description")


def test_slow_in_place_write_is_processed_once_and_complete(env: Env) -> None:
    data = png(4, w=900, h=600)
    third = len(data) // 3
    chunks = [data[:third], data[third : 2 * third], data[2 * third :]]
    with worker(env):
        # scp writes straight into the destination, so the watcher sees a growing file.
        with (env.inbox / "s.png").open("wb") as f:
            for c in chunks:
                f.write(c)
                f.flush()
                time.sleep(0.4)
        img = wait_live(env, "s")
        wait_for(env.processing_empty, "processing dir to empty")
        assert img["sha256"] == hashlib.sha256(data).hexdigest()
        assert len(env.q("select 1 from images")) == 1
        assert env.q("select count(*) as n from jobs where type = 'ingest'")[0]["n"] == 1
        assert not (env.data / "failed").exists() or not any((env.data / "failed").iterdir())
