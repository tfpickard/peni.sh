"""Owner-only upload surface. Off (404) unless ADMIN_PASSWORD is set.

Uploads are written into the worker's inbox in the same shape as an rsync drop, so the
worker needs no knowledge that this exists.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from penish.api.media import variants_for_many
from penish.config import Settings, get_settings

MAX_BYTES = 50 * 1024 * 1024
COOKIE = "penish_admin"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_FAILURES = 5
# A global budget caps guessing from rotating IPs; a lockout only costs Tom 15 minutes.
GLOBAL_LOGIN_FAILURES = 50
LOGIN_WINDOW = 15 * 60
# Short passwords would make the rate limit the only defense.
MIN_PASSWORD_LEN = 16
CHUNK = 1024 * 1024
# Slack over MAX_BYTES for the multipart framing and the text fields.
_BODY_SLACK = 1024 * 1024

_failures: dict[str, deque[float]] = defaultdict(deque)


def _settings_or_404() -> Settings:
    s = get_settings()
    if not s.admin_password or len(s.admin_password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=404, detail="not here")
    return s


def _gate(request: Request) -> None:
    _settings_or_404()
    # A cross-site form cannot set custom headers, which backs up SameSite=Strict.
    if request.method == "POST" and request.headers.get("x-requested-with") != "penish":
        raise HTTPException(status_code=403, detail="no")


router = APIRouter(prefix="/api/admin", dependencies=[Depends(_gate)], include_in_schema=False)


async def no_store(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware, not a dependency: error responses skip dependency-set headers."""
    response = await call_next(request)
    if request.url.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _key(password: str) -> bytes:
    # Derived from the password so rotating it invalidates every session.
    return hashlib.sha256(b"penish-admin-v1" + password.encode()).digest()


def _sign(password: str, expiry: int) -> str:
    return hmac.new(_key(password), f"admin:{expiry}".encode(), hashlib.sha256).hexdigest()


def _valid_cookie(value: str | None, password: str) -> bool:
    if not value or "." not in value:
        return False
    exp, _, mac = value.partition(".")
    if not exp.isdigit():
        return False
    if not hmac.compare_digest(mac, _sign(password, int(exp))):
        return False
    return int(exp) > time.time()


def require_admin(request: Request) -> None:
    password = _settings_or_404().admin_password
    assert password
    if not _valid_cookie(request.cookies.get(COOKIE), password):
        raise HTTPException(status_code=401, detail="no")


def _client_ip(request: Request) -> str:
    # Behind cloudflared every peer is loopback, so the Cloudflare header is the real client.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def _recent_failures(ip: str) -> deque[float]:
    cutoff = time.time() - LOGIN_WINDOW
    # Prune every key so the map cannot grow without bound across many IPs.
    for key in list(_failures):
        q = _failures[key]
        while q and q[0] < cutoff:
            q.popleft()
        if not q and key != ip:
            del _failures[key]
    return _failures[ip]


def _global_failures() -> int:
    return sum(len(q) for q in _failures.values())


class Login(BaseModel):
    password: str


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
    password = _settings_or_404().admin_password
    assert password
    ip = _client_ip(request)
    q = _recent_failures(ip)
    if len(q) >= LOGIN_FAILURES:
        raise HTTPException(status_code=429, detail="wait")
    ok = hmac.compare_digest(
        hashlib.sha256(body.password.encode()).digest(), hashlib.sha256(password.encode()).digest()
    )
    if not ok:
        # Checked only after the password so a botnet spraying wrong guesses cannot lock the
        # owner out; once spent, wrong guesses get the same 429 whatever the IP.
        exhausted = _global_failures() >= GLOBAL_LOGIN_FAILURES
        q.append(time.time())
        raise HTTPException(
            status_code=429 if exhausted else 401, detail="wait" if exhausted else "no"
        )
    expiry = int(time.time()) + SESSION_SECONDS
    response.set_cookie(
        COOKIE,
        f"{expiry}.{_sign(password, expiry)}",
        max_age=SESSION_SECONDS,
        path="/",
        httponly=True,
        # Plain http only happens in local dev; request.url honors proxy headers.
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_admin)])
async def me() -> dict[str, bool]:
    return {"ok": True}


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _stem(filename: str | None) -> str:
    base = Path(filename or "").stem.lower()
    clean = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:40].strip("-") or "upload"
    return f"{clean}-{secrets.token_hex(3)}"


