from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest

from penish import logs, runtime
from penish.worker import embed_server, jobs

# Nothing listens on port 1, so a connect fails at once instead of timing out.
DEAD_DSN = "postgresql://x:y@127.0.0.1:1/x"
SECRET = "sk-ant-api03-supersecretvalue1234567890"


def rows(dsn: str, where: str = "true", *args: Any) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        cur = conn.execute(
            f"select * from app_log where {where} order by id",  # noqa: S608
            args,  # type: ignore[arg-type]
        )
        return cur.fetchall()


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def test_logger() -> Iterator[logging.Logger]:
    """A logger of its own, so these tests neither read nor disturb the root's handlers."""
    lg = logging.getLogger("penish.test_logs")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    yield lg
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.setLevel(logging.NOTSET)
    lg.propagate = True


def handler(dsn: str, **kwargs: Any) -> logs.DbHandler:
    kwargs.setdefault("interval", 0.05)
    kwargs.setdefault("max_backoff", 0.1)
    kwargs.setdefault("follow_settings", False)
    h = logs.DbHandler(dsn, "worker", **kwargs)
    h.setLevel(logging.DEBUG)
    return h


# -- never in the way --------------------------------------------------------


def test_never_blocks_or_raises_when_db_down_and_queue_full(
    test_logger: logging.Logger,
) -> None:
    h = handler(DEAD_DSN, capacity=5)
    test_logger.addHandler(h)
    h.start()
    try:
        started = time.monotonic()
        for i in range(1000):
            test_logger.info("line %d", i)
        assert time.monotonic() - started < 1.0
        # At most the queue and one pending batch survive; the rest is counted.
        assert h.dropped >= 1000 - 2 * 5
    finally:
        h.stop(timeout=2)


def test_bad_record_is_a_drop_not_an_exception(test_logger: logging.Logger) -> None:
    h = handler(DEAD_DSN)
    # Wrong number of args: formatting raises inside emit.
    record = test_logger.makeRecord(test_logger.name, logging.INFO, "f", 1, "%s %s", ("a",), None)
    h.handle(record)
    assert h.dropped == 1


