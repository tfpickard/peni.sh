import hmac
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from penish.api import social
from penish.api.app import create_app
from penish.config import get_settings
from tests.test_api_images import add_image

H = {"X-Requested-With": "penish"}


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><html><head><!--og--></head></html>")
    data = tmp_path / "data"
    (data / "media" / "pub").mkdir(parents=True)
    (data / "media" / "orig").mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(web))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    get_settings.cache_clear()
    social.vote_limit.clear()
    social.comment_limit.clear()
    app = create_app()
    if not any(getattr(r, "path", "") == "/api/config" for r in app.routes):
        # Not yet included by app.py: mount ahead of the index catch-all.
        before = len(app.routes)
        app.include_router(social.router)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = added
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    get_settings.cache_clear()


def cookie_of(r: httpx.Response) -> dict[str, str]:
    # The jar will not send a Secure cookie over http, so tests carry it by hand.
    return {"cookie": r.headers["set-cookie"].split(";")[0]}


async def vote(c: httpx.AsyncClient, value: int, cookie: dict[str, str] | None = None) -> Any:
    return await c.post(
        "/api/images/img-1/vote", json={"value": value}, headers={**H, **(cookie or {})}
    )


async def test_vote_change_undo(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    r = await vote(client, 1)
    assert r.status_code == 200
    assert r.json() == {"up": 1, "down": 0, "net": 1, "mine": 1}
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "secure" in flags and "samesite=lax" in flags
    ck = cookie_of(r)
    assert (await vote(client, -1, ck)).json() == {"up": 0, "down": 1, "net": -1, "mine": -1}
    detail = await client.get("/api/images/img-1", headers=ck)
    assert detail.json()["votes"] == {"up": 0, "down": 1, "net": -1, "mine": -1}
    assert detail.headers["cache-control"] == "no-store"
    assert (await vote(client, 0, ck)).json() == {"up": 0, "down": 0, "net": 0, "mine": 0}


async def test_one_vote_per_cookie(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    a = cookie_of(await vote(client, 1))
    await vote(client, 1, a)
    await vote(client, 1, a)
    b = await vote(client, 1)
    assert b.json() == {"up": 2, "down": 0, "net": 2, "mine": 1}
    anon = (await client.get("/api/images/img-1")).json()["votes"]
    assert anon == {"up": 2, "down": 0, "net": 2, "mine": 0}
    with psycopg.connect(clean_db) as conn:
        voters = [r[0] for r in conn.execute("select voter from votes")]
    assert len(voters) == 2
    assert a["cookie"].split("=")[1] not in voters


async def test_vote_rate_limit(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    for _ in range(social.VOTE_LIMIT):
        assert (await vote(client, 1)).status_code == 200
    r = await vote(client, 1)
    assert r.status_code == 429
    assert r.json() == {"detail": "wait"}


async def test_bad_vote_value(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    assert (await vote(client, 2)).status_code == 422


async def test_not_live_is_404(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1, status="queued")
    r = await vote(client, 1)
    assert r.status_code == 404
    assert r.json() == {"detail": "not here"}
    r = await client.post("/api/images/img-1/comments", json={"body": "hi"}, headers=H)
    assert r.status_code == 404
    r = await client.post("/api/images/nope/vote", json={"value": 1}, headers=H)
    assert r.status_code == 404


async def test_requires_header(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    assert (await client.post("/api/images/img-1/vote", json={"value": 1})).status_code == 403
    r = await client.post("/api/images/img-1/comments", json={"body": "hi"})
    assert r.status_code == 403
    r = await client.post(
        "/api/images/img-1/vote", json={"value": 1}, headers={"X-Requested-With": "x"}
    )
    assert r.status_code == 403


async def test_comment_pending_until_approved(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    r = await client.post(
        "/api/images/img-1/comments",
        json={"name": "  tom\x00\n ", "body": " a\x07 line\r\nnext\x1b "},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert (await client.get("/api/images/img-1")).json()["comments"] == []
    with psycopg.connect(clean_db) as conn:
        row = conn.execute("select name, body, status from comments").fetchone()
        assert row == ("tom", "a line\nnext", "pending")
        conn.execute("update comments set status = 'approved'")
        conn.commit()
    (c,) = (await client.get("/api/images/img-1")).json()["comments"]
    assert c["name"] == "tom"
    assert c["body"] == "a line\nnext"
    assert c["created_at"]


async def test_comment_validation(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    for payload in (
        {"body": "  \x01 "},
        {"body": "x" * 2001},
        {"body": "ok", "name": "n" * 41},
    ):
        r = await client.post("/api/images/img-1/comments", json=payload, headers=H)
        assert r.status_code == 422, payload
    r = await client.post("/api/images/img-1/comments", json={"body": "x" * 2000}, headers=H)
    assert r.status_code == 200
    with psycopg.connect(clean_db) as conn:
        assert conn.execute("select name from comments").fetchone() == (None,)


async def test_comment_rate_limit(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    for _ in range(social.COMMENT_LIMIT):
        r = await client.post("/api/images/img-1/comments", json={"body": "hi"}, headers=H)
        assert r.status_code == 200
    r = await client.post("/api/images/img-1/comments", json={"body": "hi"}, headers=H)
    assert r.status_code == 429


async def test_turnstile(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_image(clean_db, 1)
    monkeypatch.setenv("TURNSTILE_SECRET", "s3cret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site")
    get_settings.cache_clear()
    assert (await client.get("/api/config")).json() == {"turnstile_site_key": "site"}

    seen: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"success": True}

    class Fake:
        def __init__(self, **_: Any) -> None: ...

        async def __aenter__(self) -> "Fake":
            return self

        async def __aexit__(self, *_: Any) -> None: ...

        async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
            seen.append({"url": url, **data})
            if outcome.get("raise"):
                raise httpx.ConnectTimeout("slow")
            return httpx.Response(200, json=outcome)

    monkeypatch.setattr(social.httpx, "AsyncClient", Fake)
    url = "/api/images/img-1/comments"

    r = await client.post(url, json={"body": "a", "turnstile": "tok"}, headers=H)
    assert r.status_code == 200
    assert seen[0]["url"] == social.SITEVERIFY
    assert seen[0]["secret"] == "s3cret"
    assert seen[0]["response"] == "tok"
    assert seen[0]["remoteip"]

    outcome["success"] = False
    r = await client.post(url, json={"body": "b", "turnstile": "tok"}, headers=H)
    assert r.status_code == 403

    outcome.update({"success": True, "raise": True})
    r = await client.post(url, json={"body": "c", "turnstile": "tok"}, headers=H)
    assert r.status_code == 403

    outcome["raise"] = False
    social.comment_limit.clear()
    n = len(seen)
    r = await client.post(url, json={"body": "d"}, headers=H)
    assert r.status_code == 403
    assert len(seen) == n  # no token, no call

    with psycopg.connect(clean_db) as conn:
        assert conn.execute("select count(*) from comments").fetchone() == (1,)


async def test_no_raw_ip_stored(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    ip = "203.0.113.77"
    h = {**H, "CF-Connecting-IP": ip}
    await client.post("/api/images/img-1/comments", json={"body": "hi"}, headers=h)
    await vote(client, 1, {"CF-Connecting-IP": ip})
    with psycopg.connect(clean_db) as conn:
        (ip_hash,) = conn.execute("select ip_hash from comments").fetchone() or (None,)
        dump = str(conn.execute("select * from comments").fetchall()) + str(
            conn.execute("select * from votes").fetchall()
        )
    assert ip not in dump
    key = get_settings().secret_key.encode()
    assert ip_hash == hmac.new(key, f"ip:{ip}".encode(), "sha256").hexdigest()


async def test_config_default(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/config")).json() == {"turnstile_site_key": None}
