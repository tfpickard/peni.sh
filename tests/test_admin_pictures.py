from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pyvips
from fastapi import FastAPI
from psycopg.types.json import Jsonb

from penish.api import admin, admin_pictures, embed_client, index
from penish.api.app import create_app
from penish.config import get_settings

PW = "hunter2-hunter2-hunter2"
XHR = {"X-Requested-With": "penish"}
MODEL = "test/embed"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
URL = "/api/admin/pictures"


def sha(n: int) -> str:
    return f"{n:064x}"


def _mount(app: FastAPI) -> FastAPI:
    # Until app.py includes the router; a no-op once it does. The index catch-all has to
    # stay last or it would answer for these paths.
    routes = app.router.routes
    if any(getattr(r, "original_router", None) is admin_pictures.router for r in routes):
        return app
    app.include_router(admin_pictures.router)
    for r in [r for r in routes if getattr(r, "original_router", None) is index.router]:
        routes.remove(r)
        routes.append(r)
    return app


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    admin._failures.clear()
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><html><head><!--og--></head></html>")
    data = tmp_path / "data"
    (data / "media" / "pub").mkdir(parents=True)
    (data / "media" / "orig").mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(web))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    monkeypatch.setenv("EMBED_MODEL", MODEL)
    monkeypatch.setenv("EMBED_DIM", "4")
    get_settings.cache_clear()
    app = _mount(create_app())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            r = await c.post("/api/admin/login", json={"password": PW}, headers=XHR)
            assert r.status_code == 200
            yield c
    get_settings.cache_clear()


def png(w: int = 200, h: int = 100) -> bytes:
    out = pyvips.Image.black(w, h).write_to_buffer(".png")
    assert isinstance(out, bytes)
    return out


def add(
    dsn: str,
    n: int,
    *,
    status: str = "live",
    caption: str | None = None,
    description: str | None = None,
    analysis: str | None = None,
    vec: list[float] | None = None,
    model: str = MODEL,
    pub_n: int | None = None,
    orig: bool = True,
) -> int:
    s = get_settings()
    pub_sha = sha(n if pub_n is None else pub_n)
    for ext in ("avif", "webp"):
        (s.media_pub / f"{pub_sha}-200.{ext}").write_bytes(b"x")
    if orig:
        (s.media_orig / f"{sha(n)}.png").write_bytes(png())
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "insert into images (sha256, pub_sha, slug, width, height, status, created_at,"
            " published_at) values (%s, %s, %s, 200, 100, %s, %s, %s) returning id",
            (
                sha(n),
                pub_sha,
                f"img-{n}" if status == "live" else None,
                status,
                BASE + timedelta(minutes=n),
                BASE + timedelta(minutes=n) if status == "live" else None,
            ),
        ).fetchone()
        assert row
        image_id: int = row[0]
        for field, body in (
            ("caption", caption),
            ("description", description),
            ("analysis", analysis),
        ):
            if body:
                conn.execute(
                    "insert into image_texts (image_id, field, body, source, model)"
                    " values (%s, %s, %s, 'model', 'm')",
                    (image_id, field, body),
                )
        if vec is not None:
            conn.execute(
                "insert into embeddings (image_id, kind, vec, model, dim)"
                " values (%s, 'description', %s::vector, %s, %s)",
                (image_id, str(vec), model, len(vec)),
            )
        conn.commit()
    return image_id