def test_outage_survived_and_drops_reported(clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(DEAD_DSN, capacity=3)
    test_logger.addHandler(h)
    h.start()
    try:
        for i in range(10):
            test_logger.warning("during outage %d", i)
        wait_for(lambda: h.dropped > 0)
        # The database comes back.
        h.dsn = clean_db
        wait_for(lambda: any(r["logger"] == logs.DROP_LOGGER for r in rows(clean_db)))
        test_logger.warning("after outage")
        wait_for(lambda: bool(rows(clean_db, "message = 'after outage'")))
    finally:
        h.stop()
    got = rows(clean_db)
    kept = [r for r in got if r["message"].startswith("during outage")]
    report = [r for r in got if r["logger"] == logs.DROP_LOGGER]
    assert len(kept) + sum(r["context"]["dropped"] for r in report) == 10
    assert report[0]["level"] == logging.WARNING
    assert report[0]["message"] == f"dropped {report[0]['context']['dropped']} log lines"
    assert h.dropped == 0


# -- what is written ----------------------------------------------------------


def test_batch_insert_fields(clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(clean_db)
    test_logger.addHandler(h)
    h.start()
    test_logger.info("plain %s", "line")
    try:
        raise ValueError("boom")
    except ValueError:
        test_logger.exception("it broke")
    test_logger.warning("nul \x00 inside")
    h.stop()
    got = rows(clean_db)
    assert [r["message"] for r in got] == ["plain line", "it broke", "nul \\0 inside"]
    plain, broke, _ = got
    assert plain["source"] == "worker" and plain["logger"] == "penish.test_logs"
    assert plain["level"] == logging.INFO and plain["exc"] is None and plain["context"] is None
    assert broke["level"] == logging.ERROR
    assert "Traceback" in broke["exc"] and "ValueError: boom" in broke["exc"]
    assert abs(time.time() - plain["at"].timestamp()) < 30


def test_context_from_contextvars_and_extra(clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(clean_db)
    test_logger.addHandler(h)
    h.start()
    with logs.context(job_id=7, job_type="haiku", attempt=2, image_id=None):
        with logs.model_context(purpose="haiku", model="openai:gpt-6-luna"):
            assert logs.current_context()["provider"] == "openai"
            test_logger.warning("inside", extra={"stop_reason": "refusal"})
        test_logger.info("job only")
    with logs.model_context("describe", "claude-sonnet-5-5"):
        test_logger.info("anthropic")
    test_logger.info("outside")
    assert logs.current_context() == {}
    h.stop()
    by = {r["message"]: r["context"] for r in rows(clean_db)}
    assert by["inside"] == {
        "job_id": 7,
        "job_type": "haiku",
        "attempt": 2,
        "purpose": "haiku",
        "model": "openai:gpt-6-luna",
        "provider": "openai",
        "stop_reason": "refusal",
    }
    assert by["job only"] == {"job_id": 7, "job_type": "haiku", "attempt": 2}
    assert by["anthropic"]["provider"] == "anthropic"
    assert by["outside"] is None


async def test_context_follows_tasks(clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(clean_db)
    test_logger.addHandler(h)
    h.start()

    async def child() -> None:
        await asyncio.sleep(0)
        test_logger.info("child")

    with logs.context(job_id=1):
        task = asyncio.create_task(child())
    with logs.context(job_id=2):
        await asyncio.to_thread(test_logger.info, "thread")
    await task
    h.stop()
    by = {r["message"]: r["context"] for r in rows(clean_db)}
    assert by == {"child": {"job_id": 1}, "thread": {"job_id": 2}}


def test_secrets_and_image_data_never_stored(clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(clean_db, secrets=(SECRET, "hunter2-hunter2-hunter2"))
    test_logger.addHandler(h)
    h.start()
    b64 = "iVBORw0KGgo" + "A" * 4000
    test_logger.warning("key %s and password hunter2-hunter2-hunter2", SECRET)
    test_logger.warning("headers {'Authorization': 'Bearer abc.def.ghi', 'x-api-key': 'k123'}")
    test_logger.warning("other key sk-proj-ABCDEFGHIJKLMNOPQRSTUV in text")
    test_logger.warning("image data:image/png;base64,%s end", b64)
    test_logger.warning('block {"type": "base64", "data": "%s"}', b64)
    test_logger.warning("ctx", extra={"context": {"body": b64, "key": SECRET}})
    try:
        raise RuntimeError(f"auth failed for {SECRET}")
    except RuntimeError:
        test_logger.exception("exc")
    h.stop()
    got = rows(clean_db)
    assert len(got) == 7
    blob = repr(got)
    for leak in (SECRET, "hunter2-hunter2-hunter2", "abc.def.ghi", "k123", "ABCDEFGHIJKLMNOP"):
        assert leak not in blob
    assert "AAAAAAAAAAAAAAAAAAAA" not in blob
    assert "<image image/png" in got[3]["message"]
    assert got[5]["context"]["body"].startswith("<base64 ")
    assert "<secret>" in got[6]["exc"]


def test_noisy_library_debug_is_not_kept(clean_db: str) -> None:
    h = handler(clean_db)
    lg = logging.getLogger("anthropic._base_client")
    record = lg.makeRecord(lg.name, logging.DEBUG, "f", 1, "Request options: %s", ("x",), None)
    h.handle(record)
    assert h._queue.qsize() == 0
    record = lg.makeRecord(lg.name, logging.WARNING, "f", 1, "retrying", None, None)
    h.handle(record)
    assert h._queue.qsize() == 1


# -- levels ---------------------------------------------------------------------


async def test_level_follows_runtime_key(p: Any, clean_db: str) -> None:
    root = logging.getLogger()
    before_root = root.level
    root.setLevel(logging.INFO)
    httpx_before = logging.getLogger("httpx").level
    h = logs.install(clean_db, "worker", interval=0.05, settings_interval=0.05)
    try:
        assert logs.installed() is h
        await asyncio.to_thread(wait_for, lambda: h.level == logging.INFO)
        await runtime.put(p, "log.level", "debug")
        await asyncio.to_thread(wait_for, lambda: h.level == logging.DEBUG)
        assert h.level_name == "debug"
        # The root has to let debug through for the handler to see it.
        assert root.getEffectiveLevel() == logging.DEBUG
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.INFO
        logging.getLogger("penish.test_logs").debug("verbose now")
        await runtime.put(p, "log.level", "error")
        await asyncio.to_thread(wait_for, lambda: h.level == logging.ERROR)
        assert root.level == logging.INFO
        logging.getLogger("penish.test_logs").warning("below error")
        logging.getLogger("penish.test_logs").error("at error")
        # Reset (the admin's DELETE) removes the row; the handler falls back to the default.
        await runtime.reset(p, "log.level")
        await asyncio.to_thread(wait_for, lambda: h.level == logging.INFO)
        assert h.level_name == "info"
        assert root.level == logging.INFO
    finally:
        logs.uninstall()
        test_root = root.level
        root.setLevel(before_root)
    assert logs.installed() is None
    assert test_root == logging.INFO
    assert logging.getLogger("httpx").level == httpx_before
    assert h not in root.handlers
    mine = [r["message"] for r in rows(clean_db, "logger = 'penish.test_logs'")]
    assert mine == ["verbose now", "at error"]


def test_runtime_keys_validate() -> None:
    assert runtime.default("log.level") == "info"
    assert runtime.default("log.keep_days") == 14
    for good in runtime.LOG_LEVELS:
        assert runtime.validate("log.level", good) == good
    for bad in ("verbose", "INFO", "", None, 10):
        with pytest.raises(ValueError):
            runtime.validate("log.level", bad)
    assert runtime.validate("log.keep_days", 1) == 1
    assert runtime.validate("log.keep_days", 365) == 365
    for bad_days in (0, 366, True, "14", 1.5, None):
        with pytest.raises(ValueError):
            runtime.validate("log.keep_days", bad_days)


# -- retention ------------------------------------------------------------------


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


async def _insert(p: Any, n: int, days_ago: int) -> None:
    async with p.connection() as conn:
        await conn.execute(
            "insert into app_log (at, source, level, logger, message)"
            " select now() - make_interval(days => %s), 'worker', 20, 't', 'm' || g"
            " from generate_series(1, %s) g",
            (days_ago, n),
        )


async def test_prune_by_age_and_cap(p: Any) -> None:
    await _insert(p, 4, 20)
    await _insert(p, 5, 1)
    assert await logs.prune(p, 14, cap=100) == 4
    assert await logs.prune(p, 14, cap=3) == 2
    async with p.connection() as conn:
        cur = await conn.execute("select message from app_log order by id")
        left = [r["message"] for r in await cur.fetchall()]
    assert left == ["m3", "m4", "m5"]
    assert await logs.prune(p, 14, cap=3) == 0


async def test_prune_loop_reads_keep_days(p: Any) -> None:
    await _insert(p, 2, 5)
    await _insert(p, 1, 1)
    await runtime.put(p, "log.keep_days", 3)
    stop = asyncio.Event()
    task = asyncio.create_task(logs.run_prune_loop(p, stop, interval=60))
    for _ in range(100):
        async with p.connection() as conn:
            cur = await conn.execute("select count(*) as n from app_log where logger = 't'")
            if (await cur.fetchone())["n"] == 1:
                break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 2)
    async with p.connection() as conn:
        cur = await conn.execute("select count(*) as n from app_log where logger = 't'")
        assert (await cur.fetchone())["n"] == 1


# -- jobs -------------------------------------------------------------------------


async def test_run_one_sets_job_context(p: Any, clean_db: str, test_logger: logging.Logger) -> None:
    h = handler(clean_db)
    logging.getLogger("penish.worker.jobs").addHandler(h)
    h.start()
    seen: list[dict[str, Any]] = []

    async def go(job: jobs.Job) -> None:
        seen.append(logs.current_context())
        raise jobs.HardError("bad")

    try:
        await jobs.enqueue(p, "crop", {"image_id": 41})
        job = await jobs.claim(p, ["crop"])
        assert job is not None
        await jobs._run_one(p, job, go)
        await jobs.enqueue(p, "grow_haikus", {"n": 3})
        job2 = await jobs.claim(p, ["grow_haikus"])
        assert job2 is not None
        await jobs._run_one(p, job2, go)
        # Payloads written by hand or by an older enqueue may carry the id as a string.
        await jobs.enqueue(p, "crop", {"image_id": "42"})
        job3 = await jobs.claim(p, ["crop"])
        assert job3 is not None
        await jobs._run_one(p, job3, go)
    finally:
        logging.getLogger("penish.worker.jobs").removeHandler(h)
        h.stop()
    assert seen[0] == {"job_id": job.id, "job_type": "crop", "attempt": 1, "image_id": 41}
    assert "image_id" not in seen[1]
    assert seen[2]["image_id"] == 42
    failed = rows(clean_db, "context->>'job_id' = %s", str(job.id))
    assert len(failed) == 1 and "failed permanently" in failed[0]["message"]
    assert failed[0]["context"]["image_id"] == 41
    assert logs.current_context() == {}


# -- bad rows ---------------------------------------------------------------------


def _plain(message: str, context: dict[str, Any] | None = None) -> logs.Row:
    return (datetime.now(UTC), "worker", logging.INFO, "t.rows", message, None, context)


def test_surrogate_in_a_line_is_stored(clean_db: str, test_logger: logging.Logger) -> None:
    # What os.fsdecode makes of a Latin-1 file name on Linux.
    name = os.fsdecode(b"IMG_\xff.jpg")
    h = handler(clean_db)
    test_logger.addHandler(h)
    h.start()
    test_logger.info("ingest %s", name)
    test_logger.warning("ingest of %s failed", name, extra={"path": name, name: 1})
    test_logger.info("after")
    h.stop()
    got = rows(clean_db)
    assert [r["message"] for r in got] == [
        "ingest IMG_\\udcff.jpg",
        "ingest of IMG_\\udcff.jpg failed",
        "after",
    ]
    assert got[1]["context"] == {"path": "IMG_\\udcff.jpg", "IMG_\\udcff.jpg": 1}
    assert h.dropped == 0


def test_unencodable_row_costs_only_itself(clean_db: str) -> None:
    # Past scrub (a future field it misses): the client's encoder raises, not Postgres.
    h = handler(clean_db)
    h._write([_plain("good 1"), _plain("bad \udcff"), _plain("good 2")])
    assert [r["message"] for r in rows(clean_db)] == ["good 1", "good 2"]
    assert h.dropped == 1


def test_refused_row_is_dropped_alone_and_reported(
    clean_db: str, test_logger: logging.Logger
) -> None:
    h = handler(clean_db)
    test_logger.addHandler(h)
    h.start()
    test_logger.info("before")
    # json lets NaN through; jsonb refuses it.
    test_logger.warning("nan", extra={"score": float("nan")})
    test_logger.info("after")
    wait_for(lambda: bool(rows(clean_db, "logger = %s", logs.DROP_LOGGER)))
    h.stop()
    got = rows(clean_db)
    assert [r["message"] for r in got if r["logger"] == test_logger.name] == ["before", "after"]
    report = [r for r in got if r["logger"] == logs.DROP_LOGGER]
    assert [r["context"]["dropped"] for r in report] == [1]
    assert h.dropped == 0


class _Flaky:
    """A connection that goes away on its nth execute, as a Postgres restart would."""

    def __init__(self, conn: psycopg.Connection[Any], fail_at: int) -> None:
        self.conn = conn
        self.fail_at = fail_at
        self.n = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.n += 1
        if self.n == self.fail_at:
            raise psycopg.OperationalError("server closed the connection")
        return self.conn.execute(*args, **kwargs)


def test_outage_during_row_fallback_writes_nothing_twice(clean_db: str) -> None:
    h = handler(clean_db)
    real = h._connection()
    flaky = _Flaky(real, fail_at=3)
    h._connection = lambda: flaky  # type: ignore[method-assign]
    batch = [_plain("one"), _plain("nan", {"score": float("nan")}), _plain("two"), _plain("three")]
    pending = list(batch)
    # The batch fails on the NaN row, the one-by-one fallback loses the connection on "two".
    assert h.flush_once(pending) is False
    assert rows(clean_db) == []
    assert h.dropped == 0 and len(pending) == 4
    # The failed flush closed the connection; the retry opens a fresh one.
    del h._connection
    assert real.closed
    assert h.flush_once(pending) is True
    assert h.flush_once(pending) is True
    got = rows(clean_db)
    assert [r["message"] for r in got if r["logger"] == "t.rows"] == ["one", "two", "three"]
    assert [r["context"]["dropped"] for r in got if r["logger"] == logs.DROP_LOGGER] == [1]
    assert h.dropped == 0
    h._drop_connection()


# -- installed in a process ---------------------------------------------------------


@pytest.fixture
def uvicorn_loggers() -> Iterator[None]:
    """uvicorn.Config sets levels, handlers and propagate on its loggers globally."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    saved = {n: logging.getLogger(n) for n in names}
    state = {n: (lg.level, list(lg.handlers), lg.propagate) for n, lg in saved.items()}
    yield
    for n, lg in saved.items():
        level, handlers, propagate = state[n]
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate


def test_embed_server_config_leaves_the_writer_running(
    clean_db: str, uvicorn_loggers: None
) -> None:
    h = logs.install(clean_db, "worker", interval=0.05, follow_settings=False)
    try:
        # What the worker's embed server builds after install, inside the same process.
        cfg = embed_server.config(embed_server.make_app(object()), "127.0.0.1", 0)  # type: ignore[arg-type]
        # No dictConfig at all: it would close every handler in the process, pytest's too.
        assert cfg.log_config is None
        assert h._thread is not None and h._thread.is_alive()
        assert h in logging.getLogger().handlers
        logging.getLogger("penish.test_embed").warning("after uvicorn config")
        wait_for(lambda: bool(rows(clean_db, "message = 'after uvicorn config'")))
    finally:
        logs.uninstall()


def test_close_while_installed_keeps_writing(clean_db: str) -> None:
    h = logs.install(clean_db, "worker", interval=0.05, follow_settings=False)
    try:
        # logging.config.dictConfig closes every handler it finds, installed or not.
        h.close()
        assert h._thread is not None and h._thread.is_alive()
        logging.getLogger("penish.test_close").warning("after close")
        wait_for(lambda: bool(rows(clean_db, "message = 'after close'")))
    finally:
        logs.uninstall()
    assert not h._thread.is_alive()
    # Once uninstalled, close is the ordinary one and must not hang or raise.
    h.close()


def test_stdout_keeps_its_level(clean_db: str) -> None:
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.INFO)
    out = io.StringIO()
    stream = logging.StreamHandler(out)
    root.addHandler(stream)
    lg = logging.getLogger("penish.test_stdout")
    try:
        h = logs.install(clean_db, "worker", interval=0.05, follow_settings=False)
        try:
            h.apply_level("debug")
            assert root.level == logging.DEBUG
            assert stream.level == logging.INFO
            lg.debug("debug for the table only")
            lg.info("info for both")
            wait_for(lambda: bool(rows(clean_db, "message = 'info for both'")))
        finally:
            logs.uninstall()
        assert out.getvalue() == "info for both\n"
        assert [r["message"] for r in rows(clean_db, "logger = 'penish.test_stdout'")] == [
            "debug for the table only",
            "info for both",
        ]
        assert stream.level == logging.NOTSET
        assert root.level == logging.INFO
    finally:
        root.removeHandler(stream)
        root.setLevel(before)


def test_stderr_takes_the_last_resorts_place() -> None:
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        h = logs.install(DEAD_DSN, "api", follow_settings=False, max_backoff=0.1)
        try:
            added = [x for x in root.handlers if x is not h]
            assert len(added) == 1
            assert type(added[0]) is logging.StreamHandler
            assert added[0].stream is sys.stderr
            assert added[0].level == logging.WARNING
        finally:
            logs.uninstall(timeout=1)
        assert root.handlers == []
    finally:
        root.handlers = saved


def test_uvicorn_logger_is_attached_and_detached(uvicorn_loggers: None) -> None:
    uv = logging.getLogger("uvicorn")
    own = logging.NullHandler()
    uv.handlers = [own]
    uv.propagate = False
    h = logs.install(DEAD_DSN, "api", follow_settings=False, max_backoff=0.1)
    try:
        # Unhandled request errors land on uvicorn.error, which never reaches the root.
        assert h in uv.handlers
    finally:
        logs.uninstall(timeout=1)
    assert uv.handlers == [own]
