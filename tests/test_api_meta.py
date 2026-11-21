import html
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest

from penish.api.app import create_app
from penish.api.haiku_render import render_haiku_html
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
    assert "<body class='x'><h1 class=\"haiku\" " in r.text


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


async def test_head_on_pages_is_allowed(client: httpx.AsyncClient) -> None:
    r = await client.head("/")
    assert r.status_code == 200


def _growths(text: str) -> list[float]:
    return [float(g) for g in re.findall(r"'grow' ([\d.]+)", text)]


async def test_index_varies_without_js(client: httpx.AsyncClient, clean_db: str) -> None:
    a = (await client.get("/")).text
    b = (await client.get("/")).text
    ga, gb = _growths(a), _growths(b)
    assert len(ga) > 20
    assert len(set(ga)) > 10
    assert ga != gb
    m = re.search(r'<h1 class="haiku" aria-label="(.*?)">', a)
    assert m
    # aria-label is the plain haiku: every part is a line from the deck.
    assert set(html.unescape(m.group(1)).split(" / ")) <= _seed_lines(clean_db)
    assert "haiku-ssr" not in a


async def test_gen_param_and_window_seed(client: httpx.AsyncClient) -> None:
    r = await client.get("/?gen=ou")
    m = re.search(r"window\.__HAIKU__=(.*?)</script>", r.text)
    assert m
    data = json.loads(m.group(1))
    assert data["gen"] == "ou"
    again = render_haiku_html(data["lines"], seed=data["seed"], gen="ou")
    assert again in r.text


async def test_image_page_has_no_server_haiku(client: httpx.AsyncClient) -> None:
    r = await client.get("/i/whatever")
    assert '<h1 class="haiku"' not in r.text


def _insert_image(dsn: str, slug: str, *, live: bool = True, tags: tuple[str, ...] = ()) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "insert into images (sha256, slug, status, published_at)"
            " values (%s, %s, %s, case when %s then now() end) returning id",
            (slug.ljust(64, "0"), slug, "live" if live else "processing", live),
        ).fetchone()
        assert row
        for t in tags:
            conn.execute(
                "insert into tags (image_id, tag, source) values (%s, %s, 'model')", (row[0], t)
            )
        return int(row[0])


def _static_routes() -> list[str]:
    root = Path(__file__).resolve().parent.parent / "web" / "src" / "routes"
    out = []
    for f in root.rglob("+page.svelte"):
        parts = f.parent.relative_to(root).parts
        if not any(p.startswith("[") for p in parts):
            out.append("/" + "/".join(parts))
    return sorted(out)


@pytest.mark.parametrize("path", ["/nope", "/sitemap.xml", "/feed.xml", "/a/b/c/d", "/tags/"])
async def test_unknown_path_is_404_with_shell(client: httpx.AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 404
    assert "window.__HAIKU__" in r.text
    assert r.headers["cache-control"] == "no-store"


async def test_head_unknown_path_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.head("/nope")).status_code == 404


async def test_every_static_route_in_the_tree_answers_200(client: httpx.AsyncClient) -> None:
    paths = _static_routes()
    assert "/" in paths and "/admin" in paths  # the walk found the tree
    for p in paths:
        assert (await client.get(p)).status_code == 200, p


async def test_admin_pages_are_200(client: httpx.AsyncClient) -> None:
    for p in ("/admin", "/admin/comments", "/admin/stats", "/admin/pictures", "/admin/pictures/12"):
        assert (await client.get(p)).status_code == 200, p
    assert (await client.get("/admin/pictures/abc")).status_code == 404
    assert (await client.get("/admin/nope")).status_code == 404


async def test_category_pages(client: httpx.AsyncClient) -> None:
    from penish.categories import SLUGS

    assert (await client.get(f"/c/{SLUGS[0]}")).status_code == 200
    assert (await client.get("/c/nope")).status_code == 404


async def test_tag_pages(client: httpx.AsyncClient, clean_db: str) -> None:
    _insert_image(clean_db, "live-one", tags=("moss", "wet moss"))
    _insert_image(clean_db, "hidden-one", live=False, tags=("unseen",))
    assert (await client.get("/t/moss")).status_code == 200
    assert (await client.get("/t/wet moss")).status_code == 200
    assert (await client.get("/t/wet%20moss")).status_code == 200
    assert (await client.get("/t/nope")).status_code == 404
    assert (await client.get("/t/unseen")).status_code == 404
    assert (await client.get("/t/")).status_code == 404


async def test_old_slug_redirects(client: httpx.AsyncClient, clean_db: str) -> None:
    img = _insert_image(clean_db, "new-name")
    with psycopg.connect(clean_db) as conn:
        conn.execute("insert into slug_aliases (slug, image_id) values ('old-name', %s)", (img,))
    for method in (client.get, client.head):
        r = await method("/i/old-name")
        assert r.status_code == 301
        assert r.headers["location"] == "/i/new-name"
    r = await client.get("/i/old-name?gen=ou")
    assert r.status_code == 301
    assert r.headers["location"] == "/i/new-name?gen=ou"
    assert (await client.get("/i/new-name")).status_code == 200
    assert (await client.get("/i/neither")).status_code == 404