def sql(dsn: str, query: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(query, args)
        return cur.fetchall() if cur.description else []


def vote(dsn: str, image_id: int, *values: int) -> None:
    for i, v in enumerate(values):
        sql(dsn, "insert into votes (image_id, voter, value) values (%s, %s, %s)", image_id, i, v)


def ids(body: dict[str, Any]) -> list[int]:
    return [i["id"] for i in body["items"]]


# -- list -------------------------------------------------------------------


async def test_requires_login(client: httpx.AsyncClient) -> None:
    client.cookies.clear()
    assert (await client.get(URL)).status_code == 401
    assert (await client.get(f"{URL}/1")).status_code == 401


async def test_list_sorts_filters_and_pages(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, caption="first")
    b = add(clean_db, 2)
    c = add(clean_db, 3)
    d = add(clean_db, 4, status="queued")
    vote(clean_db, a, 1, 1, -1)
    vote(clean_db, c, -1)

    r = await client.get(URL)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert ids(body) == [d, c, b, a] and body["next_cursor"] is None
    item = body["items"][-1]
    assert item == {
        "id": a,
        "slug": "img-1",
        "status": "live",
        "thumb": f"/media/pub/{sha(1)}-200.webp",
        "caption": "first",
        "net": 1,
        "created_at": (BASE + timedelta(minutes=1)).isoformat(),
    }
    assert ids((await client.get(URL, params={"sort": "old"})).json()) == [a, b, c, d]
    assert ids((await client.get(URL, params={"status": "queued"})).json()) == [d]
    assert ids((await client.get(URL, params={"sort": "votes"})).json()) == [a, d, b, c]

    page = (await client.get(URL, params={"limit": 3})).json()
    assert ids(page) == [d, c, b] and page["next_cursor"] == "3"
    page = (await client.get(URL, params={"limit": 3, "cursor": page["next_cursor"]})).json()
    assert ids(page) == [a] and page["next_cursor"] is None

    assert (await client.get(URL, params={"cursor": "x"})).status_code == 400
    assert (await client.get(URL, params={"sort": "nope"})).status_code == 422
    assert (await client.get(URL, params={"status": "nope"})).status_code == 422


async def test_list_lonely(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[1, 0.1, 0, 0])
    c = add(clean_db, 3, vec=[0, 0, 1, 0])
    none = add(clean_db, 4)
    other = add(clean_db, 5, vec=[0, 1, 0, 0], model="other")
    r = await client.get(URL, params={"sort": "lonely"})
    order = ids(r.json())
    assert order[0] == c and set(order[1:3]) == {a, b}
    # No vector under the current model: unranked, at the end.
    assert set(order[3:]) == {none, other}


async def test_search_ranks_by_similarity(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 0, 1, 0])
    c = add(clean_db, 3, status="pending_enrichment", vec=[0, 0.3, 1, 0])
    add(clean_db, 4, vec=[0, 0, 1, 0], model="other")
    add(clean_db, 5)
    asked: list[str] = []

    async def fake(text: str, **kw: Any) -> dict[str, Any]:
        asked.append(text)
        return {"vector": [0.0, 0.0, 1.0, 0.0], "model": MODEL, "dim": 4}

    monkeypatch.setattr(embed_client, "embed_query_full", fake)
    r = await client.get(URL, params={"q": "  a blue chair "})
    assert r.status_code == 200
    assert asked == ["a blue chair"]
    assert ids(r.json()) == [b, c, a]
    r = await client.get(URL, params={"q": "chair", "status": "live"})
    assert ids(r.json()) == [b, a]


