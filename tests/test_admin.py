import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pyvips
from fastapi import HTTPException

from penish.api import admin
from penish.api.app import create_app
from penish.config import get_settings

PW = "hunter2-hunter2-hunter2"
XHR = {"X-Requested-With": "penish"}


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    admin._failures.clear()
    admin._token_failures.clear()


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


async def test_status_received_after_a_form_upload_with_no_stem(
    client: httpx.AsyncClient,
) -> None:
    import hashlib

    await _login(client)
    data = _png()
    sha = hashlib.sha256(data).hexdigest()
    # Never seen: only now does the answer mean the server holds nothing.
    r = await client.get("/api/admin/status", params={"sha256": sha, "stem": ""})
    assert r.json() == {"state": "waiting"}
    up = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("a.png", data, "image/png")}
    )
    assert up.status_code == 200, up.text
    stem = up.json()["stem"]
    # As after a lost response: the form has the bytes' hash and no stem.
    for params in ({"sha256": sha, "stem": ""}, {"sha256": sha}):
        r = await client.get("/api/admin/status", params=params)
        assert r.json() == {"state": "received", "stem": stem}


async def test_status_failed_from_the_upload_row(client: httpx.AsyncClient, clean_db: str) -> None:
    await _login(client)
    sha = "ef" * 32
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "insert into uploads (stem, sha256, source, error, created_at) values"
            " ('x-old', %s, 'form', null, now() - interval '1 hour'),"
            " ('x-new', %s, 'form', 'x.png: not a readable image', now())",
            (sha, sha),
        )
    r = await client.get("/api/admin/status", params={"sha256": sha, "stem": ""})
    assert r.json() == {"state": "failed", "error": "x.png: not a readable image", "stem": "x-new"}


async def test_status_a_picture_outranks_its_upload_row(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    await _login(client)
    sha = "12" * 32
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "insert into uploads (stem, sha256, source, error) values ('x', %s, 'form', 'old')",
            (sha,),
        )
        conn.execute(
            "insert into images (sha256, slug, status) values (%s, 'a-cat', 'live')", (sha,)
        )
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


async def test_global_failure_budget_spares_correct_password(client: httpx.AsyncClient) -> None:
    for i in range(admin.GLOBAL_LOGIN_FAILURES):
        admin._failures[f"10.0.0.{i}"].append(time.time())
    headers = {**XHR, "cf-connecting-ip": "10.9.9.9"}
    r = await client.post("/api/admin/login", json={"password": PW}, headers=headers)
    assert r.status_code == 200
    r = await client.post("/api/admin/login", json={"password": "wrong"}, headers=headers)
    assert r.status_code == 429


def test_short_password_disables_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as e:
        admin._settings_or_404()
    assert e.value.status_code == 404
    get_settings.cache_clear()


# -- the uploads row and HEIC ----------------------------------------------------------


def _uploads(dsn: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "select stem, sha256, filename, bytes, collection_line, source from uploads"
        ).fetchall()


async def test_upload_records_the_arrival(client: httpx.AsyncClient, clean_db: str) -> None:
    import hashlib

    await _login(client)
    data = _png()
    r = await client.post(
        "/api/admin/upload",
        headers=XHR,
        files={"image": ("My Cat!.PNG", data, "image/png")},
        data={"collection": "pansophicon", "book": "3", "leaf": "14"},
    )
    assert r.status_code == 200, r.text
    stem = r.json()["stem"]
    line = (get_settings().inbox / f"{stem}_collection.txt").read_text().strip()
    assert _uploads(clean_db) == [
        (stem, hashlib.sha256(data).hexdigest(), "My Cat!.PNG", len(data), line, "form")
    ]

    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("b.png", data, "image/png")}
    )
    assert r.status_code == 200
    rows = {row[0]: row for row in _uploads(clean_db)}
    assert rows[r.json()["stem"]][4] is None


async def test_upload_survives_a_lost_record(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _login(client)

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(admin.uploads, "record", boom)
    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("a.png", _png(), "image/png")}
    )
    assert r.status_code == 200, r.text
    assert (get_settings().inbox / f"{r.json()['stem']}.png").exists()
    assert _uploads(clean_db) == []


