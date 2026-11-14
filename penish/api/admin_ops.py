"""Admin operations beyond pictures: comments, haikus, models, queue, stats and system.

Mounted under /api/admin next to admin.router, so the no_store middleware covers it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg.errors
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from penish import runtime
from penish.api.admin import _gate, require_admin
from penish.api.media import variants_for_many
from penish.config import get_settings
from penish.text import no_em_dash
from penish.worker.jobs import enqueue
from penish.worker.sidecars import stem_of

router = APIRouter(
    prefix="/api/admin",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

# Static on purpose: the list only seeds a dropdown, and a model id set by hand still shows.
# The prefixed ids were checked on 2026-11-14 against each provider's model list as taking
# images and strict json schema output; any other `provider:id` is accepted by hand.
KNOWN_MODELS = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-2",
    "openai:gpt-6-astra",
    "openai:gpt-6.1-sol",
    "openai:gpt-6.1-terra",
    "openai:gpt-6-luna",
    "xai:grok-4.7",
    "xai:grok-4.6",
    "openrouter:google/gemini-4-carbon",
    "openrouter:qwen/qwen3.8-max-prime",
    "openrouter:mistralai/mistral-large-4-0",
)
THUMB_WIDTH = 480
HAIKU_LINE_MAX = 60
ERR_CAP = 2048
# Strict allowlist: the name is the only thing between a request and the filesystem.
_FAILED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _pool(request: Request) -> Any:
    return request.app.state.pool


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _thumb(variants: list[dict[str, Any]]) -> str | None:
    """Largest webp up to THUMB_WIDTH, else the smallest webp there is."""
    webps = [v for v in variants if v["webp"]]
    if not webps:
        return None
    small = [v for v in webps if v["w"] <= THUMB_WIDTH]
    return str((small[-1] if small else webps[0])["webp"])


def _thumbs(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    found = variants_for_many([(r["pub_sha"], r["width"], r["height"]) for r in rows])
    return {sha: _thumb(v) for sha, v in found.items()}


# -- comments -------------------------------------------------------------


@router.get("/comments")
async def list_comments(
    request: Request, status: Literal["pending", "approved", "rejected"] = "pending"
) -> list[dict[str, Any]]:
    # Pending reads oldest first, as a queue; decided ones newest first, as a log.
    order = "asc" if status == "pending" else "desc"
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select c.id, c.name, c.body, c.status, c.created_at, c.decided_at, c.image_id,"
            " i.slug, i.pub_sha, i.width, i.height"
            " from comments c join images i on i.id = c.image_id"
            f" where c.status = %s order by c.created_at {order}, c.id {order} limit 200",  # noqa: S608
            (status,),
        )
        rows = await cur.fetchall()
    thumbs = _thumbs(rows)
    return [
        {
            "id": r["id"],
            "image_id": r["image_id"],
            "slug": r["slug"],
            "thumb": thumbs[r["pub_sha"]],
            "name": r["name"],
            "body": r["body"],
            "status": r["status"],
            "created_at": _iso(r["created_at"]),
            "decided_at": _iso(r["decided_at"]),
        }
        for r in rows
    ]


class CommentDecision(BaseModel):
    status: Literal["approved", "rejected"]


@router.post("/comments/{comment_id}")
async def decide_comment(
    comment_id: int, body: CommentDecision, request: Request
) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "update comments set status = %s, decided_at = now() where id = %s"
            " returning id, status, decided_at",
            (body.status, comment_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not here")
    return {"id": row["id"], "status": row["status"], "decided_at": _iso(row["decided_at"])}


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: int, request: Request) -> dict[str, bool]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute("delete from comments where id = %s", (comment_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not here")
    return {"ok": True}


# -- haikus ---------------------------------------------------------------


def _clean_lines(lines: list[str]) -> list[str]:
    if len(lines) != 3:
        raise HTTPException(status_code=422, detail="3 lines")
    out: list[str] = []
    for line in lines:
        # Converted before the length check, since `--` is longer than a dash.
        clean = no_em_dash(line).strip()
        if not clean or "\n" in clean or "\r" in clean:
            raise HTTPException(status_code=422, detail="empty line")
        if len(clean) > HAIKU_LINE_MAX:
            raise HTTPException(status_code=422, detail="line too long")
        out.append(clean)
    return out


def _haiku(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "lines": [r["l1"], r["l2"], r["l3"]],
        "source": r["source"],
        "active": r["active"],
        "image_id": r["image_id"],
        "created_at": _iso(r["created_at"]),
    }


@router.get("/haikus")
async def list_haikus(
    request: Request,
    source: Literal["seed", "model", "owner"] | None = None,
    active: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    if source is not None:
        where.append("source = %s")
        args.append(source)
    if active is not None:
        where.append("active = %s")
        args.append(active)
    clause = f"where {' and '.join(where)}" if where else ""
    async with _pool(request).connection() as conn:
        cur = await conn.execute(f"select count(*) as n from haikus {clause}", args)  # noqa: S608
        total = (await cur.fetchone())["n"]
        cur = await conn.execute(
            f"select id, l1, l2, l3, source::text as source, active, image_id, created_at"  # noqa: S608
            f" from haikus {clause} order by id desc limit %s offset %s",
            [*args, limit, offset],
        )
        rows = await cur.fetchall()
    return {"items": [_haiku(r) for r in rows], "total": total}


class HaikuIn(BaseModel):
    lines: list[str]


@router.post("/haikus")
async def create_haiku(body: HaikuIn, request: Request) -> dict[str, Any]:
    l1, l2, l3 = _clean_lines(body.lines)
    try:
        async with _pool(request).connection() as conn:
            cur = await conn.execute(
                "insert into haikus (l1, l2, l3, source) values (%s, %s, %s, 'owner')"
                " returning id, l1, l2, l3, source::text as source, active, image_id, created_at",
                (l1, l2, l3),
            )
            row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="already here") from None
    assert row
    return _haiku(row)


class HaikuPatch(BaseModel):
    lines: list[str] | None = None
    active: bool | None = None


@router.patch("/haikus/{haiku_id}")
async def patch_haiku(haiku_id: int, body: HaikuPatch, request: Request) -> dict[str, Any]:
    sets: list[str] = []
    args: list[Any] = []
    if body.lines is not None:
        l1, l2, l3 = _clean_lines(body.lines)
        sets += ["l1 = %s", "l2 = %s", "l3 = %s"]
        args += [l1, l2, l3]
    if body.active is not None:
        sets.append("active = %s")
        args.append(body.active)
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        async with _pool(request).connection() as conn:
            cur = await conn.execute(
                f"update haikus set {', '.join(sets)} where id = %s"  # noqa: S608
                " returning id, l1, l2, l3, source::text as source, active, image_id, created_at",
                [*args, haiku_id],
            )
            row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="already here") from None
    if row is None:
        raise HTTPException(status_code=404, detail="not here")
    return _haiku(row)


@router.delete("/haikus/{haiku_id}")
async def delete_haiku(haiku_id: int, request: Request) -> dict[str, bool]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute("delete from haikus where id = %s", (haiku_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not here")
    return {"ok": True}


class Grow(BaseModel):
    n: int = Field(ge=1, le=200)


@router.post("/haikus/grow")
async def grow_haikus(body: Grow, request: Request) -> dict[str, int | None]:
    return {"job_id": await enqueue(_pool(request), "grow_haikus", {"n": body.n})}


# -- models ---------------------------------------------------------------


@router.get("/models")
async def get_models(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    current = {k: await runtime.get(pool, k) for k in runtime.KEYS}
    default = {k: runtime.default(k) for k in runtime.KEYS}
    known = list(KNOWN_MODELS)
    for k, v in current.items():
        if k.endswith(".model") and v not in known:
            known.append(v)
    async with pool.connection() as conn:
        cur = await conn.execute("select info from heartbeats where name = 'worker'")
        beat = await cur.fetchone()
    info = beat["info"] if beat and isinstance(beat["info"], dict) else {}
    keyed = info.get("providers")
    return {
        "current": current,
        "default": default,
        "efforts": list(runtime.EFFORTS),
        "known_models": known,
        "purposes": list(runtime.PURPOSES),
        "providers": ["anthropic", *runtime.PROVIDERS],
        # From the worker, which holds the keys; null until a worker that reports it beats.
        "keyed": keyed if isinstance(keyed, list) else None,
        "calls_today": await runtime.calls_today(pool),
    }


class ModelPut(BaseModel):
    key: str
    value: Any = None


@router.put("/models")
async def put_model(body: ModelPut, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    try:
        await runtime.put(pool, body.key, body.value)
    except KeyError:
        raise HTTPException(status_code=422, detail="unknown key") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"key": body.key, "value": await runtime.get(pool, body.key)}


@router.delete("/models/{key}")
async def reset_model(key: str, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    try:
        await runtime.reset(pool, key)
    except KeyError:
        raise HTTPException(status_code=422, detail="unknown key") from None
    return {"key": key, "value": await runtime.get(pool, key)}


async def _bulk(request: Request, kind: str) -> dict[str, Any]:
    # A second click while one is queued or running collapses into it.
    job_id = await enqueue(_pool(request), kind, {}, dedupe_key=kind)
    return {"job_id": job_id, "queued": job_id is not None}


@router.post("/models/reembed_all")
async def reembed_all(request: Request) -> dict[str, Any]:
    return await _bulk(request, "reembed_all")


@router.post("/models/redescribe_all")
async def redescribe_all(request: Request) -> dict[str, Any]:
    return await _bulk(request, "redescribe_all")


# -- queue ----------------------------------------------------------------


def _failed_dir(name: str) -> Path:
    if not _FAILED_NAME.match(name) or name.endswith("."):
        raise HTTPException(status_code=422, detail="no")
    root = get_settings().failed.resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise HTTPException(status_code=422, detail="no")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="not here")
    return path


def _read_err(directory: Path) -> str:
    chunks: list[str] = []
    budget = ERR_CAP
    for f in sorted(directory.glob("*.err")):
        if budget <= 0:
            break
        with open(f, "rb") as fh:
            data = fh.read(budget)
        budget -= len(data)
        chunks.append(data.decode("utf-8", errors="replace"))
    return "".join(chunks)


def _list_failed() -> list[dict[str, Any]]:
    root = get_settings().failed
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in root.iterdir():
        if not d.is_dir() or d.is_symlink():
            continue
        out.append(
            {
                "name": d.name,
                "files": sorted(f.name for f in d.iterdir() if f.is_file()),
                "err": _read_err(d),
                "mtime": d.stat().st_mtime,
            }
        )
    out.sort(key=lambda x: x["mtime"], reverse=True)
    for item in out:
        item["at"] = datetime.fromtimestamp(item.pop("mtime"), UTC).isoformat()
    return out


@router.get("/queue")
async def get_queue(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select id, type, payload, status::text as status, attempts, run_after, last_error,"
            " created_at, updated_at from jobs where status <> 'done'"
            " order by created_at desc, id desc limit 200"
        )
        rows = await cur.fetchall()
    jobs = [
        {
            "id": r["id"],
            "type": r["type"],
            "payload": r["payload"],
            "status": r["status"],
            "attempts": r["attempts"],
            "run_after": _iso(r["run_after"]),
            "last_error": r["last_error"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        for r in rows
    ]
    return {"jobs": jobs, "failed": await run_in_threadpool(_list_failed)}


@router.post("/queue/{job_id}/retry")
async def retry_job(job_id: int, request: Request) -> dict[str, bool]:
    try:
        async with _pool(request).connection() as conn:
            cur = await conn.execute(
                "update jobs set status = 'queued', run_after = now(), attempts = 0,"
                " locked_at = null, updated_at = now() where id = %s",
                (job_id,),
            )
    except psycopg.errors.UniqueViolation:
        # Another live job already holds this dedupe key.
        raise HTTPException(status_code=409, detail="already queued") from None
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not here")
    return {"ok": True}


@router.delete("/queue/{job_id}")
async def delete_job(job_id: int, request: Request) -> dict[str, bool]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute("delete from jobs where id = %s", (job_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not here")
    return {"ok": True}


def _suffixed(name: str, suffix: str) -> str:
    stem, _kind = stem_of(name)
    if stem is not None:
        return stem + suffix + name[len(stem) :]
    p = Path(name)
    return p.stem + suffix + p.suffix


def _move_back(directory: Path, inbox: Path) -> list[str]:
    # The .err is a diagnostic for the admin, not input; the watcher would treat it as noise.
    files = sorted(f for f in directory.iterdir() if f.is_file() and f.suffix != ".err")
    if not files:
        raise HTTPException(status_code=422, detail="nothing to retry")
    inbox.mkdir(parents=True, exist_ok=True)
    names = {f: f.name for f in files}
    if any((inbox / n).exists() for n in names.values()):
        # One suffix for the whole group, or the sidecars would stop pairing with the image.
        while True:
            suffix = f"-r{secrets.token_hex(2)}"
            names = {f: _suffixed(f.name, suffix) for f in files}
            if not any((inbox / n).exists() for n in names.values()):
                break
    for f in files:
        os.rename(f, inbox / names[f])
    shutil.rmtree(directory)
    return sorted(names.values())


@router.post("/queue/failed/{name}/retry")
async def retry_failed(name: str) -> dict[str, Any]:
    directory = _failed_dir(name)
    moved = await run_in_threadpool(_move_back, directory, get_settings().inbox)
    return {"ok": True, "files": moved}


@router.delete("/queue/failed/{name}")
async def delete_failed(name: str) -> dict[str, bool]:
    directory = _failed_dir(name)
    await run_in_threadpool(shutil.rmtree, directory)
    return {"ok": True}


# -- stats ----------------------------------------------------------------


async def _voted(conn: Any, order: str, cmp: str) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "select i.id, i.slug, i.pub_sha, i.width, i.height, sum(v.value)::int as net"
        " from images i join votes v on v.image_id = i.id"
        " where i.status = 'live' and i.slug is not null"
        f" group by i.id having sum(v.value) {cmp} 0"  # noqa: S608
        f" order by net {order}, i.id limit 10"
    )
    return list(await cur.fetchall())


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "select status::text as status, count(*) as n from images group by 1"
        )
        by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute("select count(*) as n from votes")
        votes_total = (await cur.fetchone())["n"]
        cur = await conn.execute("select count(*) as n from comments where status = 'pending'")
        comments_pending = (await cur.fetchone())["n"]
        top = await _voted(conn, "desc", ">")
        bottom = await _voted(conn, "asc", "<")
        cur = await conn.execute(
            "select model, n, temperature, at from ("
            " select * from corpus_stats_history where kind = 'description'"
            " order by at desc limit 500) h order by at asc"
        )
        history = await cur.fetchall()
        cur = await conn.execute(
            "select (at at time zone 'utc')::date as day, model,"
            " sum(input_tokens)::bigint as inp, sum(output_tokens)::bigint as out"
            " from api_usage where at >= now() - interval '30 days'"
            " group by 1, 2 order by 1, 2"
        )
        usage = await cur.fetchall()
    thumbs = _thumbs(top + bottom)

    def pic(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "slug": r["slug"], "thumb": thumbs[r["pub_sha"]], "net": r["net"]}

    return {
        "pictures": sum(by_status.values()),
        "by_status": by_status,
        "votes_total": votes_total,
        "comments_pending": comments_pending,
        "top": [pic(r) for r in top],
        "bottom": [pic(r) for r in bottom],
        "temperature_history": [
            {"model": r["model"], "n": r["n"], "temperature": r["temperature"], "at": _iso(r["at"])}
            for r in history
        ],
        "api_usage": {
            "last_30d": [
                {"day": r["day"].isoformat(), "model": r["model"], "in": r["inp"], "out": r["out"]}
                for r in usage
            ]
        },
    }


# -- system ---------------------------------------------------------------


def _last_backup_at() -> str | None:
    root = os.environ.get("BACKUP_DIR")
    if not root or not Path(root).is_dir():
        return None
    mtimes = [f.stat().st_mtime for f in Path(root).iterdir() if f.is_file()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), UTC).isoformat()


def _disk(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


@router.get("/system")
async def system(request: Request) -> dict[str, Any]:
    settings = get_settings()
    async with _pool(request).connection() as conn:
        cur = await conn.execute("select at, info from heartbeats where name = 'worker'")
        beat = await cur.fetchone()
        cur = await conn.execute("select pg_database_size(current_database()) as size")
        size = (await cur.fetchone())["size"]
        cur = await conn.execute(
            "select id, type, status::text as status, attempts, last_error, updated_at"
            " from jobs where last_error is not null order by updated_at desc, id desc limit 20"
        )
        errors = await cur.fetchall()
    disk_path = settings.data_dir if settings.data_dir.exists() else Path("/")
    return {
        "deployed_sha": settings.git_sha,
        "worker_heartbeat_at": _iso(beat["at"]) if beat else None,
        "worker_info": beat["info"] if beat else None,
        "disk": await run_in_threadpool(_disk, disk_path),
        "last_backup_at": await run_in_threadpool(_last_backup_at),
        "db_size": size,
        "recent_errors": [
            {
                "id": r["id"],
                "type": r["type"],
                "status": r["status"],
                "attempts": r["attempts"],
                "error": r["last_error"],
                "at": _iso(r["updated_at"]),
            }
            for r in errors
        ],
    }
