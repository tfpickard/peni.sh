"""Admin logs: the app_log table, newest first, filtered by level, source, job or picture.

Rows hold no secrets by construction (penish/logs.py scrubs them on the way in), so they
are returned as stored.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from penish import logs, runtime
from penish.api.admin import _gate, require_admin

router = APIRouter(
    prefix="/api/admin/logs",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

LIMIT_MAX = 500
# Filter names; a stored level between two of them (a library's custom level) still sorts.
_FILTER_LEVELS: dict[str, int] = {**logs.LEVELS, "critical": logging.CRITICAL}


# DROP_LOGGER is a fixed module constant, never input, so formatting it in is safe.
_DROPS_SQL = (
    "select coalesce(sum((context->>'dropped')::bigint), 0) as n from app_log"
    f" where logger = '{logs.DROP_LOGGER}' and context ? 'dropped'"  # noqa: S608
)


def _pool(request: Request) -> Any:
    return request.app.state.pool


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _level_name(n: int) -> str:
    """Lowercase name for a stored level, `level 25` for one Python has no name for."""
    name = logging.getLevelName(n)
    return name.lower() if isinstance(name, str) and not name.startswith("Level ") else f"level {n}"


def _summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "at": _iso(r["at"]),
        "source": r["source"],
        "level": _level_name(r["level"]),
        "logger": r["logger"],
        "message": r["message"],
        "has_exc": r["has_exc"],
        "context": r["context"],
    }


def _like_prefix(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("")
async def list_logs(
    request: Request,
    level: Literal["debug", "info", "warning", "error", "critical"] | None = None,
    source: Literal["api", "worker"] | None = None,
    logger: str | None = Query(None, max_length=200),
    q: str | None = Query(None, max_length=500),
    job: int | None = None,
    image: int | None = None,
    before: int | None = None,
    after: int | None = None,
    limit: int = Query(100, ge=1, le=LIMIT_MAX),
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    if level is not None:
        where.append("level >= %s")
        args.append(_FILTER_LEVELS[level])
    if source is not None:
        where.append("source = %s")
        args.append(source)
    if logger:
        where.append("logger like %s")
        args.append(_like_prefix(logger))
    if q:
        # strpos over lower(): a substring, with no pattern characters to escape.
        where.append("(strpos(lower(message), %s) > 0 or strpos(lower(coalesce(exc, '')), %s) > 0)")
        args += [q.lower(), q.lower()]
    if job is not None:
        # Text, to match the expression index; context holds the job id as a json number.
        where.append("context->>'job_id' = %s")
        args.append(str(job))
    if image is not None:
        where.append("context->>'image_id' = %s")
        args.append(str(image))
    if before is not None:
        where.append("id < %s")
        args.append(before)
    if after is not None:
        # Following: only what is newer than the top of the page, so a quiet log costs an
        # empty list and not a page of messages every few seconds.
        where.append("id > %s")
        args.append(after)
    clause = f"where {' and '.join(where)}" if where else ""
    pool = _pool(request)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id, at, source, level, logger, message, exc is not null as has_exc, context"
            f" from app_log {clause} order by id desc limit %s",  # noqa: S608 -- fixed fragments
            [*args, limit],
        )
        rows = await cur.fetchall()
        # Drop reports are rows of their own, so this is what was lost within the
        # retention window, from both processes.
        # The logger is a literal, not a parameter, so the planner can match the partial
        # index app_log_drops_idx (migration 014) instead of scanning the table.
        cur = await conn.execute(_DROPS_SQL)
        drop_row = await cur.fetchone()
    dropped = int(drop_row["n"]) if drop_row else 0
    # Plus what this process dropped and has not written a report for yet.
    mine = logs.installed()
    if mine is not None:
        dropped += mine.dropped
    return {
        "items": [_summary(r) for r in rows],
        "next": rows[-1]["id"] if len(rows) == limit else None,
        "dropped": dropped,
        "level": await runtime.get(pool, "log.level"),
        "keep_days": await runtime.get(pool, "log.keep_days"),
        "levels": list(runtime.LOG_LEVELS),
    }


@router.get("/{log_id}")
async def get_log(log_id: int, request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select id, at, source, level, logger, message, exc, context from app_log"
            " where id = %s",
            (log_id,),
        )
        r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="not here")
    return {
        "id": r["id"],
        "at": _iso(r["at"]),
        "source": r["source"],
        "level": _level_name(r["level"]),
        "logger": r["logger"],
        "message": r["message"],
        "exc": r["exc"],
        "context": r["context"],
    }
