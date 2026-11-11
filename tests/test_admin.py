import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest
import pyvips

from penish.api import admin
from penish.api.app import create_app
from penish.config import get_settings

PW = "hunter2"
XHR = {"X-Requested-With": "penish"}


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    admin._failures.clear()


def _build(clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, password: str | None):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><html><head><!--og--></head><body></body></html>"
    )
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(web))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    if password is None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASSWORD", password)
    get_settings.cache_clear()
    return create_app()


async def _serve(app) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c
    get_settings.cache_clear()


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _serve(_build(clean_db, tmp_path, monkeypatch, PW)):
        yield c


@pytest.fixture
async def off_client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _serve(_build(clean_db, tmp_path, monkeypatch, None)):
        yield c


async def _login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/admin/login", json={"password": PW}, headers=XHR)
    assert r.status_code == 200


def _png() -> bytes:
    return pyvips.Image.black(8, 8).write_to_buffer(".png")


async def test_off_without_password(off_client: httpx.AsyncClient) -> None:
    assert (await off_client.get("/api/admin/me")).status_code == 404
    r = await off_client.post("/api/admin/login", json={"password": "x"}, headers=XHR)
    assert r.status_code == 404
    assert (await off_client.get("/api/admin/recent")).status_code == 404


async def test_wrong_password_then_rate_limit(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        r = await client.post("/api/admin/login", json={"password": "no"}, headers=XHR)
        assert r.status_code == 401
    r = await client.post("/api/admin/login", json={"password": "no"}, headers=XHR)
    assert r.status_code == 429
    # The limit holds even for the right password until the window passes.
    r = await client.post("/api/admin/login", json={"password": PW}, headers=XHR)
    assert r.status_code == 429


async def test_rate_limit_is_per_ip(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        await client.post(
            "/api/admin/login",
            json={"password": "no"},
            headers={**XHR, "CF-Connecting-IP": "1.1.1.1"},
        )
    r = await client.post(
        "/api/admin/login", json={"password": PW}, headers={**XHR, "CF-Connecting-IP": "2.2.2.2"}
    )
    assert r.status_code == 200


async def test_login_sets_cookie_and_me(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/admin/me")).status_code == 401
    r = await client.post("/api/admin/login", json={"password": PW}, headers=XHR)
    cookie = r.headers["set-cookie"].lower()
    assert "penish_admin=" in cookie
    for flag in ("httponly", "secure", "samesite=strict", "path=/", f"max-age={30 * 86400}"):
        assert flag in cookie
    me = await client.get("/api/admin/me")
    assert me.status_code == 200
    assert me.json() == {"ok": True}


async def test_cookie_not_secure_on_http(client: httpx.AsyncClient) -> None:
    r = await client.post("http://t/api/admin/login", json={"password": PW}, headers=XHR)
    assert "secure" not in r.headers["set-cookie"].lower()


async def test_tampered_and_expired_cookie(client: httpx.AsyncClient) -> None:
    await _login(client)
    good = client.cookies["penish_admin"]
    exp, mac = good.split(".")
    for bad in (f"{exp}.{'0' * len(mac)}", f"{int(exp) + 1}.{mac}", "garbage", ""):
        client.cookies.clear()
        client.cookies.set("penish_admin", bad)
        assert (await client.get("/api/admin/me")).status_code == 401
    past = int(time.time()) - 10
    client.cookies.clear()
    client.cookies.set("penish_admin", f"{past}.{admin._sign(PW, past)}")
    assert (await client.get("/api/admin/me")).status_code == 401


async def test_post_requires_xhr_header(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/admin/login", json={"password": PW})
    assert r.status_code == 403
    assert "penish_admin" not in client.cookies


async def test_upload_writes_inbox(client: httpx.AsyncClient) -> None:
    await _login(client)
    data = _png()
    r = await client.post(
        "/api/admin/upload",
        headers=XHR,
        files={"image": ("My Cat!.PNG", data, "image/png")},
        data={
            "caption": "a cat",
            "description": "grey",
            "tags": "Cat, grey\nfur\n",
            "prompt": "cat",
        },
    )
    assert r.status_code == 200, r.text
    stem = r.json()["stem"]
    assert stem.startswith("my-cat-") and len(stem) == len("my-cat-") + 6
    import hashlib

    assert r.json()["sha256"] == hashlib.sha256(data).hexdigest()
    inbox = get_settings().inbox
    assert sorted(p.name for p in inbox.iterdir()) == sorted(
        [
            f"{stem}.png",
            f"{stem}_caption.txt",
            f"{stem}_description.txt",
            f"{stem}_tags.txt",
            f"{stem}_prompt.txt",
        ]
    )
    assert (inbox / f"{stem}.png").read_bytes() == data
    assert (inbox / f"{stem}_caption.txt").read_text().strip() == "a cat"
    assert (inbox / f"{stem}_tags.txt").read_text().splitlines() == ["Cat", "grey", "fur"]


async def test_upload_extension_from_sniff(client: httpx.AsyncClient) -> None:
    await _login(client)
    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("x.jpg", _png(), "image/jpeg")}
    )
    stem = r.json()["stem"]
    assert (get_settings().inbox / f"{stem}.png").exists()


async def test_upload_rejects_non_image(client: httpx.AsyncClient) -> None:
    await _login(client)
    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("a.png", b"not an image", "image/png")}
    )
    assert r.status_code == 415
    inbox = get_settings().inbox
    assert not inbox.exists() or list(inbox.iterdir()) == []


