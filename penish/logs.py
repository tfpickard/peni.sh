"""Log lines into Postgres (app_log) for the admin logs page, from the api and the worker.

A model call that fails says little on stdout and nothing at all once docker rotates it,
so both processes also keep their lines in the database where the admin page can filter
them by job, picture and level. Stdout stays as it was: the stream handlers keep the
level they had, and only the database handler follows the runtime key `log.level`.

Logging must never slow down or break the code that logs. `emit` only formats the record
and puts it on a bounded queue (dropping and counting when full); a daemon thread with its
own sync connection writes batches about once a second, survives the database going away
(retrying with backoff), re-reads `log.level` about every 30 s, and reports what it had to
drop in the next batch it writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import queue
import re
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from penish import runtime
from penish.config import Settings

log = logging.getLogger(__name__)

Source = Literal["api", "worker"]

LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
CONTEXT_KEYS = ("job_id", "job_type", "attempt", "image_id", "purpose", "model", "provider")
# The logger the drop reports are written under; the admin api sums them from here.
DROP_LOGGER = "penish.logs"
ROW_CAP = 500_000
PRUNE_EVERY = 3600.0
# A traceback or a raw model reply is useful whole; a runaway repr is not.
TEXT_MAX = 32_000
# Libraries whose debug lines are noise at best and, for the model SDKs, a dump of the
# request body (base64 images included) at worst. Kept at info while log.level is debug.
NOISY = (
    "anthropic",
    "openai",
    "httpx",
    "httpcore",
    "hpack",
    "h11",
    "h2",
    "urllib3",
    "asyncio",
    "watchfiles",
    "psycopg",
    "psycopg_pool",
    "multipart",
    "python_multipart",
    "PIL",
    "filelock",
    "fsspec",
    "huggingface_hub",
    "sentence_transformers",
    "transformers",
    "torch",
    "uvicorn.access",
)

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "penish_log_context", default=_EMPTY
)


@contextlib.contextmanager
def context(**values: Any) -> Iterator[None]:
    """Adds values to the context of every line logged inside, in this task and its children.

    None values are left out, so a caller can pass what it has without checking.
    """
    merged = {**_context.get(), **{k: v for k, v in values.items() if v is not None}}
    token = _context.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _context.reset(token)


def model_context(
    purpose: str | None, model: str, provider: str | None = None
) -> contextlib.AbstractContextManager[None]:
    """Context for one model call: purpose, model and provider on every line logged inside.

    With no provider it is read off the model id (`openai:gpt-x`), a bare id being Anthropic.
    """
    if provider is None:
        try:
            prefix, _ = runtime.split_model(model)
        except ValueError:
            prefix = None
        provider = prefix or "anthropic"
    return context(purpose=purpose, model=model, provider=provider)


def current_context() -> dict[str, Any]:
    return dict(_context.get())


# -- scrubbing -------------------------------------------------------------

_DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,[A-Za-z0-9+/=]+")
# Long enough that no word, hash or path matches, short of the shortest image.
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_AUTH = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key)(['\"]?\s*[:=]\s*['\"]?)"
    r"(bearer\s+)?[^\s'\",}\]]+"
)
_KEYLIKE = re.compile(r"\b(?:sk-(?:ant-|or-|proj-)?|xai-)[A-Za-z0-9_-]{16,}")


def secrets_from(settings: Settings) -> tuple[str, ...]:
    """Every configured secret, to be blanked out of anything written to the table."""
    found = [
        settings.anthropic_api_key,
        settings.openai_api_key,
        settings.openrouter_api_key,
        settings.xai_api_key,
        settings.admin_password,
        settings.secret_key,
        settings.turnstile_secret,
    ]
    with contextlib.suppress(ValueError):
        found.append(urlsplit(settings.database_url).password)
    # Short values would blank ordinary words out of every line.
    return tuple(s for s in found if s and len(s) >= 8)


def _encodable(text: str) -> str:
    # A non-UTF-8 file name decodes to lone surrogates (os.fsdecode), which psycopg cannot
    # encode; left in, one such line would fail every retry of its batch for good.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    text = _encodable(text)
    for s in secrets:
        if s in text:
            text = text.replace(s, "<secret>")
    text = _DATA_URL.sub(lambda m: f"<image {m.group(1)} {len(m.group(0))} chars>", text)
    text = _B64_RUN.sub(lambda m: f"<base64 {len(m.group(0))} chars>", text)
    text = _AUTH.sub(lambda m: f"{m.group(1)}{m.group(2)}<secret>", text)
    text = _KEYLIKE.sub("<secret>", text)
    # Postgres text cannot hold a NUL, and one would fail the whole batch.
    return text.replace("\x00", "\\0")


def _scrub_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)[:TEXT_MAX]
    if isinstance(value, dict):
        return {_encodable(str(k)): _scrub_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, secrets) for v in value]
    return value


# -- handler ---------------------------------------------------------------

_INSERT = (
    "insert into app_log (at, source, level, logger, message, exc, context)"
    " values (%s, %s, %s, %s, %s, %s, %s)"
)
Row = tuple[datetime, str, int, str, str, str | None, dict[str, Any] | None]
_FORMATTER = logging.Formatter()
# What every record has, so the rest of its attributes are the caller's `extra`. `message`
# and `asctime` appear once a formatter has run, `color_message` is uvicorn's duplicate.
_RECORD_ATTRS = frozenset(
    logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None).__dict__
) | {"message", "asctime", "taskName", "color_message"}


class DbHandler(logging.Handler):
    def __init__(
        self,
        dsn: str,
        source: Source,
        *,
        secrets: Iterable[str] = (),
        capacity: int = 10_000,
        interval: float = 1.0,
        settings_interval: float = 30.0,
        max_backoff: float = 30.0,
        follow_settings: bool = True,
    ) -> None:
        super().__init__(level=LEVELS[runtime.LOG_LEVEL_DEFAULT])
        self.dsn = dsn
        self.source: Source = source
        self.secrets = tuple(secrets)
        self.capacity = capacity
        self.interval = interval
        self.settings_interval = settings_interval
        self.max_backoff = max_backoff
        self.follow_settings = follow_settings
        # Only the installed handler owns the root logger's level.
        self.manages_root = False
        self._queue: queue.Queue[Row] = queue.Queue(maxsize=capacity)
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._stop = threading.Event()
        self._conn: psycopg.Connection[Any] | None = None
        self._thread: threading.Thread | None = None
        self._writer_ident: int | None = None
        # Set by install: the root logger's level before it, which stdout still follows.
        self.base_root_level = logging.WARNING
        self._pinned: list[str] = []
        self.level_name = runtime.LOG_LEVEL_DEFAULT

    # Runs in the caller's thread: never block, never raise.
    def emit(self, record: logging.LogRecord) -> None:
        # The writer's own lines (a psycopg notice, a failed insert) would feed it forever.
        if record.thread is not None and record.thread == self._writer_ident:
            return
        if record.levelno < logging.INFO and record.name.split(".", 1)[0] in _NOISY_ROOTS:
            return
        try:
            self._queue.put_nowait(self._row(record))
        except Exception:
            # queue.Full, or a record that will not format: either way one lost line.
            self._count_drop()

    def _count_drop(self, n: int = 1) -> None:
        with self._drop_lock:
            self._dropped += n

    @property
    def dropped(self) -> int:
        """Lines dropped and not yet reported in a written batch."""
        with self._drop_lock:
            return self._dropped

    def _row(self, record: logging.LogRecord) -> Row:
        message = scrub(record.getMessage(), self.secrets)[:TEXT_MAX]
        exc: str | None = None
        if record.exc_info:
            exc = _FORMATTER.formatException(record.exc_info)
        elif record.exc_text:
            exc = record.exc_text
        if record.stack_info:
            exc = f"{exc}\n{record.stack_info}" if exc else record.stack_info
        if exc is not None:
            exc = scrub(exc, self.secrets)[-TEXT_MAX:]
        ctx: dict[str, Any] = dict(_context.get())
        # `extra={...}` lands as attributes on the record; anything a plain record lacks
        # is such an extra (stop_reason from the model-call seam, say), and belongs here.
        for k, v in record.__dict__.items():
            if k not in _RECORD_ATTRS:
                ctx[k] = v
        nested = ctx.pop("context", None)
        if isinstance(nested, Mapping):
            ctx.update(nested)
        clean: dict[str, Any] | None = None
        if ctx:
            # A round trip through json turns anything odd (a Path, a datetime) into text
            # here, where a failure is a dropped line, instead of in the writer's batch.
            clean = _scrub_value(json.loads(json.dumps(ctx, default=str)), self.secrets)
        at = datetime.fromtimestamp(record.created, UTC)
        return (at, self.source, record.levelno, _encodable(record.name), message, exc, clean)

    # -- levels -------------------------------------------------------------

    def apply_level(self, name: str) -> None:
        """Sets what the table keeps, opening the root logger far enough to let it through."""
        num = LEVELS.get(name, LEVELS[runtime.LOG_LEVEL_DEFAULT])
        self.level_name = name if name in LEVELS else runtime.LOG_LEVEL_DEFAULT
        self.setLevel(num)
        if not self.manages_root:
            return
        logging.getLogger().setLevel(min(self.base_root_level, num))
        # Only when the root was opened below what stdout already allowed: someone who set
        # LOG_LEVEL=DEBUG for stdout asked for the libraries' debug lines there.
        if num < logging.INFO and num < self.base_root_level:
            for name_ in NOISY:
                lg = logging.getLogger(name_)
                if lg.level == logging.NOTSET:
                    lg.setLevel(logging.INFO)
                    self._pinned.append(name_)
        else:
            self._unpin()

    def _unpin(self) -> None:
        for name_ in self._pinned:
            logging.getLogger(name_).setLevel(logging.NOTSET)
        self._pinned = []

    def refresh_settings(self) -> None:
        """Reads log.level from the settings table and applies it. Raises on a db error."""
        conn = self._connection()
        row = conn.execute("select value from settings where key = 'log.level'").fetchone()
        value = row[0] if row else runtime.LOG_LEVEL_DEFAULT
        self.apply_level(value if value in LEVELS else runtime.LOG_LEVEL_DEFAULT)

    # -- writer -------------------------------------------------------------

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                autocommit=True,
                connect_timeout=5,
                application_name=f"penish-{self.source}-log",
                # A hung insert only stalls this thread, but it should not stall it forever.
                options="-c statement_timeout=15000",
            )
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
        self._conn = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"penish-log-{self.source}")
        self._thread.daemon = True
        self._thread.start()

    def _drain(self, pending: list[Row]) -> None:
        # Bounded, so an outage holds at most twice the queue; the rest is dropped and counted.
        while len(pending) < self.capacity:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                return

    def _drop_report(self, n: int) -> Row:
        return (
            datetime.now(UTC),
            self.source,
            logging.WARNING,
            DROP_LOGGER,
            f"dropped {n} log lines",
            None,
            {"dropped": n},
        )

    def _write(self, rows: list[Row]) -> None:
        conn = self._connection()
        params = [(*r[:6], Jsonb(r[6]) if r[6] is not None else None) for r in rows]
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(_INSERT, params)
            return
        except psycopg.OperationalError:
            raise
        except Exception:
            # Something in the batch the table (or the client's encoder) will not take.
            # Fall through to one by one, so one bad line costs itself and not the second
            # of logging around it.
            pass
        # Savepoints inside one transaction: if the connection goes partway through, none
        # of it landed, so the retry neither writes a line twice nor counts a drop twice.
        bad = 0
        with conn.transaction():
            for p in params:
                try:
                    with conn.transaction():
                        conn.execute(_INSERT, p)
                except psycopg.OperationalError:
                    raise
                except Exception:
                    bad += 1
        if bad:
            self._count_drop(bad)

    def flush_once(self, pending: list[Row]) -> bool:
        """Writes pending lines (plus a drop report). False when the database is unreachable."""
        self._drain(pending)
        reported = self.dropped
        batch = list(pending)
        if reported:
            batch.append(self._drop_report(reported))
        if not batch:
            return True
        try:
            self._write(batch)
        except Exception as e:
            self._drop_connection()
            # Not through logging: this thread's own lines are ignored by emit anyway.
            print(f"penish.logs: write failed, will retry: {e!r}", file=sys.stderr)
            return False
        pending.clear()
        if reported:
            self._count_drop(-reported)
        return True

    def _run(self) -> None:
        self._writer_ident = threading.get_ident()
        pending: list[Row] = []
        backoff = self.interval
        retry_at = 0.0
        next_settings = 0.0
        while True:
            stopping = self._stop.is_set()
            now = time.monotonic()
            if self.follow_settings and now >= next_settings and now >= retry_at:
                next_settings = now + self.settings_interval
                try:
                    self.refresh_settings()
                except Exception:
                    self._drop_connection()
            if now >= retry_at or stopping:
                if self.flush_once(pending):
                    backoff = self.interval
                    retry_at = 0.0
                else:
                    retry_at = time.monotonic() + backoff
                    backoff = min(backoff * 2, self.max_backoff)
            if stopping:
                break
            self._stop.wait(self.interval)
        self._drop_connection()

    def close(self) -> None:
        # logging.config.dictConfig (uvicorn with its default log_config, say) closes every
        # handler it finds. While installed, this one is still on the root logger, so it
        # keeps writing; uninstall stops it, and the writer is a daemon thread anyway.
        if _installed.handler is not self:
            self.stop()
        super().close()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._unpin()


_NOISY_ROOTS = frozenset(n.split(".", 1)[0] for n in NOISY if n != "uvicorn.access")


@dataclass
class _Installed:
    handler: DbHandler | None = None
    stream: logging.Handler | None = None
    stream_levels: list[tuple[logging.Handler, int]] = field(default_factory=list)
    root_level: int = logging.WARNING
    uvicorn: bool = False


_installed = _Installed()


def installed() -> DbHandler | None:
    return _installed.handler


def install(
    dsn: str,
    source: Source,
    *,
    secrets: Iterable[str] = (),
    stderr_level: int | None = None,
    **kwargs: Any,
) -> DbHandler:
    """Adds the database handler to the root logger and starts its writer thread.

    Stdout keeps today's behaviour: stream handlers on the root logger are pinned at the
    root's current level before the root is opened up for the database. With no handler
    at all (the api under uvicorn), Python's last resort printed warnings to stderr; a
    stderr handler at `stderr_level` (default warning) takes its place.
    """
    if _installed.handler is not None:
        uninstall()
    root = logging.getLogger()
    _installed.root_level = root.level
    handler = DbHandler(dsn, source, secrets=secrets, **kwargs)
    handler.base_root_level = root.level
    handler.manages_root = True
    _installed.stream_levels = []
    if not root.handlers:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(stderr_level if stderr_level is not None else logging.WARNING)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
        _installed.stream = stream
    else:
        for h in root.handlers:
            # Exactly StreamHandler: pytest's capture handlers subclass it and set their own.
            if type(h) is logging.StreamHandler and h.level == logging.NOTSET:
                _installed.stream_levels.append((h, h.level))
                h.setLevel(root.level)
    root.addHandler(handler)
    # uvicorn's own loggers do not propagate; its error logger is where an unhandled
    # exception in a request lands, so the table should see it too.
    uv = logging.getLogger("uvicorn")
    if uv.handlers and not uv.propagate:
        uv.addHandler(handler)
        _installed.uvicorn = True
    handler.apply_level(runtime.LOG_LEVEL_DEFAULT)
    _installed.handler = handler
    handler.start()
    return handler


def uninstall(timeout: float = 5.0) -> None:
    """Flushes what is queued (as far as the database allows within timeout) and detaches."""
    handler = _installed.handler
    if handler is None:
        return
    root = logging.getLogger()
    handler.stop(timeout)
    root.removeHandler(handler)
    if _installed.uvicorn:
        logging.getLogger("uvicorn").removeHandler(handler)
    if _installed.stream is not None:
        root.removeHandler(_installed.stream)
    for h, level in _installed.stream_levels:
        h.setLevel(level)
    root.setLevel(_installed.root_level)
    _installed.handler = None
    _installed.stream = None
    _installed.stream_levels = []
    _installed.uvicorn = False


# -- retention -------------------------------------------------------------


async def prune(pool: runtime.Pool, keep_days: int, cap: int = ROW_CAP) -> int:
    """Deletes lines older than keep_days, then all but the newest cap. Returns the count."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "delete from app_log where at < now() - make_interval(days => %s)", (keep_days,)
        )
        n = cur.rowcount
        # By id, the insert order, which the primary key answers without a sort.
        cur = await conn.execute(
            "delete from app_log where id <= ("
            " select id from app_log order by id desc offset %s limit 1)",
            (cap,),
        )
        n += cur.rowcount
    return n


async def run_prune_loop(
    pool: runtime.Pool, stop: asyncio.Event, *, interval: float = PRUNE_EVERY
) -> None:
    """The worker's hourly prune, the first one at startup."""
    while not stop.is_set():
        try:
            keep = await runtime.get(pool, "log.keep_days")
            try:
                keep = runtime.validate("log.keep_days", keep)
            except ValueError:
                keep = runtime.LOG_KEEP_DAYS_DEFAULT
            n = await prune(pool, keep)
            if n:
                log.info("pruned %d log lines", n)
        except Exception:
            log.exception("log prune failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
