"""Settings the admin can change without a deploy: models, effort, prompts and the call cap.

The `settings` table only holds overrides. An absent row means the default, which comes
from the env-backed Settings (models) or penish/prompts/*.md (prompts), so a reset is a
delete and a code change to a default reaches every key nobody has overridden.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from penish.config import Settings, get_settings

log = logging.getLogger(__name__)

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
Getter = Callable[[str], Awaitable[Any]]
# (model, usage) where usage is the response's usage object.
UsageRecorder = Callable[[str, Any], Awaitable[None]]

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_PROMPT_FILES = {
    "describe.prompt": "describe.md",
    "haiku.prompt": "haiku.md",
    "grow.prompt": "grow_haikus.md",
    "extract.prompt": "mycelium_extract.md",
    "name.prompt": "mycelium_name.md",
}
KEYS = (
    "describe.model",
    "describe.effort",
    "describe.prompt",
    "haiku.model",
    "haiku.prompt",
    "grow.model",
    "grow.prompt",
    "extract.model",
    "extract.effort",
    "extract.prompt",
    "name.model",
    "name.prompt",
    "mycelium.daily_calls",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PURPOSES = ("describe", "haiku", "grow", "extract", "name")
# The purposes that count against mycelium.daily_calls. Describe, haiku and grow run once
# per upload or per click; these two run from sweeps and the organism, so they are the
# ones that could run away.
CAPPED_PURPOSES = ("extract", "name")
DAILY_CALLS_DEFAULT = 400
DAILY_CALLS_MAX = 100_000
# A model id may name its provider as `provider:id`. A bare id is Anthropic, which keeps
# every id stored before providers existed valid. OpenRouter ids may hold colons of their
# own (`vendor/model:variant`), so only the first one separates.
PROVIDERS = ("openai", "openrouter", "xai")


def split_model(model: str) -> tuple[str | None, str]:
    """(provider, id); provider None means Anthropic. ValueError for an unknown prefix."""
    if ":" not in model:
        return None, model
    prefix, rest = model.split(":", 1)
    if prefix not in PROVIDERS:
        raise ValueError(f"unknown provider {prefix}")
    # `openai: gpt-6-astra` from a text field means `gpt-6-astra`; a provider would
    # take the space as part of the id and refuse it on every call.
    rest = rest.strip()
    if not rest:
        raise ValueError(f"no model id after {prefix}:")
    return prefix, rest


def _check_key(key: str) -> None:
    if key not in KEYS:
        raise KeyError(key)


def default(key: str, settings: Settings | None = None) -> Any:
    """The value a key has when no override row exists."""
    _check_key(key)
    s = settings or get_settings()
    # Grow, extract and naming are describe-grade work (extract also needs vision), so they
    # follow describe until someone splits them.
    if key in ("describe.model", "grow.model", "extract.model", "name.model"):
        return s.describe_model
    if key == "haiku.model":
        return s.haiku_model
    if key.endswith(".effort"):
        return None
    if key == "mycelium.daily_calls":
        return DAILY_CALLS_DEFAULT
    return (_PROMPTS / _PROMPT_FILES[key]).read_text()


def validate(key: str, value: Any) -> Any:
    """Normalized value, or ValueError. Shared with the admin api so both reject the same."""
    _check_key(key)
    if key.endswith(".effort"):
        if value is None or value in EFFORTS:
            return value
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)} or null")
    if key == "mycelium.daily_calls":
        # bool is an int to Python, and true is not a number of calls.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if not 0 <= value <= DAILY_CALLS_MAX:
            raise ValueError(f"{key} must be between 0 and {DAILY_CALLS_MAX}")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    if key.endswith(".model"):
        # Ids, so stray whitespace from a text field is never intended; prompts keep theirs.
        value = value.strip()
        provider, rest = split_model(value)
        if provider is not None:
            value = f"{provider}:{rest}"
    return value


async def get(pool: Pool, key: str, *, settings: Settings | None = None) -> Any:
    _check_key(key)
    async with pool.connection() as conn:
        cur = await conn.execute("select value from settings where key = %s", (key,))
        row = await cur.fetchone()
    if row is None:
        return default(key, settings)
    return row["value"]


async def put(pool: Pool, key: str, value: Any) -> None:
    value = validate(key, value)
    async with pool.connection() as conn:
        await conn.execute(
            "insert into settings (key, value, updated_at) values (%s, %s, now())"
            " on conflict (key) do update set value = excluded.value, updated_at = now()",
            (key, Jsonb(value)),
        )


async def reset(pool: Pool, key: str) -> None:
    _check_key(key)
    async with pool.connection() as conn:
        await conn.execute("delete from settings where key = %s", (key,))


def getter(pool: Pool | None, settings: Settings | None = None) -> Getter:
    """A get bound to a pool. Without one (unit tests, scripts with no db) it serves defaults."""

    async def go(key: str) -> Any:
        if pool is None:
            return default(key, settings)
        return await get(pool, key, settings=settings)

    return go


async def record_usage(pool: Pool, purpose: str, model: str, usage: Any) -> None:
    """One api_usage row per model call; a missing usage block counts as zero."""
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    async with pool.connection() as conn:
        await conn.execute(
            "insert into api_usage (purpose, model, input_tokens, output_tokens)"
            " values (%s, %s, %s, %s)",
            (purpose, model, inp, out),
        )


def day_start(now: datetime | None = None) -> datetime:
    """Start of the current UTC day: the cap resets at UTC midnight, whatever the box's zone."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def next_midnight(now: datetime | None = None) -> datetime:
    return day_start(now) + timedelta(days=1)


async def calls_today(pool: Pool, now: datetime | None = None) -> int:
    """Capped model calls since UTC midnight, from the same rows the stats page reads."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) as n from api_usage where purpose = any(%s) and at >= %s",
            (list(CAPPED_PURPOSES), day_start(now)),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


def usage_recorder(pool: Pool | None, purpose: str) -> UsageRecorder | None:
    if pool is None:
        return None
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")

    async def go(model: str, usage: Any) -> None:
        await record_usage(pool, purpose, model, usage)

    return go