async def test_upload_rejects_oversize(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _login(client)
    monkeypatch.setattr(admin, "MAX_BYTES", 100)
    big = _png() + b"\0" * 200
    r = await client.post(
        "/api/admin/upload",
        headers=XHR,
        files={"image": ("a.png", big, "image/png")},
        data={"caption": "x"},
    )
    assert r.status_code == 413
    assert list(get_settings().inbox.iterdir()) == []


async def test_status_waiting_then_live(client: httpx.AsyncClient, clean_db: str) -> None:
    await _login(client)
    sha = "ab" * 32
    r = await client.get("/api/admin/status", params={"sha256": sha, "stem": "x-abc123"})
    assert r.json() == {"state": "waiting"}
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "insert into images (sha256, slug, status) values (%s, 'a-cat', 'processing')", (sha,)
        )
    r = await client.get("/api/admin/status", params={"sha256": sha})
    assert r.json() == {"state": "processing"}
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("update images set status = 'live' where sha256 = %s", (sha,))
    r = await client.get("/api/admin/status", params={"sha256": sha})
    assert r.json() == {"state": "live", "slug": "a-cat"}


async def test_status_failed_from_dir(client: httpx.AsyncClient) -> None:
    await _login(client)
    d = get_settings().failed / "x-abc123"
    d.mkdir(parents=True)
    (d / "x.err").write_text("bad" * 300)
    r = await client.get("/api/admin/status", params={"sha256": "cd" * 32, "stem": "x-abc123"})
    body = r.json()
    assert body["state"] == "failed"
    assert len(body["error"]) == 500


async def test_recent_lists_rows(client: httpx.AsyncClient, clean_db: str) -> None:
    await _login(client)
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("insert into images (sha256, status) values (%s, 'queued')", ("ef" * 32,))
    r = await client.get("/api/admin/recent")
    assert r.status_code == 200
    items = r.json()
    assert items[0]["status"] == "queued" and items[0]["thumb"] is None


async def test_no_store_everywhere(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/admin/me")).headers["cache-control"] == "no-store"
    await _login(client)
    assert (await client.get("/api/admin/me")).headers["cache-control"] == "no-store"
    assert (await client.get("/api/admin/recent")).headers["cache-control"] == "no-store"


async def test_no_store_when_off(off_client: httpx.AsyncClient) -> None:
    assert (await off_client.get("/api/admin/me")).headers["cache-control"] == "no-store"