async def test_search_503_when_embed_server_down(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(clean_db, 1, vec=[1, 0, 0, 0])
    # Nothing listens on the discard port, so the connect is refused immediately.
    monkeypatch.setenv("EMBED_SERVER_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    r = await client.get(URL, params={"q": "chair"})
    assert r.status_code == 503
    assert r.json() == {"detail": "search unavailable"}
    assert r.headers["cache-control"] == "no-store"


# -- detail -----------------------------------------------------------------


async def test_detail(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, caption="a cup", description="A cup.", analysis="Subject: a cup.")
    sql(clean_db, "update image_texts set source = 'owner', model = null where field = 'caption'")
    sql(clean_db, "update images set crop = %s where id = %s", Jsonb({"x": 1}), a)
    sql(
        clean_db,
        "insert into tags (image_id, tag, source, model) values (%s, 'cup', 'model', 'm')",
        a,
    )
    sql(clean_db, "insert into slug_aliases (slug, image_id) values ('old-cup', %s)", a)
    vote(clean_db, a, 1, 1, -1)
    for status in ("pending", "pending", "approved", "rejected"):
        sql(
            clean_db,
            "insert into comments (image_id, body, status) values (%s, 'hi', %s)",
            a,
            status,
        )
    for i in range(7):
        sql(
            clean_db,
            "insert into jobs (type, payload, status, last_error)"
            " values ('regen', %s, 'failed', %s)",
            Jsonb({"image_id": a, "fields": ["caption"]}),
            f"err {i}",
        )
    sql(clean_db, "insert into jobs (type, payload) values ('regen', %s)", Jsonb({"image_id": 999}))

    r = await client.get(f"{URL}/{a}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == a and d["slug"] == "img-1" and d["status"] == "live"
    assert d["caption"] == {"body": "a cup", "source": "owner", "model": None}
    assert d["description"] == {"body": "A cup.", "source": "model", "model": "m"}
    assert d["analysis"] == {"body": "Subject: a cup.", "source": "model", "model": "m"}
    assert d["prompt"] is None
    assert d["tags"] == [{"tag": "cup", "source": "model", "model": "m"}]
    assert d["crop"] == {"x": 1} and (d["width"], d["height"]) == (200, 100)
    assert d["original"] == {"width": 200, "height": 100}
    assert d["aliases"] == ["old-cup"]
    assert d["votes"] == {"up": 2, "down": 1, "net": 1}
    assert d["comments"] == {"pending": 2, "approved": 1, "rejected": 1}
    assert [j["last_error"] for j in d["jobs"]] == ["err 6", "err 5", "err 4", "err 3", "err 2"]
    assert d["jobs"][0]["status"] == "failed" and d["jobs"][0]["type"] == "regen"
    assert d["variants"][0]["webp"] == f"/media/pub/{sha(1)}-200.webp"

    assert (await client.get(f"{URL}/999")).status_code == 404


# -- patch ------------------------------------------------------------------


async def test_patch_texts_become_owner(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(
        clean_db, 1, caption="a cup", description="A cup.", analysis="Subject: a cup.", vec=[1, 0]
    )

    r = await client.patch(f"{URL}/{a}", json={"caption": " A Mug "}, headers=XHR)
    assert r.status_code == 200
    d = r.json()
    assert d["caption"] == {"body": "A Mug", "source": "owner", "model": None}
    # A caption edit here never renames.
    assert d["slug"] == "img-1" and d["aliases"] == []
    assert sql(clean_db, "select count(*) from embeddings") == [(1,)]
    assert sql(clean_db, "select count(*) from jobs") == [(0,)]

    # The description is public text only; the vector stays.
    r = await client.patch(f"{URL}/{a}", json={"description": "A mug."}, headers=XHR)
    assert r.json()["description"]["source"] == "owner"
    assert sql(clean_db, "select count(*) from embeddings") == [(1,)]
    assert sql(clean_db, "select count(*) from jobs") == [(0,)]

    r = await client.patch(f"{URL}/{a}", json={"analysis": "Subject: a mug."}, headers=XHR)
    assert r.json()["analysis"] == {"body": "Subject: a mug.", "source": "owner", "model": None}
    assert sql(clean_db, "select count(*) from embeddings") == [(0,)]
    jobs = sql(clean_db, "select type, payload, dedupe_key from jobs order by type")
    assert jobs == [
        # The split facets went with the old words.
        ("mycelium_split", {"image_id": a}, f"mycelium_split:{a}"),
        ("regen", {"image_id": a, "fields": ["embedding"]}, f"regen:{a}:embedding"),
    ]
    # Same text again: nothing new to embed.
    await client.patch(f"{URL}/{a}", json={"analysis": "Subject: a mug."}, headers=XHR)
    assert sql(clean_db, "select count(*) from jobs") == [(2,)]

    r = await client.patch(
        f"{URL}/{a}", json={"tags": ["Mug", " mug", "", "Steam"], "prompt": "p"}, headers=XHR
    )
    d = r.json()
    assert d["tags"] == [
        {"tag": "mug", "source": "owner", "model": None},
        {"tag": "steam", "source": "owner", "model": None},
    ]
    assert d["prompt"] == {"body": "p", "source": "owner", "model": None}
    r = await client.patch(f"{URL}/{a}", json={"prompt": ""}, headers=XHR)
    assert r.json()["prompt"] is None

    for bad in (
        {"caption": "  "},
        {"description": None},
        {"analysis": ""},
        {"nope": 1},
        {"tags": "x"},
    ):
        r = await client.patch(f"{URL}/{a}", json=bad, headers=XHR)
        assert r.status_code == 422, bad
    # A rejected edit rolls back whole: the slug in the same body did not land either.
    r = await client.patch(f"{URL}/{a}", json={"slug": "fine", "caption": ""}, headers=XHR)
    assert r.status_code == 422
    assert sql(clean_db, "select slug from images") == [("img-1",)]
    assert (await client.patch(f"{URL}/999", json={}, headers=XHR)).status_code == 404


async def test_patch_slug(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1)
    b = add(clean_db, 2)
    q = add(clean_db, 3, status="queued")
    sql(clean_db, "insert into slug_aliases (slug, image_id) values ('gone', %s)", b)

    for bad, code in (("Bad Slug", 422), ("a" * 81, 422), ("", 422), ("img-2", 409), ("gone", 409)):
        r = await client.patch(f"{URL}/{a}", json={"slug": bad}, headers=XHR)
        assert r.status_code == code, bad

    r = await client.patch(f"{URL}/{a}", json={"slug": "a-cup"}, headers=XHR)
    assert r.status_code == 200
    assert r.json()["slug"] == "a-cup" and r.json()["aliases"] == ["img-1"]
    # Taking an old slug back swaps it with the current one.
    r = await client.patch(f"{URL}/{a}", json={"slug": "img-1"}, headers=XHR)
    assert r.json()["slug"] == "img-1" and r.json()["aliases"] == ["a-cup"]
    # Unchanged is a no-op, not a self-alias.
    r = await client.patch(f"{URL}/{a}", json={"slug": "img-1"}, headers=XHR)
    assert r.json()["aliases"] == ["a-cup"]

    r = await client.patch(f"{URL}/{q}", json={"slug": "early"}, headers=XHR)
    assert r.status_code == 409


# -- regen and crop ---------------------------------------------------------


async def test_regen_enqueues_with_force_owner(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1)
    r = await client.post(
        f"{URL}/{a}/regen", json={"fields": ["haiku", "caption", "caption"]}, headers=XHR
    )
    assert r.status_code == 200
    assert r.json()["queued"] is True and isinstance(r.json()["job_id"], int)
    jobs = sql(clean_db, "select type, payload, dedupe_key from jobs")
    assert jobs == [
        (
            "regen",
            {"image_id": a, "fields": ["caption", "haiku"], "force_owner": True},
            f"regen:{a}:caption,haiku:owner",
        )
    ]
    r = await client.post(f"{URL}/{a}/regen", json={"fields": ["caption", "haiku"]}, headers=XHR)
    assert r.json() == {"job_id": None, "queued": False}

    assert (
        await client.post(f"{URL}/{a}/regen", json={"fields": []}, headers=XHR)
    ).status_code == 422
    r = await client.post(f"{URL}/{a}/regen", json={"fields": ["prompt"]}, headers=XHR)
    assert r.status_code == 422
    r = await client.post(f"{URL}/999/regen", json={"fields": ["caption"]}, headers=XHR)
    assert r.status_code == 404
    r = await client.post(f"{URL}/{a}/regen", json={"fields": ["caption"]})
    assert r.status_code == 403


async def test_crop_enqueues_clamped(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1)
    r = await client.post(
        f"{URL}/{a}/crop", json={"rect": {"x": -10, "y": 10.4, "w": 100, "h": 500}}, headers=XHR
    )
    assert r.status_code == 200 and r.json()["queued"] is True
    payloads = sql(clean_db, "select payload, dedupe_key from jobs")
    assert payloads == [({"image_id": a, "rect": {"x": 0, "y": 10, "w": 90, "h": 90}}, None)]

    for bad in ({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": "a", "y": 0, "w": 50, "h": 50}):
        r = await client.post(f"{URL}/{a}/crop", json={"rect": bad}, headers=XHR)
        assert r.status_code == 422
    assert (await client.post(f"{URL}/{a}/crop", json={}, headers=XHR)).status_code == 422
    assert (
        await client.post(f"{URL}/999/crop", json={"rect": None}, headers=XHR)
    ).status_code == 404

    # Same framing as now is a no-op, unless a pending crop is about to change it.
    sql(clean_db, "update jobs set status = 'done'")
    full = {"x": 0, "y": 0, "w": 200, "h": 100}
    r = await client.post(f"{URL}/{a}/crop", json={"rect": full}, headers=XHR)
    assert r.json() == {"job_id": None, "queued": False}
    r = await client.post(f"{URL}/{a}/crop", json={"rect": {**full, "w": 150}}, headers=XHR)
    assert r.json()["queued"] is True
    r = await client.post(f"{URL}/{a}/crop", json={"rect": None}, headers=XHR)
    assert r.json()["queued"] is True
    assert sql(clean_db, "select payload->'rect' from jobs order by id desc limit 1") == [(None,)]


async def test_crop_without_original(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, orig=False)
    r = await client.post(
        f"{URL}/{a}/crop", json={"rect": {"x": 0, "y": 0, "w": 50, "h": 50}}, headers=XHR
    )
    assert r.status_code == 409


# -- delete and original ----------------------------------------------------


async def test_delete_removes_files_and_keeps_haiku(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    s = get_settings()
    a = add(clean_db, 1, description="A cup.", vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    vote(clean_db, a, 1)
    sql(clean_db, "insert into comments (image_id, body) values (%s, 'hi')", a)
    sql(
        clean_db,
        "insert into haikus (l1, l2, l3, source, image_id) values ('x', 'y', 'z', 'model', %s)",
        a,
    )
    before = sql(clean_db, "select count(*) from corpus_stats_history")[0][0]

    r = await client.delete(f"{URL}/{a}", headers=XHR)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [x[0] for x in sql(clean_db, "select id from images")] == [b]
    assert sql(clean_db, "select count(*) from votes") == [(0,)]
    assert sql(clean_db, "select count(*) from comments") == [(0,)]
    assert sql(clean_db, "select image_id from haikus where l1 = 'x'") == [(None,)]
    assert not (s.media_orig / f"{sha(1)}.png").exists()
    assert sorted(p.name for p in s.media_pub.iterdir()) == [
        f"{sha(2)}-200.avif",
        f"{sha(2)}-200.webp",
    ]
    assert (s.media_orig / f"{sha(2)}.png").exists()
    # Live with a vector, so the corpus moved.
    assert sql(clean_db, "select count(*) from corpus_stats_history")[0][0] == before + 1
    assert sql(clean_db, "select n from corpus_stats where model = %s", MODEL) == [(1,)]

    assert (await client.delete(f"{URL}/{a}", headers=XHR)).status_code == 404


async def test_delete_keeps_shared_pub_files(client: httpx.AsyncClient, clean_db: str) -> None:
    s = get_settings()
    a = add(clean_db, 1, status="queued")
    add(clean_db, 2, pub_n=1)
    r = await client.delete(f"{URL}/{a}", headers=XHR)
    assert r.status_code == 200
    assert (s.media_pub / f"{sha(1)}-200.webp").exists()
    assert not (s.media_orig / f"{sha(1)}.png").exists()
    # Not live: the corpus did not change.
    assert sql(clean_db, "select count(*) from corpus_stats_history") == [(0,)]


async def test_original_streams_clean_file(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1)
    r = await client.get(f"{URL}/{a}/original")
    assert r.status_code == 200
    assert r.content == (get_settings().media_orig / f"{sha(1)}.png").read_bytes()
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    b = add(clean_db, 2, orig=False)
    assert (await client.get(f"{URL}/{b}/original")).status_code == 404
    assert (await client.get(f"{URL}/999/original")).status_code == 404
