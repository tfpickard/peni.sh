"""Settings the admin can change without a deploy: models, effort and prompts.

The `settings` table only holds overrides. An absent row means the default, which comes
from the env-backed Settings (models) or penish/prompts/*.md (prompts), so a reset is a
delete and a code change to a default reaches every key nobody has overridden.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
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
}
KEYS = (
    "describe.model",
    "describe.effort",
    "describe.prompt",
    "haiku.model",
    "haiku.prompt",
    "grow.model",
    "grow.prompt",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PURPOSES = ("describe", "haiku", "grow")


def _check_key(key: str) -> None:
    if key not in KEYS:
        raise KeyError(key)


def default(key: str, settings: Settings | None = None) -> Any:
    """The value a key has when no override row exists."""
    _check_key(key)
    s = settings or get_settings()
    # Grow is describe-grade writing, so it follows describe until someone splits them.
    if key in ("describe.model", "grow.model"):
        return s.describe_model
    if key == "haiku.model":
        return s.haiku_model
    if key == "describe.effort":
        return None
    return (_PROMPTS / _PROMPT_FILES[key]).read_text()


def validate(key: str, value: Any) -> Any:
    """Normalized value, or ValueError. Shared with the admin api so both reject the same."""
    _check_key(key)
    if key == "describe.effort":
        if value is None or value in EFFORTS:
            return value
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)} or null")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    # Models are ids, so stray whitespace from a text field is never intended; prompts keep theirs.
    return value.strip() if key.endswith(".model") else value


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
    """One api_usage row per Anthropic call; a missing usage block counts as zero."""
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    async with pool.connection() as conn:
        await conn.execute(
            "insert into api_usage (purpose, model, input_tokens, output_tokens)"
            " values (%s, %s, %s, %s)",
            (purpose, model, inp, out),
        )


def usage_recorder(pool: Pool | None, purpose: str) -> UsageRecorder | None:
    if pool is None:
        return None
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")

    async def go(model: str, usage: Any) -> None:
        await record_usage(pool, purpose, model, usage)

    return go
