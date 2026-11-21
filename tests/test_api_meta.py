import html
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pyvips

from penish.api.app import create_app
from penish.api.haiku_render import render_haiku_html
from penish.config import get_settings


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>peni.sh</title><!--og--></head>"
        "<body class='x'><div id=app></div></body></html>"
    )
    data = tmp_path / "data"
    (data / "media" / "pub").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data))
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


@pytest.mark.parametrize("path", ["/nope", "/rss.xml", "/a/b/c/d", "/tags/"])
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


# -- titles and previews -------------------------------------------------------------

BASE = datetime(2026, 3, 1, tzinfo=UTC)


def _sha(n: int) -> str:
    return f"{n:064x}"


def _pic(
    dsn: str,
    n: int,
    *,
    caption: str | None = None,
    description: str | None = None,
    live: bool = True,
    jpg: bool = True,
    webp: bool = True,
    width: int = 2000,
    height: int = 1000,
) -> int:
    """A picture with the files the page may point at: the og JPEG and a webp ladder."""
    pub = get_settings().media_pub
    if jpg:
        (pub / f"{_sha(n)}-1200.jpg").write_bytes(b"x")
    if webp:
        for w in (480, 1600):
            (pub / f"{_sha(n)}-{w}.webp").write_bytes(b"x")
    at = BASE + timedelta(minutes=n)
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "insert into images (sha256, pub_sha, slug, width, height, status, published_at)"
            " values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (
                _sha(n),
                _sha(n),
                f"p-{n}",
                width,
                height,
                "live" if live else "unpublished",
                at if live else None,
            ),
        ).fetchone()
        assert row
        for field, body in (("caption", caption), ("description", description)):
            if body:
                conn.execute(
                    "insert into image_texts (image_id, field, body, source) values (%s, %s, %s,"
                    " 'owner')",
                    (row[0], field, body),
                )
        return int(row[0])


def _head(text: str) -> str:
    return text.split("</head>")[0]


