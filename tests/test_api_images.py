import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from penish.api.app import create_app
from penish.config import get_settings

DIM = 1024
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def sha(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><html><head><!--og--></head><body><div id=app></div></body></html>"
    )
    data = tmp_path / "data"
    (data / "media" / "pub").mkdir(parents=True)
    (data / "media" / "orig").mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(web))
    monkeypatch.setenv("DATA_DIR", str(data))
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    get_settings.cache_clear()


def add_image(
    dsn: str,
    n: int,
    *,
    status: str = "live",
    caption: str | None = None,
    description: str | None = None,
    analysis: str | None = None,
    widths: tuple[int, ...] = (480, 960, 1600),
    vec: list[float] | None = None,
    pub_n: int | None = None,
) -> int:
    """pub_n names the derivatives (pub_sha) separately from the upload hash, as after a crop."""
    pub_sha = sha(n if pub_n is None else pub_n)
    pub = get_settings().media_pub
    for w in widths:
        for ext in ("avif", "webp"):
            (pub / f"{pub_sha}-{w}.{ext}").write_bytes(b"x")
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "insert into images (sha256, pub_sha, slug, width, height, blurhash, palette, status, "
            "published_at) values (%s, %s, %s, 3200, 2400, 'LEHV6n', '[\"#fff\"]', %s, %s) "
            "returning id",
            (sha(n), pub_sha, f"img-{n}", status, BASE + timedelta(minutes=n)),
        ).fetchone()
        assert row
        image_id = row[0]
        for field, body in (
            ("caption", caption),
            ("description", description),
            ("analysis", analysis),
        ):
            if body:
                conn.execute(
                    "insert into image_texts (image_id, field, body, source, model) "
                    "values (%s, %s, %s, 'model', 'm')",
                    (image_id, field, body),
                )
        if vec is not None:
            conn.execute(
                "insert into embeddings (image_id, kind, vec, model, dim) "
                "values (%s, 'description', %s::vector, %s, %s)",
                (image_id, str(vec), get_settings().embed_model, DIM),
            )
        conn.commit()
    return image_id


def unit(i: int, j: int | None = None) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    if j is not None:
        v[j] = 0.5
    return v


