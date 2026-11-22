"""Where the gallery, the mycelium and the collection meet: leaf lines on strips and
gallery covers, the home line, search by the collection's names and leaf references, and
the collection page's haiku. Nothing of an unpublished collection shows anywhere."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from penish.api import embed_client, routes_search
from penish.config import get_settings
from tests.test_admin_pictures import sql
from tests.test_api_images import add_image, client, unit  # noqa: F401
from tests.test_collections_api import SLUG, leaf, moved, publish
from tests.test_galleries_api import colony, public

# loose_title names book 0 (incertae sedis) where a line would put a numeral.
LINE = {"slug": SLUG, "abbrev": "ΠΤΠ", "abbrev_latin": "PTP", "loose_title": "incertae sedis"}


@pytest.fixture(autouse=True)
def _fresh_search() -> None:
    routes_search._vectors.clear()
    routes_search.search_limit.clear()


def fake_embed(monkeypatch: pytest.MonkeyPatch, vec: list[float]) -> list[str]:
    asked: list[str] = []

    async def fake(text: str, **kw: Any) -> dict[str, Any]:
        asked.append(text)
        return {"vector": vec, "model": get_settings().embed_model, "dim": len(vec)}

    monkeypatch.setattr(embed_client, "embed_query_full", fake)
    return asked


def take_down(dsn: str, image_id: int) -> None:
    sql(dsn, "update images set status = 'unpublished' where id = %s", image_id)


# -- strips ---------------------------------------------------------------------------


async def test_strip_names_leaf_neighbours(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    add_image(clean_db, 1, vec=unit(0))
    b = add_image(clean_db, 2, vec=unit(0, 1))
    add_image(clean_db, 3, vec=unit(1))
    leaf(clean_db, b, 3, 14)

    def leaves(body: dict[str, Any]) -> dict[str, Any]:
        return {n["slug"]: n["leaf"] for n in body["similar"] + body["dissimilar"]}

    # Unpublished: the neighbour is a picture like any other.
    body = (await client.get("/api/images/img-1")).json()
    assert leaves(body) == {"img-2": None, "img-3": None}

    publish(clean_db)
    body = (await client.get("/api/images/img-1")).json()
    assert leaves(body) == {
        "img-2": {"collection": LINE, "book": 3, "position": 14},
        "img-3": None,
    }
    # A strip names where a leaf is, not where it was.
    assert "previous" not in body["similar"][0]["leaf"]


# -- mycelium -------------------------------------------------------------------------


async def test_gallery_cover_that_is_a_leaf_is_named(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_image(clean_db, 1)
    b = add_image(clean_db, 2)
    leaf(clean_db, a, 2, 7)
    colony(clean_db, 1, 1, "alive", members=(a,), cover=a)
    colony(clean_db, 2, 2, "alive", members=(b,), fruited=2, cover=b)
    public(clean_db)

    def covers(body: list[dict[str, Any]]) -> dict[int, Any]:
        return {g["serial"]: g["cover"].get("leaf") for g in body}

    assert covers((await client.get("/api/galleries")).json()) == {1: None, 2: None}
    assert "leaf" not in (await client.get("/api/galleries")).text
    publish(clean_db)
    body = (await client.get("/api/galleries")).json()
    assert covers(body) == {1: {"collection": LINE, "book": 2, "position": 7}, 2: None}
    # An ordinary cover keeps the shape it always had.
    assert "leaf" not in next(g for g in body if g["serial"] == 2)["cover"]


# -- home -----------------------------------------------------------------------------


async def test_home_line_names_the_week_s_change(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_image(clean_db, 1)
    b = add_image(clean_db, 2)
    leaf(clean_db, a, 3, 44)
    leaf(clean_db, b, 3, 45)
    moved(clean_db, a, None, (3, 44))

    async def changed() -> Any:
        return (await client.get("/api/stats")).json().get("changed")

    assert await changed() is None
    publish(clean_db)
    assert await changed() == {
        "collection": LINE,
        "book": 3,
        "position": 44,
        "slug": "img-1",
        "from": None,
    }

    # At one moment the filing outranks the shift it caused in its book, though the
    # shift was written after it.
    moved(clean_db, b, (3, 44), (3, 45))
    sql(clean_db, "update collection_leaf_history set at = now() - interval '1 minute'")
    assert (await changed())["slug"] == "img-1"

    # A move across books, later still.
    sql(clean_db, "update collection_leaves set book = 5, position = 2 where image_id = %s", b)
    moved(clean_db, b, (3, 45), (5, 2))
    got = await changed()
    assert (got["slug"], got["book"], got["position"]) == ("img-2", 5, 2)
    assert got["from"] == {"book": 3, "position": 45}

    # A change the leaf has since left is not stated, nor one whose picture is down.
    sql(clean_db, "update collection_leaves set position = 9 where image_id = %s", b)
    assert (await changed())["slug"] == "img-1"
    take_down(clean_db, a)
    assert await changed() is None

    # A quiet week says nothing.
    sql(clean_db, "update images set status = 'live'")
    sql(clean_db, "update collection_leaf_history set at = now() - interval '8 days'")
    assert await changed() is None


# -- search ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    ["ptp", "PTP", "pansophicon", "Πανσοφικὸν τῶν πάντων", "πανσοφικον", "ΠΤΠ", "π.τ.π.", "P.T.P"],
)
async def test_search_by_name_brings_the_collection_first(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
    q: str,
) -> None:
    a = add_image(clean_db, 1, vec=unit(0))
    b = add_image(clean_db, 2, vec=unit(1))
    c = add_image(clean_db, 3, vec=unit(2))
    leaf(clean_db, a, 3, 44)
    leaf(clean_db, b, 1, 2)
    leaf(clean_db, c, 1, 1)
    take_down(clean_db, c)
    add_image(clean_db, 4, vec=unit(3))
    fake_embed(monkeypatch, unit(3))

    body = (await client.get("/api/search", params={"q": q})).json()
    assert "collection" not in body
    publish(clean_db)
    body = (await client.get("/api/search", params={"q": q})).json()
    assert body["collection"] == {
        "slug": SLUG,
        "title": "Πανσοφικὸν τῶν πάντων",
        "abbrev": "ΠΤΠ",
        "abbrev_latin": "PTP",
    }
    # Its live leaves in book and leaf order, then the usual matches.
    assert [i["slug"] for i in body["items"]][:3] == ["img-2", "img-1", "img-4"]


@pytest.mark.parametrize("q", ["ptp iii 44", "ΠΤΠ iii, leaf 44", "PTP 3 44"])
async def test_search_by_named_leaf_reference(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
    q: str,
) -> None:
    a = add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(1))
    leaf(clean_db, a, 3, 44)
    asked = fake_embed(monkeypatch, unit(1))

    # Unpublished, a reference is ordinary words.
    body = (await client.get("/api/search", params={"q": q})).json()
    assert asked == [q]
    assert [i["slug"] for i in body["items"]][0] == "img-2"

    publish(clean_db)
    asked.clear()
    body = (await client.get("/api/search", params={"q": q})).json()
    assert [i["slug"] for i in body["items"]] == ["img-1"]
    assert body["tags"] == [] and asked == []


@pytest.mark.parametrize(
    ("q", "book", "place"), [("iii 44", 3, 44), ("iii.44", 3, 44), ("v 8", 5, 8), ("i 1", 1, 1)]
)
async def test_search_by_bare_leaf_reference_keeps_the_usual_search(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
    q: str,
    book: int,
    place: int,
) -> None:
    """Without the collection's name a reference is also just words: the leaf comes first
    and the ordinary tag and embedding search follows it."""
    a = add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(1))
    c = add_image(clean_db, 3, vec=unit(2))
    sql(clean_db, "insert into tags (image_id, tag, source) values (%s, %s, 'model')", c, q)
    leaf(clean_db, a, book, place)
    publish(clean_db)
    asked = fake_embed(monkeypatch, unit(1))

    body = (await client.get("/api/search", params={"q": q})).json()
    slugs = [i["slug"] for i in body["items"]]
    assert slugs[0] == "img-1"
    assert sorted(slugs) == ["img-1", "img-2", "img-3"]
    assert slugs.index("img-3") < slugs.index("img-2")  # the tagged one, as ever
    assert [t["tag"] for t in body["tags"]] == [q]
    assert asked == [q]


async def test_search_leaf_reference_that_resolves_to_nothing(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = add_image(clean_db, 1, vec=unit(0))
    add_image(clean_db, 2, vec=unit(1))
    leaf(clean_db, a, 3, 44)
    publish(clean_db)
    asked = fake_embed(monkeypatch, unit(1))
    # An empty slot, and arabic books without a name, are searched as words.
    for q in ("iii 45", "3 44"):
        body = (await client.get("/api/search", params={"q": q})).json()
        assert body["items"][0]["slug"] == "img-2"
    assert asked == ["iii 45", "3 44"]
    take_down(clean_db, a)
    body = (await client.get("/api/search", params={"q": "iii 44"})).json()
    assert "img-1" not in [i["slug"] for i in body["items"]]


# -- the collection's haiku -----------------------------------------------------------


def haiku(dsn: str, image_id: int, *lines: str) -> None:
    sql(
        dsn,
        "insert into haikus (l1, l2, l3, source, image_id) values (%s, %s, %s, 'model', %s)",
        *lines,
        image_id,
    )


async def test_collection_haiku_comes_from_its_leaves(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_image(clean_db, 1)
    b = add_image(clean_db, 2)
    other = add_image(clean_db, 3)
    leaf(clean_db, a, 3, 44)
    leaf(clean_db, b, 1, 2)
    publish(clean_db)
    url = f"/api/collections/{SLUG}"

    # The deck has haikus, but none of a leaf: the page has none.
    assert (await client.get(url)).json()["haiku"] is None
    haiku(clean_db, other, "not", "a", "leaf")
    assert (await client.get(url)).json()["haiku"] is None

    haiku(clean_db, a, "the vellum", "was", "salted")
    got = (await client.get(url)).json()["haiku"]
    assert got["lines"] == ["the vellum", "was", "salted"] and got["id"]

    # A leaf taken down, or a haiku set aside, speaks for nothing.
    take_down(clean_db, a)
    assert (await client.get(url)).json()["haiku"] is None
    sql(clean_db, "update images set status = 'live' where id = %s", a)
    sql(clean_db, "update haikus set active = false where image_id = %s", a)
    assert (await client.get(url)).json()["haiku"] is None

    # Unpublished, the collection is not here at all.
    sql(clean_db, "update haikus set active = true")
    publish(clean_db, False)
    assert (await client.get(url)).status_code == 404


async def test_search_by_name_answers_without_the_embedder(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No leaves, no tags, and nothing listening for embeddings: the name still finds it.
    publish(clean_db)
    monkeypatch.setenv("EMBED_SERVER_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    r = await client.get("/api/search", params={"q": "pansophicon"})
    assert r.status_code == 200, r.text
    assert r.json()["collection"]["slug"] == SLUG
    assert r.json()["items"] == []
    assert (await client.get("/api/search", params={"q": "a chair"})).status_code == 503