def _metas(text: str) -> dict[str, str]:
    """property -> content for every meta tag in the head, unescaped."""
    return {
        m.group(1): html.unescape(m.group(2))
        for m in re.finditer(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', _head(text))
    }


def _title(text: str) -> str:
    m = re.search(r"<title>(.*?)</title>", _head(text))
    assert m
    return html.unescape(m.group(1))


async def test_picture_page_title_and_og_jpeg(client: httpx.AsyncClient, clean_db: str) -> None:
    _pic(clean_db, 1, caption="a lamp", description="Subject: a lamp.\nIt hums.")
    r = await client.get("/i/p-1")
    assert _title(r.text) == "a lamp -- peni.sh"
    assert r.text.count("<title>") == 1
    og = _metas(r.text)
    assert og["og:title"] == "a lamp"
    assert og["og:description"] == "a lamp. It hums."
    assert og["og:image"] == f"http://t/media/pub/{_sha(1)}-1200.jpg"
    assert (og["og:image:width"], og["og:image:height"]) == ("1200", "600")
    assert og["og:image:type"] == "image/jpeg"


async def test_picture_falls_back_to_the_webp_then_to_nothing(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _pic(clean_db, 1, caption="one", jpg=False)
    og = _metas((await client.get("/i/p-1")).text)
    assert og["og:image"] == f"http://t/media/pub/{_sha(1)}-1600.webp"
    assert og["og:image:width"] == "1600" and og["og:image:height"] == "800"
    assert og["og:image:type"] == "image/webp"

    _pic(clean_db, 2, caption="two", jpg=False, webp=False)
    og = _metas((await client.get("/i/p-2")).text)
    assert not [k for k in og if k.startswith("og:image")]
    assert "og:description" not in og


async def test_title_and_tags_are_escaped(client: httpx.AsyncClient, clean_db: str) -> None:
    caption = "a <b>\"lamp\"</b> & 'co'"
    _pic(clean_db, 1, caption=caption, description="x <script>alert(1)</script> & y")
    r = await client.get("/i/p-1")
    head = _head(r.text)
    assert "<b>" not in head and "<script>alert" not in head
    assert "&lt;b&gt;" in head and "&amp;" in head
    assert _title(r.text) == f"{caption} -- peni.sh"
    og = _metas(r.text)
    assert og["og:title"] == caption
    assert og["og:description"] == "x <script>alert(1)</script> & y"


def _publish(dsn: str, on: bool = True) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("update collections set published = %s", (on,))


def _leaf(dsn: str, image_id: int, book: int, position: int, note: str | None = None) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "insert into collection_leaves (image_id, collection_id, book, position, note,"
            " note_source) select %s, id, %s, %s, %s, case when %s::text is null then null"
            " else 'owner' end from collections where slug = 'pansophicon'",
            (image_id, book, position, note, note),
        )


async def test_leaf_page_prefers_the_note(client: httpx.AsyncClient, clean_db: str) -> None:
    _publish(clean_db)
    noted = _pic(clean_db, 1, caption="a leaf", description="the description")
    plain = _pic(clean_db, 2, caption="another", description="the other description")
    _leaf(clean_db, noted, 3, 14, note="Foxed at the gutter.")
    _leaf(clean_db, plain, 3, 15)
    assert _metas((await client.get("/i/p-1")).text)["og:description"] == "Foxed at the gutter."
    assert _metas((await client.get("/i/p-2")).text)["og:description"] == "the other description"
    assert _title((await client.get("/i/p-1")).text) == "a leaf -- peni.sh"

    # An unpublished collection has no leaves in public: the description stands.
    _publish(clean_db, False)
    assert _metas((await client.get("/i/p-1")).text)["og:description"] == "the description"


def _deck(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "select l1, l2, l3 from haikus where active and colony_id is null"
        ).fetchall()
    return {" / ".join(r) for r in rows}


async def test_home_has_the_haiku_of_the_day_and_the_newest_picture(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _pic(clean_db, 1, caption="old")
    _pic(clean_db, 3, caption="newest")
    _pic(clean_db, 2, caption="middle")
    _pic(clean_db, 9, caption="hidden", live=False)
    first = _metas((await client.get("/")).text)
    assert first["og:description"] in _deck(clean_db)
    assert first["og:image"] == f"http://t/media/pub/{_sha(3)}-1200.jpg"
    assert first["og:image:type"] == "image/jpeg"
    assert first["og:title"] == "peni.sh"
    # The page's haiku changes on every load; the preview's does not.
    for _ in range(5):
        assert _metas((await client.get("/")).text)["og:description"] == first["og:description"]


async def test_home_description_can_be_nothing(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from penish.api import index

    monkeypatch.setattr(index, "home_description", lambda haiku: None)
    og = _metas((await client.get("/")).text)
    assert "og:description" not in og


async def test_home_without_pictures_has_no_image(client: httpx.AsyncClient) -> None:
    og = _metas((await client.get("/")).text)
    assert not [k for k in og if k.startswith("og:image")]
    assert _title((await client.get("/")).text) == "peni.sh"


async def test_unpublished_collection_shows_nothing(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _leaf(clean_db, _pic(clean_db, 1, caption="x"), 1, 1)
    for path in ("/collections/pansophicon", "/collections/pansophicon/1"):
        r = await client.get(path)
        assert r.status_code == 404
        assert _title(r.text) == "peni.sh"
        assert list(_metas(r.text)) == ["og:title"]


async def test_collection_page_gloss_title_and_first_leaf(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _publish(clean_db)
    with psycopg.connect(clean_db) as conn:
        conn.execute("update collections set gloss = %s", ("An all-wise thing.\nOf all things.",))
    # Lowest book, then lowest position, and only a live leaf.
    _leaf(clean_db, _pic(clean_db, 1, caption="later"), 2, 1)
    _leaf(clean_db, _pic(clean_db, 2, caption="second"), 1, 9)
    _leaf(clean_db, _pic(clean_db, 3, caption="first"), 1, 4)
    _leaf(clean_db, _pic(clean_db, 4, caption="down", live=False), 1, 1)
    r = await client.get("/collections/pansophicon")
    assert r.status_code == 200
    og = _metas(r.text)
    assert _title(r.text).endswith(" -- peni.sh") and "peni.sh" != _title(r.text)
    assert og["og:title"] == _title(r.text).removesuffix(" -- peni.sh")
    assert og["og:description"] == "An all-wise thing. Of all things."
    assert og["og:image"] == f"http://t/media/pub/{_sha(3)}-1200.jpg"
    assert (og["og:image:width"], og["og:image:height"]) == ("1200", "600")


async def test_collection_page_prefers_the_owners_png(
    client: httpx.AsyncClient, clean_db: str, tmp_path: Path
) -> None:
    _publish(clean_db)
    _leaf(clean_db, _pic(clean_db, 1, caption="a"), 1, 1)
    (tmp_path / "og").mkdir()
    pyvips.Image.black(120, 63, bands=3).write_to_file(str(tmp_path / "og" / "pansophicon.png"))
    og = _metas((await client.get("/collections/pansophicon")).text)
    assert og["og:image"] == "http://t/og/pansophicon.png"
    assert (og["og:image:width"], og["og:image:height"]) == ("120", "63")
    assert og["og:image:type"] == "image/png"
    assert (await client.get("/og/pansophicon.png")).status_code == 200


async def test_collection_page_without_any_image(client: httpx.AsyncClient, clean_db: str) -> None:
    _publish(clean_db)
    og = _metas((await client.get("/collections/pansophicon")).text)
    assert not [k for k in og if k.startswith("og:image")]
    assert "twitter:card" not in og


async def test_book_title(client: httpx.AsyncClient, clean_db: str) -> None:
    _publish(clean_db)
    r = await client.get("/collections/pansophicon/3")
    assert re.fullmatch(r".+ iii -- peni\.sh", _title(r.text))
    assert (await client.get("/collections/pansophicon/8")).status_code == 404


async def test_list_pages_are_titled_as_the_spa_titles_them(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _insert_image(clean_db, "live-one", tags=("wet moss",))
    from penish.categories import SLUGS

    cases = {
        "/categories": "categories -- peni.sh",
        "/tags": "tags -- peni.sh",
        "/galleries": "galleries -- peni.sh",
        "/mycelium": "map -- peni.sh",
        "/collections": "collections -- peni.sh",
        "/search": "search -- peni.sh",
        f"/c/{SLUGS[0]}": f"{SLUGS[0].replace('-', ' ')} -- peni.sh",
        "/t/wet%20moss": "wet moss -- peni.sh",
        "/admin": "peni.sh",
    }
    for path, title in cases.items():
        assert _title((await client.get(path)).text) == title, path


async def test_every_page_links_the_feed(client: httpx.AsyncClient) -> None:
    for path in ("/", "/nope", "/i/x"):
        assert '<link rel="alternate" type="application/atom+xml" href="/feed.xml">' in _head(
            (await client.get(path)).text
        )


def test_title_is_added_when_the_shell_has_none() -> None:
    from penish.api.index import _set_title

    assert _set_title("<head></head>", "a & b") == "<head><title>a &amp; b</title></head>"
    assert _set_title("<head><title>x</title></head>", "y") == "<head><title>y</title></head>"


def test_robots_names_the_sitemap() -> None:
    robots = Path(__file__).resolve().parent.parent / "web" / "static" / "robots.txt"
    assert "Sitemap: https://peni.sh/sitemap.xml" in robots.read_text().splitlines()


def _own_haiku(dsn: str, image_id: int, line: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("update haikus set active = false")
        conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id) values (%s, 'b', 'c', 'model', %s)",
            (line, image_id),
        )


async def test_a_taken_down_pictures_haiku_is_not_shown(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    down = _pic(clean_db, 1, caption="gone")
    live = _pic(clean_db, 2, caption="here")
    _own_haiku(clean_db, down, "from the gone one")
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "update images set status = 'unpublished', published_at = null where id = %s", (down,)
        )
        conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id) values ('from the live one',"
            " 'b', 'c', 'model', %s)",
            (live,),
        )
    for _ in range(8):
        assert (await client.get("/api/haiku")).json()["lines"][0] == "from the live one"
    assert _metas((await client.get("/")).text)["og:description"] == "from the live one / b / c"

    # Back up, and it is in the deck again; a haiku of no picture was never out of it.
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "update images set status = 'live', published_at = now() where id = %s", (down,)
        )
        conn.execute("update haikus set image_id = null where l1 = 'from the gone one'")
    seen = {(await client.get("/api/haiku")).json()["lines"][0] for _ in range(30)}
    assert "from the gone one" in seen


async def test_collection_page_holds_one_connection_at_a_time(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rendering the shell takes a connection of its own; the page must have given its back.
    _publish(clean_db)
    held = 0
    peak = 0
    pool = client._transport.app.state.pool  # type: ignore[attr-defined]
    real = pool.connection

    def counting(*a: Any, **kw: Any) -> Any:
        cm = real(*a, **kw)

        class Wrap:
            async def __aenter__(self) -> Any:
                nonlocal held, peak
                held += 1
                peak = max(peak, held)
                return await cm.__aenter__()

            async def __aexit__(self, *exc: Any) -> Any:
                nonlocal held
                held -= 1
                return await cm.__aexit__(*exc)

        return Wrap()

    monkeypatch.setattr(pool, "connection", counting)
    r = await client.get("/collections/pansophicon")
    assert r.status_code == 200
    assert peak == 1
