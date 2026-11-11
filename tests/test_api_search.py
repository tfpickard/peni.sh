from typing import Any

import httpx
import psycopg
import pytest

from penish.api import embed_client, routes_search
from penish.config import get_settings
from tests.test_api_images import add_image, client, unit  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh() -> None:
    routes_search._vectors.clear()
    routes_search.search_limit.clear()


def tag(dsn: str, n: int, *tags: str) -> None:
    with psycopg.connect(dsn) as conn:
        for t in tags:
            conn.execute(
                "insert into tags (image_id, tag, source) "
                "select id, %s, 'model' from images where slug = %s",
                (t, f"img-{n}"),
            )
        conn.commit()


def fake_embed(monkeypatch: pytest.MonkeyPatch, vec: list[float]) -> list[str]:
    asked: list[str] = []

    async def fake(text: str, **kw: Any) -> dict[str, Any]:
        asked.append(text)
        return {"vector": vec, "model": get_settings().embed_model, "dim": len(vec)}

    monkeypatch.setattr(embed_client, "embed_query_full", fake)
    return asked


async def test_cloud_counts_live_caps_and_floors(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for n in range(1, 5):
        add_image(clean_db, n)
    add_image(clean_db, 5, status="queued")
    tag(clean_db, 1, "chair", "moss", "once")
    tag(clean_db, 2, "chair", "moss", "eel")
    tag(clean_db, 3, "chair", "eel")
    tag(clean_db, 5, "chair", "once")
    r = await client.get("/api/tags")
    # "once" is on one live picture and one queued one: below the floor of two.
    assert r.json() == {
        "tags": [{"tag": "chair", "n": 3}, {"tag": "eel", "n": 2}, {"tag": "moss", "n": 2}]
    }

    monkeypatch.setenv("TAG_CLOUD_LIMIT", "2")
    monkeypatch.setenv("TAG_CLOUD_MIN", "1")
    get_settings.cache_clear()
    r = await client.get("/api/tags")
    assert [t["tag"] for t in r.json()["tags"]] == ["chair", "eel"]


async def test_cloud_empty(client: httpx.AsyncClient) -> None:  # noqa: F811
    assert (await client.get("/api/tags")).json() == {"tags": []}


async def test_list_by_tag(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    for n in range(1, 5):
        add_image(clean_db, n)
    add_image(clean_db, 5, status="queued")
    tag(clean_db, 1, "chair")
    tag(clean_db, 3, "chair")
    tag(clean_db, 4, "chair", "eel")
    tag(clean_db, 5, "chair")
    r = await client.get("/api/images", params={"tag": "chair", "limit": 2})
    body = r.json()
    assert [i["slug"] for i in body["items"]] == ["img-4", "img-3"]
    assert body["count"] == 3
    page = (
        await client.get(
            "/api/images", params={"tag": "chair", "limit": 2, "cursor": body["next_cursor"]}
        )
    ).json()
    assert [i["slug"] for i in page["items"]] == ["img-1"]
    assert page["next_cursor"] is None and "count" not in page
    none = (await client.get("/api/images", params={"tag": "nope"})).json()
    assert none == {"items": [], "next_cursor": None, "count": 0}
    assert "count" not in (await client.get("/api/images")).json()


async def test_search_tagged_first_then_nearest(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(2))
    add_image(clean_db, 3, vec=unit(2, 3))
    add_image(clean_db, 4, vec=unit(2), status="queued")
    add_image(clean_db, 5)
    tag(clean_db, 1, "blue-chair")
    tag(clean_db, 3, "blue-chair-leg")
    tag(clean_db, 5, "blue-chair")
    asked = fake_embed(monkeypatch, unit(2))
    r = await client.get("/api/search", params={"q": "  Blue   chair "})
    assert r.status_code == 200
    body = r.json()
    assert asked == ["Blue chair"]
    assert body["q"] == "Blue chair"
    # Exact tag carriers lead (the one with a vector ahead of the one without), then
    # everything else by distance. img-5 has no vector and is only here for its tag.
    assert [i["slug"] for i in body["items"]] == ["img-1", "img-5", "img-2", "img-3"]
    assert body["tags"] == [{"tag": "blue-chair", "n": 2}, {"tag": "blue-chair-leg", "n": 1}]
    assert set(body["items"][0]) == {"slug", "width", "height", "blurhash", "palette", "variants"}

    # The same words again are served from the cache.
    await client.get("/api/search", params={"q": "blue chair", "limit": 1})
    assert asked == ["Blue chair"]


async def test_search_finds_free_text_owner_tags(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(1))
    # A sidecar keeps spaces, so slugifying the query alone would never reach this tag.
    tag(clean_db, 2, "black and white")
    fake_embed(monkeypatch, unit(0))
    body = (await client.get("/api/search", params={"q": "Black and white"})).json()
    assert body["tags"] == [{"tag": "black and white", "n": 1}]
    assert [i["slug"] for i in body["items"]] == ["img-2", "img-1"]


async def test_list_by_long_owner_tag(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    long = "a-tag-from-a-sidecar-that-runs-well-past-the-admin-form-limit-of-sixty"
    add_image(clean_db, 1)
    tag(clean_db, 1, long)
    r = await client.get("/api/images", params={"tag": long})
    assert r.status_code == 200
    assert r.json()["count"] == 1


async def test_search_blank(client: httpx.AsyncClient) -> None:  # noqa: F811
    assert (await client.get("/api/search", params={"q": "  "})).json() == {
        "q": "",
        "tags": [],
        "items": [],
    }
    assert (await client.get("/api/search", params={"q": "x" * 201})).status_code == 422


async def test_search_without_worker_falls_back_to_tags(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(1))
    tag(clean_db, 2, "eel")
    # Nothing listens on the discard port, so the connect is refused immediately.
    monkeypatch.setenv("EMBED_SERVER_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    body = (await client.get("/api/search", params={"q": "eel"})).json()
    assert [i["slug"] for i in body["items"]] == ["img-2"]
    r = await client.get("/api/search", params={"q": "a chair"})
    assert r.status_code == 503
    assert r.json() == {"detail": "search unavailable"}


async def test_search_rate_limited(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_image(clean_db, 1, vec=unit(0))
    fake_embed(monkeypatch, unit(0))
    monkeypatch.setattr(routes_search.search_limit, "limit", 2)
    for q in ("a", "b"):
        assert (await client.get("/api/search", params={"q": q})).status_code == 200
    assert (await client.get("/api/search", params={"q": "c"})).status_code == 429
    # Cached words cost the worker nothing, so they are never refused.
    assert (await client.get("/api/search", params={"q": "a"})).status_code == 200
