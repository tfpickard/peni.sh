"""Owner-only upload surface. Off (404) unless ADMIN_PASSWORD is set.

Uploads are written into the worker's inbox in the same shape as an rsync drop, so the
worker needs no knowledge that this exists.
"""

import hashlib
import hmac
import logging
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

from penish import uploads
from penish.api.media import variants_for_many
from penish.collection.numerals import Request as Filing
from penish.collection.numerals import format_line, parse_line, parse_number
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

# A token shorter than this would make the rate limit the only defense, as with passwords.
MIN_READ_TOKEN_LEN = 32
# GET only: these routes are registered for GET, and the catch-all answers HEAD before the
# token is ever looked at.
_READ_METHODS = frozenset({"GET"})
# Major brands of HEIF holding HEVC. AVIF shares the container and is refused here on
# purpose: no door claims `.avif` (the inbox does not group it), so an upload of one could
# never be grouped with its sidecars.
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"})
_HEIF_GENERIC = frozenset({b"mif1", b"msf1"})

log = logging.getLogger(__name__)

_failures: dict[str, deque[float]] = defaultdict(deque)
# Bearer misses are kept apart: a script on the owner's own machine with a stale token
# must not use up the attempts the owner's password login needs from the same address.
_token_failures: dict[str, deque[float]] = defaultdict(deque)


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


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def require_admin(request: Request) -> None:
    settings = _settings_or_404()
    password = settings.admin_password
    assert password
    if _valid_cookie(request.cookies.get(COOKIE), password):
        return
    token = settings.admin_read_token
    given = _bearer(request)
    # Reading only: on any other method the header is as good as absent.
    if (
        given is None
        or not token
        or len(token) < MIN_READ_TOKEN_LEN
        or request.method not in _READ_METHODS
    ):
        raise HTTPException(status_code=401, detail="no")
    # Its own per-address bucket with the login's limits, and the same global budget, so
    # the token cannot be guessed faster than the password.
    q = _recent_failures(_client_ip(request), _token_failures)
    if len(q) >= LOGIN_FAILURES:
        raise HTTPException(status_code=429, detail="wait")
    if hmac.compare_digest(given.encode(), token.encode()):
        return
    exhausted = _global_failures() >= GLOBAL_LOGIN_FAILURES
    q.append(time.time())
    raise HTTPException(status_code=429 if exhausted else 401, detail="wait" if exhausted else "no")


def _client_ip(request: Request) -> str:
    # Behind cloudflared every peer is loopback, so the Cloudflare header is the real client.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def _prune(bucket: dict[str, deque[float]], keep: str) -> None:
    cutoff = time.time() - LOGIN_WINDOW
    # Prune every key so the map cannot grow without bound across many IPs.
    for key in list(bucket):
        q = bucket[key]
        while q and q[0] < cutoff:
            q.popleft()
        if not q and key != keep:
            del bucket[key]


def _recent_failures(ip: str, bucket: dict[str, deque[float]] | None = None) -> deque[float]:
    bucket = _failures if bucket is None else bucket
    # Both buckets, because the global budget sums them and must not count expired misses.
    _prune(_failures, ip if bucket is _failures else "")
    _prune(_token_failures, ip if bucket is _token_failures else "")
    return bucket[ip]


def _global_failures() -> int:
    return sum(len(q) for q in _failures.values()) + sum(len(q) for q in _token_failures.values())


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
    if _heic(head):
        return "heic"
    return None


def _heic(head: bytes) -> bool:
    """An ISO BMFF `ftyp` box naming HEVC-coded HEIF, as a major brand or, under a generic
    major brand, among the compatible ones."""
    if len(head) < 12 or head[4:8] != b"ftyp":
        return False
    major = head[8:12]
    if major in _HEIC_BRANDS:
        return True
    if major not in _HEIF_GENERIC:
        return False
    end = min(int.from_bytes(head[:4], "big"), len(head))
    return any(head[i : i + 4] in _HEIC_BRANDS for i in range(16, end - 3, 4))


def _stem(filename: str | None) -> str:
    base = Path(filename or "").stem.lower()
    clean = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:40].strip("-") or "upload"
    return f"{clean}-{secrets.token_hex(3)}"


def _tags(raw: str) -> str:
    seen = dict.fromkeys(t.strip() for t in re.split(r"[\n,]", raw) if t.strip())
    return "\n".join(seen)


def _collection_line(collection: object, book: object, leaf: object) -> str | None:
    """The form's collection fields as the sidecar line rsync would carry, so both
    arrivals take one path. Whether the book and leaf exist is ingest's to say."""
    if not isinstance(collection, str) or not collection.strip():
        return None

    def num(v: object) -> int | None:
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            return parse_number(v)
        except ValueError:
            raise HTTPException(status_code=422, detail="bad leaf") from None

    try:
        req = parse_line(collection)
    except ValueError:
        raise HTTPException(status_code=422, detail="bad collection") from None
    b, n = num(book), num(leaf)
    if n is not None and b is None:
        raise HTTPException(status_code=422, detail="a leaf needs a book")
    return format_line(Filing(req.collection, b, n))


