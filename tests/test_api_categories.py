# ruff: noqa: F811  -- the shared `client` fixture is imported, then named as a parameter.
import httpx
import psycopg

from penish.api import routes_images
from penish.categories import SLUGS
from tests.test_api_images import add_image, client  # noqa: F401


def file(dsn: str, n: int, *cats: str, source: str = "model") -> None:
    with psycopg.connect(dsn) as conn:
        for c in cats:
            conn.execute(
                "insert into image_categories (image_id, category, source) "
                "select id, %s, %s from images where slug = %s",
                (c, source, f"img-{n}"),
            )
        conn.commit()


async def test_every_category_listed_in_order_with_count_and_cover(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    for n in range(1, 5):
        add_image(clean_db, n)
    add_image(clean_db, 5, status="queued")
    file(clean_db, 1, "cartoon")
    file(clean_db, 2, "cartoon", "erotica")
    file(clean_db, 3, "photojournalism")
    file(clean_db, 5, "cartoon")
    body = (await client.get("/api/categories")).json()["categories"]
    assert [c["slug"] for c in body] == list(SLUGS)
    by = {c["slug"]: c for c in body}
    assert by["cartoon"]["n"] == 2 and by["cartoon"]["label"] == "cartoon"
    assert by["cartoon"]["cover"]["slug"] == "img-2"
    assert by["erotica"]["n"] == 1
    assert by["fine-art"] == {"slug": "fine-art", "label": "fine art", "n": 0, "cover": None}


async def test_list_by_category_newest_and_sorted(client: httpx.AsyncClient, clean_db: str) -> None:
    routes_images._orders._items.clear()
    for n in range(1, 5):
        add_image(clean_db, n)
    file(clean_db, 1, "drawing")
    file(clean_db, 3, "drawing")
    r = (await client.get("/api/images", params={"category": "drawing"})).json()
    assert [i["slug"] for i in r["items"]] == ["img-3", "img-1"] and r["count"] == 2
    r = (await client.get("/api/images", params={"category": "drawing", "sort": "oldest"})).json()
    assert [i["slug"] for i in r["items"]] == ["img-1", "img-3"] and r["count"] == 2
    # Refiling moves a cached sorted listing too.
    file(clean_db, 2, "drawing")
    r = (await client.get("/api/images", params={"category": "drawing", "sort": "oldest"})).json()
    assert [i["slug"] for i in r["items"]] == ["img-1", "img-2", "img-3"]
    r = (await client.get("/api/images", params={"category": "drawing", "limit": 1})).json()
    nxt = (
        await client.get(
            "/api/images", params={"category": "drawing", "limit": 1, "cursor": r["next_cursor"]}
        )
    ).json()
    assert [i["slug"] for i in nxt["items"]] == ["img-2"] and "count" not in nxt
    assert (await client.get("/api/images", params={"category": "pottery"})).status_code == 404


async def test_picture_carries_its_categories(client: httpx.AsyncClient, clean_db: str) -> None:
    add_image(clean_db, 1)
    file(clean_db, 1, "erotica", "fine-art")
    body = (await client.get("/api/images/img-1")).json()
    assert body["categories"] == [
        {"slug": "fine-art", "label": "fine art"},
        {"slug": "erotica", "label": "erotica"},
    ]