async def test_list_pagination(client: httpx.AsyncClient, clean_db: str) -> None:
    for n in range(1, 6):
        add_image(clean_db, n)
    add_image(clean_db, 6, status="queued")
    r = await client.get("/api/images", params={"limit": 2})
    body = r.json()
    assert [i["slug"] for i in body["items"]] == ["img-5", "img-4"]
    item = body["items"][0]
    assert item["blurhash"] == "LEHV6n"
    assert [v["w"] for v in item["variants"]] == [480, 960, 1600]
    assert item["variants"][0]["webp"] == f"/media/pub/{sha(5)}-480.webp"
    assert item["variants"][0]["h"] == 360

    seen = [i["slug"] for i in body["items"]]
    cursor = body["next_cursor"]
    while cursor:
        page = (await client.get("/api/images", params={"limit": 2, "cursor": cursor})).json()
        seen += [i["slug"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert seen == ["img-5", "img-4", "img-3", "img-2", "img-1"]


async def test_list_bad_cursor(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/images", params={"cursor": "nope"})).status_code == 400


async def test_detail_with_neighbors(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(
        clean_db,
        1,
        caption="a cup",
        description="The table has a scratch. A cup is on it.",
        analysis="Subject: a hidden cup analysis.",
        vec=unit(0),
    )
    add_image(clean_db, 2, vec=unit(0, 1))
    add_image(clean_db, 3, vec=unit(5))
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "insert into tags (image_id, tag, source) "
            "select id, 'cup', 'model' from images where slug = 'img-1'"
        )
        conn.commit()
    r = await client.get("/api/images/img-1")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "img-1"
    assert body["caption"] == {"body": "a cup", "source": "model", "model": "m"}
    assert body["description"]["body"] == "The table has a scratch. A cup is on it."
    assert body["prompt"] is None
    # The analysis is what the vector is made from; it never leaves the server.
    assert "analysis" not in body and "hidden cup analysis" not in r.text
    assert body["tags"] == [{"tag": "cup", "source": "model"}]
    assert body["mode"] == "raw"
    assert [n["slug"] for n in body["similar"]] == ["img-2", "img-3"]
    assert body["dissimilar"][0]["slug"] == "img-3"
    assert body["similar"][0]["score"] == round(body["similar"][0]["score"], 3)
    assert body["similar"][0]["src"] == f"/media/pub/{sha(2)}-480.webp"
    assert body["similar"][0]["blurhash"] == "LEHV6n"
    assert set(body["metrics"]) == {"loneliness", "typicality", "surprise"}
    assert body["published_at"]

    r = await client.get("/api/images/img-1", params={"sim": "centered"})
    assert r.status_code == 200
    assert (await client.get("/api/images/img-1", params={"sim": "bogus"})).status_code == 422


async def test_detail_prev_next_follow_grid_and_wrap(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    for n in range(1, 4):
        add_image(clean_db, n)
    add_image(clean_db, 4, status="queued")

    async def adj(slug: str) -> tuple[str | None, str | None]:
        body = (await client.get(f"/api/images/{slug}")).json()
        return body["prev"], body["next"]

    # Grid order is img-3, img-2, img-1; the unpublished img-4 never appears.
    assert await adj("img-3") == ("img-1", "img-2")
    assert await adj("img-2") == ("img-3", "img-1")
    assert await adj("img-1") == ("img-2", "img-3")


async def test_detail_lone_picture_has_no_prev_next(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    add_image(clean_db, 1)
    body = (await client.get("/api/images/img-1")).json()
    assert body["prev"] is None and body["next"] is None


async def test_alias_redirect(client: httpx.AsyncClient, clean_db: str) -> None:
    image_id = add_image(clean_db, 1)
    with psycopg.connect(clean_db) as conn:
        conn.execute("insert into slug_aliases values ('old-name', %s)", (image_id,))
        conn.commit()
    r = await client.get("/api/images/old-name")
    assert r.json() == {"redirect": "img-1"}


async def test_not_live_is_404(client: httpx.AsyncClient, clean_db: str) -> None:
    image_id = add_image(clean_db, 1, status="pending_enrichment")
    with psycopg.connect(clean_db) as conn:
        conn.execute("insert into slug_aliases values ('old-name', %s)", (image_id,))
        conn.commit()
    for slug in ("img-1", "old-name", "missing"):
        r = await client.get(f"/api/images/{slug}")
        assert r.status_code == 404
        assert r.json() == {"detail": "not here"}


async def test_media(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    add_image(clean_db, 255)
    r = await client.get(f"/media/pub/{sha(1)}-480.webp")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-type"] == "image/webp"
    r = await client.get(f"/media/pub/{sha(1)}-960.avif")
    assert r.headers["content-type"] == "image/avif"

    orig = get_settings().media_orig / f"{sha(2)}.jpg"
    orig.write_bytes(b"secret")
    bad = [
        f"/media/pub/{sha(1)}-480.png",
        f"/media/pub/{sha(9)}-480.webp",
        "/media/pub/..%2Forig%2F" + orig.name,
        "/media/pub/../orig/" + orig.name,
        "/media/orig/" + orig.name,
        f"/media/pub/{sha(255).upper()}-480.webp",
    ]
    for url in bad:
        r = await client.get(url)
        assert r.status_code == 404, url
        assert b"secret" not in r.content


async def test_single_native_width(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1, widths=(300,))
    body = (await client.get("/api/images/img-1")).json()
    assert [v["w"] for v in body["variants"]] == [300]


async def test_og_page(client: httpx.AsyncClient, clean_db: str) -> None:
    words = " ".join(f"w{i}" for i in range(80))
    add_image(
        clean_db,
        1,
        caption='a "cup" <b>&',
        description=f"The table edge is chipped.\n\n{words}",
        analysis="Subject: a cup on a table.\n\nSetting: a kitchen.\n\nMood: calm.",
    )
    r = await client.get("/i/img-1")
    assert r.status_code == 200
    assert f'<meta property="og:image" content="http://t/media/pub/{sha(1)}-1600.webp">' in r.text
    assert 'content="a &quot;cup&quot; &lt;b&gt;&amp;"' in r.text
    og = re.search(r'<meta property="og:description" content="([^"]*)">', r.text)
    assert og is not None
    # The opening of the public description, cut at a word, never the analysis.
    assert og.group(1).startswith("The table edge is chipped. w0 w1")
    assert len(og.group(1)) <= 200 and not og.group(1).endswith(" ")
    assert (og.group(1) + " ") in f"The table edge is chipped. {words} "
    assert "Subject" not in r.text and "kitchen" not in r.text
    assert 'property="og:image:width" content="1600"' in r.text
    assert 'property="og:image:height" content="1200"' in r.text
    assert 'property="og:type" content="article"' in r.text
    assert 'name="twitter:card" content="summary_large_image"' in r.text
    assert r.text.count('property="og:title"') == 1
    assert "<b>" not in r.text


async def test_og_old_labelled_description(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(
        clean_db, 1, description="Subject: a cup on a table.\n\nSetting: a kitchen.\n\nMood: calm."
    )
    r = await client.get("/i/img-1")
    assert '<meta property="og:description" content="a cup on a table. a kitchen. calm.">' in r.text


async def test_og_unknown_slug_is_404_spa(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1, status="queued")
    for slug in ("img-1", "nothing"):
        r = await client.get(f"/i/{slug}")
        assert r.status_code == 404
        assert "<div id=app>" in r.text
        assert "og:image" not in r.text


async def test_variants_follow_pub_sha(client: httpx.AsyncClient, clean_db: str) -> None:
    # A cropped image keeps its upload hash but serves files named by pub_sha.
    add_image(clean_db, 1, pub_n=901, description="Subject: a cup.", vec=unit(0))
    add_image(clean_db, 2, pub_n=902, vec=unit(0, 1))
    listed = (await client.get("/api/images")).json()["items"]
    assert listed[0]["variants"][0]["webp"] == f"/media/pub/{sha(902)}-480.webp"
    body = (await client.get("/api/images/img-1")).json()
    assert body["variants"][-1]["webp"] == f"/media/pub/{sha(901)}-1600.webp"
    assert body["similar"][0]["src"] == f"/media/pub/{sha(902)}-480.webp"
    page = (await client.get("/i/img-1")).text
    assert f"/media/pub/{sha(901)}-1600.webp" in page
    assert sha(1) not in page


async def test_pub_sha_defaults_to_sha256(clean_db: str) -> None:
    # Raw inserts without pub_sha (tests, psql) must still name files that exist.
    with psycopg.connect(clean_db) as conn:
        row = conn.execute(
            "insert into images (sha256, status) values (%s, 'queued') returning pub_sha",
            (sha(77),),
        ).fetchone()
    assert row == (sha(77),)