async def test_upload_writes_the_row_before_any_file_lands(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is the one await in the upload; a cancelled request at that point must
    # find nothing in the inbox but dotfile temps, never sidecars without their image.
    await _login(client)
    events: list[str] = []
    real_rename, real_record = os.rename, admin.uploads.record
    seen: list[list[str]] = []

    def rename(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        events.append(f"rename {Path(dst).name.rpartition('.')[2]}")
        real_rename(src, dst, *a, **k)

    async def record(*a: Any, **k: Any) -> None:
        events.append("record")
        seen.append(sorted(p.name for p in get_settings().inbox.iterdir() if p.name[0] != "."))
        await real_record(*a, **k)

    monkeypatch.setattr(admin.os, "rename", rename)
    monkeypatch.setattr(admin.uploads, "record", record)
    r = await client.post(
        "/api/admin/upload",
        headers=XHR,
        files={"image": ("a.png", _png(), "image/png")},
        data={"caption": "a cat", "tags": "cat"},
    )
    assert r.status_code == 200, r.text
    assert events == ["record", "rename txt", "rename txt", "rename png"]
    assert seen == [[]]


async def test_a_cancelled_record_leaves_the_inbox_empty(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _login(client)

    async def cancelled(*_: object, **__: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(admin, "_record", cancelled)
    # The cancellation is the server's to swallow; the client only needs to see no files.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(
            client.post(
                "/api/admin/upload",
                headers=XHR,
                files={"image": ("a.png", _png(), "image/png")},
                data={"caption": "a cat"},
            ),
            timeout=5,
        )
    inbox = get_settings().inbox
    assert not inbox.exists() or list(inbox.iterdir()) == []


def _ftyp(major: bytes, *compatible: bytes) -> bytes:
    body = b"ftyp" + major + b"\0\0\0\0" + b"".join(compatible)
    return (len(body) + 4).to_bytes(4, "big") + body + b"\0" * 64


@pytest.mark.parametrize(
    "head",
    [
        _ftyp(b"heic", b"mif1", b"heic"),
        _ftyp(b"heix", b"mif1"),
        _ftyp(b"mif1", b"miaf", b"heic"),
        _ftyp(b"msf1", b"hevc"),
    ],
)
async def test_upload_takes_heic(client: httpx.AsyncClient, head: bytes) -> None:
    await _login(client)
    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("IMG_0001.HEIC", head, "image/heic")}
    )
    assert r.status_code == 200, r.text
    stem = r.json()["stem"]
    assert sorted(p.name for p in get_settings().inbox.iterdir()) == [f"{stem}.heic"]
    assert (get_settings().inbox / f"{stem}.heic").read_bytes() == head


@pytest.mark.parametrize(
    "head",
    [
        _ftyp(b"avif", b"mif1", b"avif"),
        _ftyp(b"mif1", b"avif", b"miaf"),
        # The brand list stops at the box's end; a brand past it does not count.
        _ftyp(b"mif1", b"avif")[:24] + b"heic",
        _ftyp(b"isom", b"heic"),
        bytes(range(256)),
    ],
)
async def test_upload_refuses_avif_and_noise(client: httpx.AsyncClient, head: bytes) -> None:
    await _login(client)
    r = await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("a.heic", head, "image/heic")}
    )
    assert r.status_code == 415
    inbox = get_settings().inbox
    assert not inbox.exists() or list(inbox.iterdir()) == []


# -- the read token --------------------------------------------------------------------

TOKEN = "r3ad-only-" + "k" * 30


def _spent() -> bool:
    return any(admin._failures.values()) or any(admin._token_failures.values())


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _token_client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> AsyncIterator[httpx.AsyncClient]:
    if token is None:
        monkeypatch.delenv("ADMIN_READ_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ADMIN_READ_TOKEN", token)
    async for c in _serve(_build(clean_db, tmp_path, monkeypatch, PW)):
        yield c