def _tags(raw: str) -> str:
    seen = dict.fromkeys(t.strip() for t in re.split(r"[\n,]", raw) if t.strip())
    return "\n".join(seen)


def _write_tmp(inbox: Path, name: str, data: bytes) -> Path:
    """Dotfile first, then the caller renames, so the watcher only sees finished files."""
    tmp = inbox / f".{name}.{uuid.uuid4().hex[:8]}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _stream(dest: Path, head: bytes, src: BinaryIO) -> str:
    """Copy the upload to dest under the size cap; returns the sha256 hex."""
    digest = hashlib.sha256()
    size = 0
    with open(dest, "wb") as f:
        chunk = head
        while chunk:
            size += len(chunk)
            if size > MAX_BYTES:
                raise HTTPException(status_code=413, detail="too large")
            digest.update(chunk)
            f.write(chunk)
            chunk = src.read(CHUNK)
        f.flush()
        os.fsync(f.fileno())
    return digest.hexdigest()


@router.post("/upload", dependencies=[Depends(require_admin)])
async def upload(request: Request) -> dict[str, str]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES + _BODY_SLACK:
        raise HTTPException(status_code=413, detail="too large")

    inbox = get_settings().inbox
    inbox.mkdir(parents=True, exist_ok=True)
    temps: list[Path] = []
    try:
        async with request.form() as form:
            image = form.get("image")
            if not isinstance(image, UploadFile):
                raise HTTPException(status_code=422, detail="no image")

            head = await image.read(CHUNK)
            ext = _sniff(head)
            if ext is None:
                raise HTTPException(status_code=415, detail="not an image")

            stem = _stem(image.filename)
            img_tmp = inbox / f".{stem}.{ext}.{uuid.uuid4().hex[:8]}"
            temps.append(img_tmp)
            sha = await run_in_threadpool(_stream, img_tmp, head, image.file)

            sidecars: list[tuple[str, str]] = []
            for field in ("caption", "description", "prompt"):
                val = form.get(field)
                if isinstance(val, str) and val.strip():
                    sidecars.append((f"{stem}_{field}.txt", val.strip() + "\n"))
            raw_tags = form.get("tags")
            if isinstance(raw_tags, str) and _tags(raw_tags):
                sidecars.append((f"{stem}_tags.txt", _tags(raw_tags) + "\n"))

        # Sidecars land before the image: the settle timer is keyed by stem, and an image
        # arriving first could be claimed without them.
        finals: list[tuple[Path, Path]] = []
        for name, text in sidecars:
            tmp = _write_tmp(inbox, name, text.encode())
            temps.append(tmp)
            finals.append((tmp, inbox / name))
        finals.append((img_tmp, inbox / f"{stem}.{ext}"))
        for tmp, final in finals:
            os.rename(tmp, final)
            temps.remove(tmp)
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
    return {"stem": stem, "sha256": sha}


@router.get("/status", dependencies=[Depends(require_admin)])
async def status(request: Request, sha256: str, stem: str = "") -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not re.fullmatch(r"[a-z0-9-]*", stem):
        raise HTTPException(status_code=422, detail="no")
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("select slug, status from images where sha256 = %s", (sha256,))
        row = await cur.fetchone()
    if row is not None:
        if row["status"] == "live" and row["slug"]:
            return {"state": "live", "slug": row["slug"]}
        return {"state": row["status"]}
    if stem:
        for d in sorted(get_settings().failed.glob(f"{stem}*")):
            err = next(iter(sorted(d.glob("*.err"))), None)
            text = err.read_text(errors="replace")[:500] if err else ""
            return {"state": "failed", "error": text}
    return {"state": "waiting"}


@router.get("/recent", dependencies=[Depends(require_admin)])
async def recent(request: Request) -> list[dict[str, Any]]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select sha256, slug, status, width, height, created_at from images "
            "order by created_at desc limit 20"
        )
        rows = await cur.fetchall()
    variants = variants_for_many([(r["sha256"], r["width"], r["height"]) for r in rows])
    out: list[dict[str, Any]] = []
    for r in rows:
        thumb = None
        if r["status"] == "live":
            webps = [v["webp"] for v in variants[r["sha256"]] if v["webp"]]
            thumb = webps[0] if webps else None
        out.append(
            {
                "slug": r["slug"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
                "thumb": thumb,
            }
        )
    return out
