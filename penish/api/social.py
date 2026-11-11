"""Public votes and comments. Anonymous: identity is a cookie id, stored only as an hmac."""

import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from penish.config import get_settings

COOKIE = "penish_v"
COOKIE_SECONDS = 365 * 24 * 3600
VOTE_LIMIT = 30
VOTE_WINDOW = 60
COMMENT_LIMIT = 3
COMMENT_WINDOW = 10 * 60
BODY_MAX = 2000
NAME_MAX = 40
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SITEVERIFY_TIMEOUT = 5.0

# Newlines survive in bodies; every other control character, and the bidi overrides that
# can reorder surrounding text, are dropped.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_CONTROL_NAME = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


class RateLimit:
    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Record an attempt; False when the key is over its limit."""
        now = time.time()
        cutoff = now - self.window
        # Prune every key so the map cannot grow without bound across many IPs.
        for k in list(self._hits):
            q = self._hits[k]
            while q and q[0] < cutoff:
                q.popleft()
            if not q and k != key:
                del self._hits[k]
        q = self._hits[key]
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._hits.clear()


vote_limit = RateLimit(VOTE_LIMIT, VOTE_WINDOW)
comment_limit = RateLimit(COMMENT_LIMIT, COMMENT_WINDOW)


def _mac(purpose: str, value: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _raw_ip(request: Request) -> str:
    # Behind cloudflared every peer is loopback, so the Cloudflare header is the real client.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def ip_hash(request: Request) -> str:
    return _mac("ip", _raw_ip(request))


def voter_for(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE)
    # Bounded so a hostile cookie cannot make us hash megabytes.
    if not cookie or len(cookie) > 64:
        return None
    return _mac("voter", cookie)


async def vote_summary(conn: Any, image_id: int, voter: str | None) -> dict[str, int]:
    cur = await conn.execute(
        """
        select count(*) filter (where value = 1) as up,
               count(*) filter (where value = -1) as down,
               coalesce(max(value) filter (where voter = %s), 0) as mine
        from votes where image_id = %s
        """,
        (voter, image_id),
    )
    row = await cur.fetchone()
    up, down = row["up"], row["down"]
    # max() of one row is that row's value, and voter is unique per image.
    return {"up": up, "down": down, "net": up - down, "mine": row["mine"]}


async def approved_comments(conn: Any, image_id: int) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "select name, body, created_at from comments "
        "where image_id = %s and status = 'approved' order by created_at, id",
        (image_id,),
    )
    return [
        {"name": r["name"], "body": r["body"], "created_at": r["created_at"].isoformat()}
        for r in await cur.fetchall()
    ]


def _gate(request: Request) -> None:
    # A cross-site form cannot set custom headers, which backs up SameSite=Lax.
    if request.headers.get("x-requested-with") != "penish":
        raise HTTPException(status_code=403, detail="no")


router = APIRouter()


@router.get("/api/config")
async def config() -> dict[str, str | None]:
    return {"turnstile_site_key": get_settings().turnstile_site_key}


async def _live_id(request: Request, slug: str) -> int:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select id from images where slug = %s and status = 'live'", (slug,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not here")
    return int(row["id"])


class VoteIn(BaseModel):
    value: Literal[-1, 0, 1]


@router.post("/api/images/{slug}/vote", dependencies=[Depends(_gate)])
async def vote(request: Request, response: Response, slug: str, body: VoteIn) -> dict[str, int]:
    image_id = await _live_id(request, slug)
    if not vote_limit.hit(ip_hash(request)):
        raise HTTPException(status_code=429, detail="wait")
    cookie = request.cookies.get(COOKIE)
    if not cookie or len(cookie) > 64:
        cookie = secrets.token_urlsafe(16)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=COOKIE_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
        )
    voter = _mac("voter", cookie)
    async with request.app.state.pool.connection() as conn:
        if body.value == 0:
            await conn.execute(
                "delete from votes where image_id = %s and voter = %s", (image_id, voter)
            )
        else:
            await conn.execute(
                """
                insert into votes (image_id, voter, value) values (%s, %s, %s)
                on conflict (image_id, voter) do update set value = excluded.value
                """,
                (image_id, voter, body.value),
            )
        return await vote_summary(conn, image_id, voter)


class CommentIn(BaseModel):
    name: str | None = Field(default=None, max_length=400)
    body: str = Field(max_length=10 * BODY_MAX)
    turnstile: str | None = Field(default=None, max_length=4096)


def clean(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


async def _turnstile_ok(token: str | None, ip: str) -> bool:
    secret = get_settings().turnstile_secret
    if not secret:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=SITEVERIFY_TIMEOUT) as client:
            r = await client.post(
                SITEVERIFY, data={"secret": secret, "response": token, "remoteip": ip}
            )
        return r.status_code == 200 and r.json().get("success") is True
    except (httpx.HTTPError, ValueError, AttributeError):
        # Failing closed: a Cloudflare outage must not become an open door.
        return False


@router.post("/api/images/{slug}/comments", dependencies=[Depends(_gate)])
async def comment(request: Request, slug: str, body: CommentIn) -> dict[str, bool]:
    image_id = await _live_id(request, slug)
    hashed = ip_hash(request)
    text = clean(body.body, _CONTROL)
    name = clean(body.name, _CONTROL_NAME) if body.name else ""
    if not text or len(text) > BODY_MAX or len(name) > NAME_MAX:
        raise HTTPException(status_code=422, detail="no")
    # Before turnstile so a flood cannot spend siteverify calls either.
    if not comment_limit.hit(hashed):
        raise HTTPException(status_code=429, detail="wait")
    if not await _turnstile_ok(body.turnstile, _raw_ip(request)):
        raise HTTPException(status_code=403, detail="no")
    async with request.app.state.pool.connection() as conn:
        await conn.execute(
            "insert into comments (image_id, name, body, ip_hash) values (%s, %s, %s, %s)",
            (image_id, name or None, text, hashed),
        )
    return {"ok": True}
