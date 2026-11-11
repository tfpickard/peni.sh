import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from penish.api import admin_ops
from penish.config import get_settings
from tests.test_admin import PW, XHR, _build, _login

SHA_A = "aa" * 32
SHA_B = "bb" * 32


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    app = _build(clean_db, tmp_path, monkeypatch, PW)
    # app.py does not include the router yet and the index catch-all is registered last,
    # so the routes go in front of it.
    app.router.routes[0:0] = list(admin_ops.router.routes)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            await _login(c)
            yield c
    get_settings.cache_clear()


def sql(dsn: str, query: str, *args: Any) -> list[Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(query, args)  # type: ignore[arg-type]
        return cur.fetchall() if cur.description else []


def image(dsn: str, sha: str, slug: str, status: str = "live") -> int:
    return int(
        sql(
            dsn,
            "insert into images (sha256, slug, status, width, height) values (%s, %s, %s, 960, 640)"
            " returning id",
            sha,
            slug,
            status,
        )[0][0]
    )


def data(tmp_path: Path) -> Path:
    return tmp_path / "data"


async def test_requires_login(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build(clean_db, tmp_path, monkeypatch, PW)
    app.router.routes[0:0] = list(admin_ops.router.routes)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            for path in ("comments", "haikus", "models", "queue", "stats", "system"):
                r = await c.get(f"/api/admin/{path}")
                assert r.status_code == 401
                assert r.headers["cache-control"] == "no-store"
    get_settings.cache_clear()


async def test_all_no_store_and_post_needs_header(client: httpx.AsyncClient) -> None:
    for path in ("comments", "haikus", "models", "queue", "stats", "system"):
        r = await client.get(f"/api/admin/{path}")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
    r = await client.post("/api/admin/haikus", json={"lines": ["a", "b", "c"]})
    assert r.status_code == 403


# -- comments -------------------------------------------------------------


async def test_comment_flow(client: httpx.AsyncClient, clean_db: str, tmp_path: Path) -> None:
    img = image(clean_db, SHA_A, "a-cat")
    pub = data(tmp_path) / "media" / "pub"
    pub.mkdir(parents=True)
    for w in (240, 480, 960):
        (pub / f"{SHA_A}-{w}.webp").write_bytes(b"x")
    cid = sql(
        clean_db,
        "insert into comments (image_id, name, body) values (%s, 'n', 'hello') returning id",
        img,
    )[0][0]

    r = await client.get("/api/admin/comments")
    [item] = r.json()
    assert item["id"] == cid and item["slug"] == "a-cat" and item["status"] == "pending"
    assert item["thumb"] == f"/media/pub/{SHA_A}-480.webp"
    assert item["decided_at"] is None
    assert (await client.get("/api/admin/comments?status=approved")).json() == []
    assert (await client.get("/api/admin/comments?status=bogus")).status_code == 422

    r = await client.post(f"/api/admin/comments/{cid}", json={"status": "approved"}, headers=XHR)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["decided_at"]
    assert sql(clean_db, "select decided_at is not null from comments")[0][0]
    assert (await client.get("/api/admin/comments")).json() == []
    [item] = (await client.get("/api/admin/comments?status=approved")).json()
    assert item["id"] == cid

    r = await client.post(f"/api/admin/comments/{cid}", json={"status": "rejected"}, headers=XHR)
    assert r.json()["status"] == "rejected"
    r = await client.post(f"/api/admin/comments/{cid}", json={"status": "pending"}, headers=XHR)
    assert r.status_code == 422
    r = await client.post("/api/admin/comments/999", json={"status": "approved"}, headers=XHR)
    assert r.status_code == 404

    assert (await client.delete(f"/api/admin/comments/{cid}")).status_code == 200
    assert sql(clean_db, "select count(*) from comments")[0][0] == 0
    assert (await client.delete(f"/api/admin/comments/{cid}")).status_code == 404


async def test_comment_without_thumb(client: httpx.AsyncClient, clean_db: str) -> None:
    img = image(clean_db, SHA_A, "a-cat")
    sql(clean_db, "insert into comments (image_id, body) values (%s, 'x')", img)
    [item] = (await client.get("/api/admin/comments")).json()
    assert item["thumb"] is None and item["name"] is None


# -- haikus ---------------------------------------------------------------


async def test_haiku_crud(client: httpx.AsyncClient, clean_db: str) -> None:
    lines = [" one ", "two " + chr(0x2014) + " two", "three"]
    r = await client.post("/api/admin/haikus", json={"lines": lines}, headers=XHR)
    assert r.status_code == 200
    h = r.json()
    assert h["lines"] == ["one", "two -- two", "three"]
    assert h["source"] == "owner" and h["active"] is True

    dup = await client.post(
        "/api/admin/haikus", json={"lines": ["ONE", "two -- two", "three"]}, headers=XHR
    )
    assert dup.status_code == 409

    for bad in (["a", "b"], ["a", "b", "c", "d"], ["a", " ", "c"], ["a", "b", "x" * 61]):
        r = await client.post("/api/admin/haikus", json={"lines": bad}, headers=XHR)
        assert r.status_code == 422, bad
    ok = await client.post("/api/admin/haikus", json={"lines": ["a", "b", "x" * 60]}, headers=XHR)
    assert ok.status_code == 200

    r = await client.get("/api/admin/haikus?source=owner")
    body = r.json()
    assert body["total"] == 2 and len(body["items"]) == 2
    assert (await client.get("/api/admin/haikus?source=seed")).json()["total"] > 0
    assert (await client.get("/api/admin/haikus?source=nope")).status_code == 422

    r = await client.patch(f"/api/admin/haikus/{h['id']}", json={"active": False})
    assert r.json()["active"] is False
    assert (await client.get("/api/admin/haikus?source=owner&active=false")).json()["total"] == 1
    r = await client.patch(f"/api/admin/haikus/{h['id']}", json={"lines": ["x", "y", "z"]})
    assert r.json()["lines"] == ["x", "y", "z"]
    r = await client.patch(f"/api/admin/haikus/{h['id']}", json={"lines": ["x", "y"]})
    assert r.status_code == 422
    assert (await client.patch(f"/api/admin/haikus/{h['id']}", json={})).status_code == 422
    assert (
        await client.patch("/api/admin/haikus/999999", json={"active": True})
    ).status_code == 404

    page = (await client.get("/api/admin/haikus?limit=1&offset=1")).json()
    assert len(page["items"]) == 1 and page["total"] > 2

    assert (await client.delete(f"/api/admin/haikus/{h['id']}")).status_code == 200
    assert (await client.delete(f"/api/admin/haikus/{h['id']}")).status_code == 404


async def test_haiku_grow_enqueues(client: httpx.AsyncClient, clean_db: str) -> None:
    for bad in (0, 201, "x"):
        r = await client.post("/api/admin/haikus/grow", json={"n": bad}, headers=XHR)
        assert r.status_code == 422
    r = await client.post("/api/admin/haikus/grow", json={"n": 25}, headers=XHR)
    assert r.status_code == 200 and r.json()["job_id"]
    rows = sql(clean_db, "select type, payload, status::text from jobs")
    assert rows == [("grow_haikus", {"n": 25}, "queued")]


# -- models ---------------------------------------------------------------


async def test_models(client: httpx.AsyncClient, clean_db: str) -> None:
    r = await client.get("/api/admin/models")
    body = r.json()
    assert body["current"] == body["default"]
    assert body["current"]["describe.effort"] is None
    assert "grow.model" in body["current"]
    assert "claude-opus-5-5" in body["known_models"] and "xhigh" in body["efforts"]

    r = await client.put(
        "/api/admin/models", json={"key": "describe.model", "value": "claude-custom"}, headers=XHR
    )
    assert r.status_code == 200 and r.json()["value"] == "claude-custom"
    body = (await client.get("/api/admin/models")).json()
    assert body["current"]["describe.model"] == "claude-custom"
    assert body["default"]["describe.model"] != "claude-custom"
    assert "claude-custom" in body["known_models"]

    r = await client.put(
        "/api/admin/models", json={"key": "describe.effort", "value": "xhigh"}, headers=XHR
    )
    assert r.status_code == 200
    r = await client.put(
        "/api/admin/models", json={"key": "describe.effort", "value": "extreme"}, headers=XHR
    )
    assert r.status_code == 422
    r = await client.put(
        "/api/admin/models", json={"key": "describe.effort", "value": None}, headers=XHR
    )
    assert r.status_code == 200
    r = await client.put(
        "/api/admin/models", json={"key": "haiku.prompt", "value": " "}, headers=XHR
    )
    assert r.status_code == 422
    r = await client.put("/api/admin/models", json={"key": "nope", "value": "x"}, headers=XHR)
    assert r.status_code == 422

    r = await client.delete("/api/admin/models/describe.model")
    assert r.status_code == 200 and r.json()["value"] != "claude-custom"
    assert sql(clean_db, "select count(*) from settings where key = 'describe.model'")[0][0] == 0
    assert (await client.delete("/api/admin/models/nope")).status_code == 422


async def test_bulk_jobs_dedupe(client: httpx.AsyncClient, clean_db: str) -> None:
    for kind in ("reembed_all", "redescribe_all"):
        first = await client.post(f"/api/admin/models/{kind}", headers=XHR)
        assert first.json()["queued"] is True
        second = await client.post(f"/api/admin/models/{kind}", headers=XHR)
        assert second.json() == {"job_id": None, "queued": False}
    rows = sql(clean_db, "select type, dedupe_key from jobs order by id")
    assert rows == [("reembed_all", "reembed_all"), ("redescribe_all", "redescribe_all")]


# -- queue ----------------------------------------------------------------


async def test_queue_jobs(client: httpx.AsyncClient, clean_db: str) -> None:
    sql(
        clean_db,
        "insert into jobs (type, status, attempts, last_error, run_after) values"
        " ('old', 'failed', 12, 'boom', now() + interval '1 day'),"
        " ('done', 'done', 1, null, now()),"
        " ('new', 'queued', 0, null, now())",
    )
    jobs = (await client.get("/api/admin/queue")).json()["jobs"]
    assert [j["type"] for j in jobs] == ["new", "old"]
    old = jobs[1]
    assert old["last_error"] == "boom" and old["attempts"] == 12

    r = await client.post(f"/api/admin/queue/{old['id']}/retry", headers=XHR)
    assert r.status_code == 200
    row = sql(
        clean_db,
        "select status::text, attempts, run_after <= now() from jobs where id = %s",
        old["id"],
    )[0]
    assert row == ("queued", 0, True)
    assert (await client.post("/api/admin/queue/9999/retry", headers=XHR)).status_code == 404

    assert (await client.delete(f"/api/admin/queue/{old['id']}")).status_code == 200
    assert (await client.delete(f"/api/admin/queue/{old['id']}")).status_code == 404


async def test_retry_conflicting_dedupe(client: httpx.AsyncClient, clean_db: str) -> None:
    sql(
        clean_db,
        "insert into jobs (type, status, dedupe_key) values"
        " ('x', 'failed', 'k'), ('x', 'queued', 'k')",
    )
    failed_id = sql(clean_db, "select id from jobs where status = 'failed'")[0][0]
    r = await client.post(f"/api/admin/queue/{failed_id}/retry", headers=XHR)
    assert r.status_code == 409


def make_failed(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    d = data(tmp_path) / "failed" / name
    d.mkdir(parents=True)
    for fname, text in files.items():
        (d / fname).write_text(text)
    return d


async def test_failed_listing_caps_err(client: httpx.AsyncClient, tmp_path: Path) -> None:
    make_failed(tmp_path, "cat", {"cat.png": "x", "cat.err": "e" * 5000})
    make_failed(tmp_path, "dog", {"dog.png": "x"})
    (data(tmp_path) / "failed" / "stray.txt").write_text("not a dir")
    failed = (await client.get("/api/admin/queue")).json()["failed"]
    by = {f["name"]: f for f in failed}
    assert set(by) == {"cat", "dog"}
    assert len(by["cat"]["err"]) == 2048 and by["cat"]["files"] == ["cat.err", "cat.png"]
    assert by["dog"]["err"] == ""


async def test_failed_no_dir(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/admin/queue")).json()["failed"] == []


async def test_failed_retry_moves_back(client: httpx.AsyncClient, tmp_path: Path) -> None:
    d = make_failed(tmp_path, "cat", {"cat.png": "img", "cat_caption.txt": "c", "cat.err": "boom"})
    r = await client.post("/api/admin/queue/failed/cat/retry", headers=XHR)
    assert r.status_code == 200
    inbox = data(tmp_path) / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["cat.png", "cat_caption.txt"]
    assert (inbox / "cat.png").read_text() == "img"
    assert not d.exists()


async def test_failed_retry_collision_suffixes_whole_group(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    inbox = data(tmp_path) / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "cat.png").write_text("other")
    make_failed(tmp_path, "cat", {"cat.png": "img", "cat_caption.txt": "c", "cat.err": "boom"})
    r = await client.post("/api/admin/queue/failed/cat/retry", headers=XHR)
    assert r.status_code == 200
    names = sorted(p.name for p in inbox.iterdir())
    assert (inbox / "cat.png").read_text() == "other"
    new = [n for n in names if n != "cat.png"]
    assert len(new) == 2
    img = next(n for n in new if n.endswith(".png"))
    side = next(n for n in new if n.endswith("_caption.txt"))
    stem = img[: -len(".png")]
    assert stem.startswith("cat-r") and len(stem) == len("cat-r") + 4
    assert side == f"{stem}_caption.txt"
    assert (inbox / img).read_text() == "img"
    assert not (data(tmp_path) / "failed" / "cat").exists()


async def test_failed_delete(client: httpx.AsyncClient, tmp_path: Path) -> None:
    d = make_failed(tmp_path, "cat", {"cat.png": "x"})
    r = await client.delete("/api/admin/queue/failed/cat")
    assert r.status_code == 200 and not d.exists()
    assert (await client.delete("/api/admin/queue/failed/cat")).status_code == 404
    assert (await client.post("/api/admin/queue/failed/cat/retry", headers=XHR)).status_code == 404


@pytest.mark.parametrize(
    "name",
    ["..", ".", ".hidden", "a%2Fb", "..%2Fdata", "%2e%2e", "a..%2F..", "cat%00", "a b", "x."],
)
async def test_failed_names_rejected(client: httpx.AsyncClient, tmp_path: Path, name: str) -> None:
    victim = data(tmp_path) / "victim"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("keep")
    make_failed(tmp_path, "cat", {"cat.png": "x"})
    for r in (
        await client.delete(f"/api/admin/queue/failed/{name}"),
        await client.post(f"/api/admin/queue/failed/{name}/retry", headers=XHR),
    ):
        # 405 when an encoded slash makes the path miss every route.
        assert r.status_code in (404, 405, 422)
    assert (victim / "keep.txt").exists()
    assert (data(tmp_path) / "failed" / "cat" / "cat.png").exists()


async def test_failed_symlink_rejected(client: httpx.AsyncClient, tmp_path: Path) -> None:
    victim = data(tmp_path) / "victim"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("keep")
    (data(tmp_path) / "failed").mkdir()
    os.symlink(victim, data(tmp_path) / "failed" / "link")
    assert (await client.delete("/api/admin/queue/failed/link")).status_code == 422
    assert (victim / "keep.txt").exists()


# -- stats ----------------------------------------------------------------


async def test_stats(client: httpx.AsyncClient, clean_db: str, tmp_path: Path) -> None:
    a = image(clean_db, SHA_A, "a-cat")
    b = image(clean_db, SHA_B, "b-dog")
    image(clean_db, "cc" * 32, "c-fish", status="queued")
    pub = data(tmp_path) / "media" / "pub"
    pub.mkdir(parents=True)
    (pub / f"{SHA_A}-480.webp").write_bytes(b"x")
    sql(
        clean_db,
        "insert into votes (image_id, voter, value) values"
        " (%s, 'v1', 1), (%s, 'v2', 1), (%s, 'v3', -1), (%s, 'v1', -1)",
        a,
        a,
        a,
        b,
    )
    sql(clean_db, "insert into comments (image_id, body) values (%s, 'x')", a)
    sql(
        clean_db,
        "insert into corpus_stats_history (kind, model, n, temperature) values"
        " ('description', 'm', 3, 0.5), ('image', 'm', 3, 0.1)",
    )
    sql(
        clean_db,
        "insert into api_usage (at, purpose, model, input_tokens, output_tokens) values"
        " (now(), 'describe', 'm1', 10, 5), (now(), 'haiku', 'm1', 1, 2),"
        " (now(), 'describe', 'm2', 7, 7), (now() - interval '40 days', 'describe', 'm1', 99, 99)",
    )
    body = (await client.get("/api/admin/stats")).json()
    assert body["pictures"] == 3
    assert body["by_status"] == {"live": 2, "queued": 1}
    assert body["votes_total"] == 4 and body["comments_pending"] == 1
    assert [(p["slug"], p["net"]) for p in body["top"]] == [("a-cat", 1)]
    assert body["top"][0]["thumb"] == f"/media/pub/{SHA_A}-480.webp"
    assert [(p["slug"], p["net"]) for p in body["bottom"]] == [("b-dog", -1)]
    assert body["bottom"][0]["thumb"] is None
    [t] = body["temperature_history"]
    assert t["temperature"] == 0.5 and t["n"] == 3
    usage = {u["model"]: u for u in body["api_usage"]["last_30d"]}
    assert (usage["m1"]["in"], usage["m1"]["out"]) == (11, 7)
    assert (usage["m2"]["in"], usage["m2"]["out"]) == (7, 7)
    assert len(usage["m1"]["day"]) == 10


async def test_stats_empty(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/admin/stats")).json()
    assert body["pictures"] == 0 and body["by_status"] == {}
    assert body["top"] == [] and body["bottom"] == [] and body["temperature_history"] == []
    assert body["api_usage"] == {"last_30d": []}


# -- system ---------------------------------------------------------------


async def test_system(
    client: httpx.AsyncClient,
    clean_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    data(tmp_path).mkdir(exist_ok=True)
    body = (await client.get("/api/admin/system")).json()
    assert body["deployed_sha"] == get_settings().git_sha
    assert body["worker_heartbeat_at"] is None and body["last_backup_at"] is None
    assert body["recent_errors"] == []
    assert body["db_size"] > 0
    assert body["disk"]["total"] >= body["disk"]["used"] > 0 and body["disk"]["free"] > 0

    sql(
        clean_db,
        "insert into heartbeats (name, info) values ('worker', '{\"queue\": 2}')",
    )
    for i in range(25):
        sql(
            clean_db,
            "insert into jobs (type, status, last_error) values ('t', 'failed', %s)",
            f"e{i}",
        )
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "a.sql").write_text("x")
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    body = (await client.get("/api/admin/system")).json()
    assert body["worker_heartbeat_at"] and body["worker_info"] == {"queue": 2}
    assert body["last_backup_at"]
    assert len(body["recent_errors"]) == 20
    assert body["recent_errors"][0]["error"] == "e24"

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "missing"))
    assert (await client.get("/api/admin/system")).json()["last_backup_at"] is None