def _write_tmp(inbox: Path, name: str, data: bytes) -> Path:
    """Dotfile first, then the caller renames, so the watcher only sees finished files."""
    tmp = inbox / f".{name}.{uuid.uuid4().hex[:8]}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _stream(dest: Path, head: bytes, src: BinaryIO) -> tuple[str, int]:
    """Copy the upload to dest under the size cap; returns the sha256 hex and the size."""
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
    return digest.hexdigest(), size


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
            sha, size = await run_in_threadpool(_stream, img_tmp, head, image.file)
            filename = image.filename

            sidecars: list[tuple[str, str]] = []
            for field in ("caption", "description", "prompt"):
                val = form.get(field)
                if isinstance(val, str) and val.strip():
                    sidecars.append((f"{stem}_{field}.txt", val.strip() + "\n"))
            raw_tags = form.get("tags")
            if isinstance(raw_tags, str) and _tags(raw_tags):
                sidecars.append((f"{stem}_tags.txt", _tags(raw_tags) + "\n"))
            line = _collection_line(form.get("collection"), form.get("book"), form.get("leaf"))
            if line:
                sidecars.append((f"{stem}_collection.txt", line + "\n"))
                # The conservator note is the owner's, for the leaf this picture becomes;
                # with no collection there is no leaf, so it is not written.
                note = form.get("note")
                if isinstance(note, str) and note.strip():
                    sidecars.append((f"{stem}_note.txt", note.strip() + "\n"))

        finals: list[tuple[Path, Path]] = []
        for name, text in sidecars:
            tmp = _write_tmp(inbox, name, text.encode())
            temps.append(tmp)
            finals.append((tmp, inbox / name))
        # The one await before the files land, so a cancelled request leaves only temps for
        # the finally to remove. Still before the image, so the row is there by the time
        # the watcher can claim it.
        await _record(request, stem, sha, filename, size, line)
        # Nothing below awaits: sidecars land before the image (the settle timer is keyed
        # by stem, and an image arriving first could be claimed without them) and a
        # cancellation cannot fall between them.
        for tmp, final in finals:
            os.rename(tmp, final)
            temps.remove(tmp)
        os.rename(img_tmp, inbox / f"{stem}.{ext}")
        temps.remove(img_tmp)
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
    return {"stem": stem, "sha256": sha}


async def _record(
    request: Request, stem: str, sha: str, filename: str | None, size: int, line: str | None
) -> None:
    try:
        async with request.app.state.pool.connection() as conn:
            await uploads.record(
                conn,
                stem=stem,
                sha256=sha,
                filename=filename,
                size=size,
                collection_line=line,
                source="form",
            )
    except Exception:
        # The picture still arrives; the worker writes the row itself when it claims it.
        log.warning("upload %s: could not record it", stem, exc_info=True)


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
    # Only reached while no images row exists yet. The row is written before the files land,
    # so on its own it cannot say the bytes arrived: a crash between the two leaves a record
    # with nothing behind it, and answering received would stop the form resending.
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select stem, error from uploads where sha256 = %s order by created_at desc, id desc",
            (sha256,),
        )
        ups = await cur.fetchall()
    settings = get_settings()
    for up in ups:
        if up["error"]:
            return {"state": "failed", "error": up["error"], "stem": up["stem"]}
        if await run_in_threadpool(uploads.held, settings, up["stem"]):
            return {"state": "received", "stem": up["stem"]}
    return {"state": "waiting"}


@router.get("/recent", dependencies=[Depends(require_admin)])
async def recent(request: Request) -> list[dict[str, Any]]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select pub_sha, slug, status, width, height, created_at from images "
            "order by created_at desc limit 20"
        )
        rows = await cur.fetchall()
    variants = variants_for_many([(r["pub_sha"], r["width"], r["height"]) for r in rows])
    out: list[dict[str, Any]] = []
    for r in rows:
        thumb = None
        if r["status"] == "live":
            webps = [v["webp"] for v in variants[r["pub_sha"]] if v["webp"]]
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


@router.get("/shas", dependencies=[Depends(require_admin)])
async def shas(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Every file the site holds or was handed, so a script can skip what is already here."""
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select sha256, status::text as status, slug from images order by id"
        )
        images = [dict(r) for r in await cur.fetchall()]
        staged = await uploads.stages(conn, get_settings())
    return {
        "images": images,
        "uploads": [
            {"sha256": u["sha256"], "stem": u["stem"], "stage": u["stage"], "error": u["error"]}
            for u in staged
        ],
    }


@router.get("/uploads", dependencies=[Depends(require_admin)])
async def list_uploads(request: Request, collection: str | None = None) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        staged = await uploads.stages(conn, get_settings(), collection=collection)
    return {"uploads": [{**u, "created_at": u["created_at"].isoformat()} for u in staged]}