@pytest.fixture
async def reader(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _token_client(clean_db, tmp_path, monkeypatch, TOKEN):
        yield c


async def test_token_reads(reader: httpx.AsyncClient, clean_db: str) -> None:
    for path in ("me", "queue", "system", "stats", "logs", "collections/pansophicon", "shas"):
        r = await reader.get(f"/api/admin/{path}", headers=_bearer())
        assert r.status_code == 200, (path, r.text)
        assert r.headers["cache-control"] == "no-store"
    assert "penish_admin" not in reader.cookies
    assert (
        await reader.get("/api/admin/me", headers={"authorization": f"bearer {TOKEN}"})
    ).status_code == 200


async def test_token_cannot_write(reader: httpx.AsyncClient, clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        job = conn.execute(
            "insert into jobs (type, status, last_error) values ('x', 'failed', 'boom')"
            " returning id"
        ).fetchone()
        settings_before = conn.execute("select count(*) from settings").fetchone()
    assert job is not None
    h = {**XHR, **_bearer()}
    tries = [
        reader.post(
            "/api/admin/upload", headers=h, files={"image": ("a.png", _png(), "image/png")}
        ),
        reader.patch("/api/admin/collections/pansophicon", headers=h, json={"title": "x"}),
        reader.post(f"/api/admin/queue/{job[0]}/retry", headers=h),
        reader.put("/api/admin/models", headers=h, json={"key": "describe.model", "value": "x"}),
        reader.delete(f"/api/admin/queue/{job[0]}", headers=h),
    ]
    for t in tries:
        r = await t
        assert r.status_code == 401, r.text
        assert r.json() == {"detail": "no"}
    inbox = get_settings().inbox
    assert not inbox.exists() or list(inbox.iterdir()) == []
    with psycopg.connect(clean_db, autocommit=True) as conn:
        assert conn.execute("select status::text from jobs").fetchall() == [("failed",)]
        title = conn.execute("select title from collections where slug = 'pansophicon'").fetchone()
        assert title is not None and title[0] != "x"
        assert conn.execute("select count(*) from settings").fetchone() == settings_before
    # Refused writes are not guesses: they do not spend the limiter.
    assert not _spent()


async def test_wrong_token_is_refused_and_limited(reader: httpx.AsyncClient) -> None:
    ip = {"cf-connecting-ip": "3.3.3.3"}
    for bad in ("x" * 40, TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        r = await reader.get("/api/admin/me", headers={**ip, **_bearer(bad)})
        assert r.status_code == 401
    await reader.get("/api/admin/me", headers={**ip, **_bearer("y" * 40)})
    # Spent: even the right token waits from this address, and another address is untouched.
    r = await reader.get("/api/admin/me", headers={**ip, **_bearer()})
    assert r.status_code == 429
    r = await reader.get("/api/admin/me", headers={"cf-connecting-ip": "4.4.4.4", **_bearer()})
    assert r.status_code == 200
    # The token's bucket is not the login's: the password still works from the address a
    # stale token locked, and it did not borrow from the login bucket to get there.
    assert not any(admin._failures.values())
    r = await reader.post("/api/admin/login", json={"password": PW}, headers={**XHR, **ip})
    assert r.status_code == 200


async def test_locked_login_does_not_stop_a_correct_token(reader: httpx.AsyncClient) -> None:
    ip = {"cf-connecting-ip": "5.5.5.5"}
    for _ in range(admin.LOGIN_FAILURES):
        r = await reader.post("/api/admin/login", json={"password": "wrong"}, headers={**XHR, **ip})
        assert r.status_code == 401
    r = await reader.post("/api/admin/login", json={"password": PW}, headers={**XHR, **ip})
    assert r.status_code == 429
    assert (await reader.get("/api/admin/me", headers={**ip, **_bearer()})).status_code == 200


async def test_token_misses_count_toward_the_global_budget(reader: httpx.AsyncClient) -> None:
    for i in range(admin.GLOBAL_LOGIN_FAILURES):
        admin._token_failures[f"10.0.0.{i}"].append(time.time())
    ip = {"cf-connecting-ip": "10.9.9.9"}
    # Spent for everyone: a wrong guess, from a fresh address, is turned away as a 429.
    r = await reader.get("/api/admin/me", headers={**ip, **_bearer("w" * 40)})
    assert r.status_code == 429
    # The right credentials still pass.
    assert (await reader.get("/api/admin/me", headers={**ip, **_bearer()})).status_code == 200
    r = await reader.post("/api/admin/login", json={"password": "wrong"}, headers={**XHR, **ip})
    assert r.status_code == 429


async def test_head_with_a_valid_token_is_not_served_by_the_admin_route(
    reader: httpx.AsyncClient,
) -> None:
    # The admin routes are GET only, so the catch-all answers HEAD; the token never opens
    # anything it was not honoured for.
    r = await reader.head("/api/admin/me", headers=_bearer())
    assert r.status_code == 404
    assert "no-store" in r.headers["cache-control"]


async def test_short_token_is_off(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = "s" * 31
    async for c in _token_client(clean_db, tmp_path, monkeypatch, short):
        assert (await c.get("/api/admin/me", headers=_bearer(short))).status_code == 401
        assert (await c.get("/api/admin/me", headers=_bearer(short + "s"))).status_code == 401
        assert not _spent()


async def test_unset_token_ignores_bearer(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async for c in _token_client(clean_db, tmp_path, monkeypatch, None):
        assert get_settings().admin_read_token is None
        assert (await c.get("/api/admin/me", headers=_bearer())).status_code == 401
        assert (await c.get("/api/admin/me", headers=_bearer(""))).status_code == 401
        assert not _spent()


async def test_token_does_not_wake_a_switched_off_admin(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_READ_TOKEN", TOKEN)
    async for c in _serve(_build(clean_db, tmp_path, monkeypatch, None)):
        for path in ("me", "queue", "shas"):
            assert (await c.get(f"/api/admin/{path}", headers=_bearer())).status_code == 404


async def test_cookie_still_works_with_a_token_set(reader: httpx.AsyncClient) -> None:
    assert (await reader.get("/api/admin/me")).status_code == 401
    await _login(reader)
    assert (await reader.get("/api/admin/me")).status_code == 200
    r = await reader.post(
        "/api/admin/upload", headers=XHR, files={"image": ("a.png", _png(), "image/png")}
    )
    assert r.status_code == 200
    # A wrong bearer beside a good cookie is not a guess; the cookie is enough.
    assert (await reader.get("/api/admin/me", headers=_bearer("z" * 40))).status_code == 200
    assert not _spent()


async def test_token_never_shows(
    reader: httpx.AsyncClient, clean_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    bodies: list[str] = []
    for path in ("me", "queue", "system", "logs", "shas", "uploads"):
        bodies.append((await reader.get(f"/api/admin/{path}", headers=_bearer())).text)
    bodies.append((await reader.get("/api/admin/me", headers=_bearer(TOKEN + "x"))).text)
    bodies.append((await reader.post("/api/admin/upload", headers={**XHR, **_bearer()})).text)
    assert all(TOKEN not in b for b in bodies)
    assert TOKEN not in caplog.text
    assert all(TOKEN not in str(r.__dict__) for r in caplog.records)


# -- what the site holds -----------------------------------------------------------------


async def test_shas_and_uploads(reader: httpx.AsyncClient, clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "insert into images (sha256, slug, status) values"
            " (%s, 'a-cat', 'live'), (%s, null, 'failed'), (%s, 'gone', 'unpublished')",
            ("a1" * 32, "b2" * 32, "c3" * 32),
        )
        conn.execute(
            "insert into uploads (stem, sha256, filename, bytes, collection_line, source, error)"
            " values ('a-cat-abc123', %s, 'A.PNG', 9, 'pansophicon', 'form', null),"
            " ('broken', %s, null, null, null, 'rsync', 'broken.png: no')",
            ("a1" * 32, "d4" * 32),
        )
    r = await reader.get("/api/admin/shas", headers=_bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["images"] == [
        {"sha256": "a1" * 32, "status": "live", "slug": "a-cat"},
        {"sha256": "b2" * 32, "status": "failed", "slug": None},
        {"sha256": "c3" * 32, "status": "unpublished", "slug": "gone"},
    ]
    assert sorted(body["uploads"], key=lambda u: u["stem"]) == [
        {"sha256": "a1" * 32, "stem": "a-cat-abc123", "stage": "unfiled", "error": None},
        {"sha256": "d4" * 32, "stem": "broken", "stage": "failed", "error": "broken.png: no"},
    ]

    every = (await reader.get("/api/admin/uploads", headers=_bearer())).json()["uploads"]
    assert {u["stem"] for u in every} == {"a-cat-abc123", "broken"}
    mine = (
        await reader.get(
            "/api/admin/uploads", params={"collection": "pansophicon"}, headers=_bearer()
        )
    ).json()["uploads"]
    assert len(mine) == 1
    got = mine[0]
    assert set(got) == {
        "stem",
        "sha256",
        "filename",
        "bytes",
        "collection_line",
        "source",
        "stage",
        "error",
        "image_id",
        "slug",
        "created_at",
    }
    assert (got["filename"], got["bytes"], got["source"], got["slug"]) == (
        "A.PNG",
        9,
        "form",
        "a-cat",
    )
    assert isinstance(got["created_at"], str) and "T" in got["created_at"]
    r = await reader.get("/api/admin/uploads", params={"collection": "nothing"}, headers=_bearer())
    assert r.json() == {"uploads": []}
