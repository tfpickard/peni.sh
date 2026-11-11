import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest

from penish.api.app import create_app
from penish.config import get_settings


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><!--og--></head>"
        "<body class='x'><div id=app></div></body></html>"
    )
    imm = tmp_path / "_app" / "immutable"
    imm.mkdir(parents=True)
    (imm / "a.js").write_text("1")
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(tmp_path))
    get_settings.cache_clear()
    app = create_app()
    # ASGITransport does not run lifespan, so drive it explicitly.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    get_settings.cache_clear()


def _seed_lines(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("select l1, l2, l3 from haikus").fetchall()
    return {line for r in rows for line in r}


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_haiku(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/haiku")
    assert r.status_code == 200
    assert len(r.json()["lines"]) == 3


async def test_stats_empty(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/stats")
    assert r.json() == {"pictures": 0, "temperature": None}


async def test_index_injects_haiku(client: httpx.AsyncClient, clean_db: str) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert 'property="og:title"' in r.text
    assert "<!--og-->" not in r.text
    m = re.search(r"window\.__HAIKU__=(.*?)</script>", r.text)
    assert m
    lines = json.loads(m.group(1))["lines"]
    assert set(lines) <= _seed_lines(clean_db)
    assert "<body class='x'><h1 class=\"haiku-ssr\">" in r.text


async def test_spa_fallback(client: httpx.AsyncClient) -> None:
    r = await client.get("/i/whatever")
    assert r.status_code == 404  # unknown slug: SPA renders "not here"
    assert "window.__HAIKU__" in r.text


async def test_api_unknown_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/nope")
    assert r.status_code == 404
    assert "__HAIKU__" not in r.text


async def test_immutable_asset_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/_app/immutable/a.js")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]
