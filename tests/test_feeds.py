"""/sitemap.xml and /feed.xml: valid XML, and only what is live or published."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx
import psycopg

from penish.categories import SLUGS
from tests import test_api_meta
from tests.test_api_meta import BASE, _leaf, _pic, _publish, _sha
from tests.test_galleries_api import colony, public, ticks

# The app over a temporary build and media dir, as the meta tests have it.
client = test_api_meta.client

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
AT = "{http://www.w3.org/2005/Atom}"
W3C = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def sql(dsn: str, q: str, *args: object) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(q.encode(), args)


async def sitemap(client: httpx.AsyncClient) -> dict[str, ET.Element]:
    r = await client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert r.content.startswith(b"<?xml")
    root = ET.fromstring(r.content)
    assert root.tag == f"{SM}urlset"
    urls = {u.findtext(f"{SM}loc") or "": u for u in root}
    assert len(urls) == len(root), "a url listed twice"
    return urls


def history(dsn: str, image_id: int, book: int, position: int, at: datetime) -> None:
    sql(
        dsn,
        "insert into collection_leaf_history (image_id, collection_id, to_book, to_position,"
        " reason, at) select %s, id, %s, %s, 'auto', %s from collections"
        " where slug = 'pansophicon'",
        image_id,
        book,
        position,
        at,
    )


async def test_empty_sitemap_is_valid(client: httpx.AsyncClient) -> None:
    assert list(await sitemap(client)) == ["http://t/"]


async def test_sitemap_lists_what_is_live(client: httpx.AsyncClient, clean_db: str) -> None:
    live = _pic(clean_db, 1, caption="one")
    also = _pic(clean_db, 2, caption="two")
    hidden = _pic(clean_db, 3, caption="three", live=False)
    for image_id, category in ((live, SLUGS[0]), (hidden, SLUGS[1])):
        sql(
            clean_db,
            "insert into image_categories (image_id, category, source) values (%s, %s, 'model')",
            image_id,
            category,
        )
    for image_id, tag in ((live, "moss"), (also, "a/b c"), (hidden, "unseen")):
        sql(
            clean_db,
            "insert into tags (image_id, tag, source) values (%s, %s, 'model')",
            image_id,
            tag,
        )
    sql(clean_db, "update images set updated_at = published_at where published_at is not null")
    urls = await sitemap(client)
    assert set(urls) == {
        "http://t/",
        "http://t/i/p-1",
        "http://t/i/p-2",
        f"http://t/c/{SLUGS[0]}",
        "http://t/t/moss",
        "http://t/t/a%2Fb%20c",
    }
    assert urls["http://t/"].find(f"{SM}lastmod") is None
    for loc, u in urls.items():
        if loc != "http://t/":
            assert W3C.match(u.findtext(f"{SM}lastmod") or ""), loc
    assert urls["http://t/i/p-2"].findtext(f"{SM}lastmod") == "2026-03-01T00:02:00Z"
    # The encoded address is one the page answers.
    assert (await client.get("/t/a%2Fb%20c")).status_code == 200


async def test_sitemap_galleries_only_the_live_ones(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    a = _pic(clean_db, 1, caption="a")
    ticks(clean_db, 3, BASE)
    colony(clean_db, 1, 1, "alive", members=(a,))
    colony(clean_db, 2, 2, "fading", members=(a,), faded=2)
    colony(clean_db, 3, 3, "dead", members=(a,), died=2, cause="starved")
    colony(clean_db, 4, None, "forming", members=(a,), fruited=None)
    # Not served until the first live tick: the address may still be renumbered.
    assert not {u for u in await sitemap(client) if "/g/" in u}
    public(clean_db)
    assert {u for u in await sitemap(client) if "/g/" in u} == {"http://t/g/1", "http://t/g/2"}


async def test_sitemap_collection_and_its_books(client: httpx.AsyncClient, clean_db: str) -> None:
    leaf = _pic(clean_db, 1, caption="a leaf")
    _leaf(clean_db, leaf, 3, 14)
    assert not [u for u in await sitemap(client) if "/collections" in u]
    _publish(clean_db)
    urls = await sitemap(client)
    mine = {u for u in urls if "/collections" in u}
    assert mine == {
        "http://t/collections/pansophicon",
        *(f"http://t/collections/pansophicon/{n}" for n in range(1, 8)),
    }
    # The leaf is a picture and listed as one; only the book that holds it has a lastmod.
    assert "http://t/i/p-1" in urls
    book = {u: e.find(f"{SM}lastmod") is not None for u, e in urls.items() if "/collections/" in u}
    assert book["http://t/collections/pansophicon/3"]
    assert not book["http://t/collections/pansophicon/4"]


async def feed(client: httpx.AsyncClient) -> ET.Element:
    r = await client.get("/feed.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/atom+xml")
    root = ET.fromstring(r.content)
    assert root.tag == f"{AT}feed"
    return root


def entries(root: ET.Element) -> list[ET.Element]:
    return root.findall(f"{AT}entry")


def links(e: ET.Element, rel: str) -> list[ET.Element]:
    return [x for x in e.findall(f"{AT}link") if x.get("rel") == rel]


async def test_empty_feed_is_valid(client: httpx.AsyncClient) -> None:
    root = await feed(client)
    assert entries(root) == []
    assert W3C.match(root.findtext(f"{AT}updated") or "")


async def test_feed_level_is_the_title_only(client: httpx.AsyncClient, clean_db: str) -> None:
    _pic(clean_db, 1, caption="one")
    root = await feed(client)
    assert sorted(c.tag.removeprefix(AT) for c in root if c.tag != f"{AT}entry") == [
        "author",
        "id",
        "link",
        "link",
        "title",
        "updated",
    ]
    assert root.findtext(f"{AT}title") == "peni.sh"
    # RFC 4287 4.1.1: a feed needs an author when its entries have none.
    assert root.findtext(f"{AT}author/{AT}name") == "peni.sh"
    assert root.findtext(f"{AT}id")
    (self_link,) = links(root, "self")
    assert self_link.get("href") == "http://t/feed.xml"
    assert root.findtext(f"{AT}updated") == "2026-03-01T00:01:00Z"


async def test_feed_entries_are_live_pictures_newest_first(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _pic(clean_db, 1, caption="old", description="Subject: an old lamp.\nIt hums.")
    _pic(clean_db, 2, caption="Tom & <Jerry>", description="new")
    _pic(clean_db, 3, caption="hidden", live=False)
    _pic(clean_db, 4, caption="no jpeg", jpg=False)
    _pic(clean_db, 5, caption="no files", jpg=False, webp=False)
    got = entries(await feed(client))
    assert [e.findtext(f"{AT}title") for e in got] == [
        "no files",
        "no jpeg",
        "Tom & <Jerry>",
        "old",
    ]
    old = got[3]
    assert old.findtext(f"{AT}summary") == "an old lamp. It hums."
    (alt,) = links(old, "alternate")
    assert alt.get("href") == "http://t/i/p-1"
    (enc,) = links(old, "enclosure")
    assert enc.get("type") == "image/jpeg"
    assert enc.get("href") == f"http://t/media/pub/{_sha(1)}-1200.jpg"
    assert W3C.match(old.findtext(f"{AT}updated") or "")
    (webp,) = links(got[1], "enclosure")
    assert (webp.get("type"), webp.get("href")) == (
        "image/webp",
        f"http://t/media/pub/{_sha(4)}-1600.webp",
    )
    assert links(got[0], "enclosure") == []
    assert got[0].find(f"{AT}summary") is None
    ids = [e.findtext(f"{AT}id") for e in got]
    assert len(set(ids)) == len(ids) and all(ids)


async def test_feed_ids_survive_a_rename(client: httpx.AsyncClient, clean_db: str) -> None:
    _pic(clean_db, 1, caption="one")
    before = entries(await feed(client))[0].findtext(f"{AT}id")
    sql(clean_db, "update images set slug = 'renamed'")
    after = entries(await feed(client))[0]
    assert after.findtext(f"{AT}id") == before
    assert links(after, "alternate")[0].get("href") == "http://t/i/renamed"


async def test_feed_is_cut_at_thirty(client: httpx.AsyncClient, clean_db: str) -> None:
    for n in range(1, 36):
        _pic(clean_db, n, caption=f"p{n}")
    got = entries(await feed(client))
    assert len(got) == 30
    assert got[0].findtext(f"{AT}title") == "p35"


async def test_feed_carries_new_leaves_merged_by_time(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    _publish(clean_db)
    older = _pic(clean_db, 1, caption="the oldest", description="plain description")
    newer = _pic(clean_db, 2, caption="a newer one", description="its description")
    _pic(clean_db, 3, caption="the newest")
    _leaf(clean_db, older, 3, 14, note="Foxed at the gutter.")
    _leaf(clean_db, newer, 1, 2)
    # Filed after the newest picture was published, and before the newer leaf.
    history(clean_db, older, 3, 14, BASE + timedelta(minutes=10))
    history(clean_db, newer, 1, 2, BASE + timedelta(minutes=11))
    # A move is not a filing.
    sql(
        clean_db,
        "insert into collection_leaf_history (image_id, collection_id, from_book, from_position,"
        " to_book, to_position, reason, at) select %s, id, 1, 1, 1, 2, 'resort', %s"
        " from collections",
        newer,
        BASE + timedelta(minutes=20),
    )
    got = entries(await feed(client))
    summaries = [(e.findtext(f"{AT}title"), e.findtext(f"{AT}summary")) for e in got]
    assert summaries == [
        ("a newer one", "ptp i, leaf 2\nits description"),
        ("the oldest", "ptp iii, leaf 14\nFoxed at the gutter."),
        ("the newest", None),
        ("a newer one", "its description"),
        ("the oldest", "plain description"),
    ]
    ids = [e.findtext(f"{AT}id") for e in got]
    assert len(set(ids)) == 5


async def test_feed_has_no_leaves_of_an_unpublished_collection(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    one = _pic(clean_db, 1, caption="one")
    _leaf(clean_db, one, 1, 1)
    history(clean_db, one, 1, 1, BASE + timedelta(minutes=5))
    assert [e.findtext(f"{AT}title") for e in entries(await feed(client))] == ["one"]
    _publish(clean_db)
    assert len(entries(await feed(client))) == 2


async def test_feed_leaf_taken_down_is_gone(client: httpx.AsyncClient, clean_db: str) -> None:
    _publish(clean_db)
    one = _pic(clean_db, 1, caption="one")
    _leaf(clean_db, one, 1, 1)
    history(clean_db, one, 1, 1, BASE + timedelta(minutes=5))
    sql(clean_db, "update images set status = 'unpublished', published_at = null")
    assert entries(await feed(client)) == []
    assert (await client.get("/sitemap.xml")).status_code == 200
    assert "p-1" not in (await client.get("/sitemap.xml")).text


async def test_feed_and_sitemap_answer_head(client: httpx.AsyncClient, clean_db: str) -> None:
    _pic(clean_db, 1, caption="one")
    assert (await client.head("/feed.xml")).status_code == 200
    assert (await client.head("/sitemap.xml")).status_code == 200


async def test_control_characters_do_not_break_the_feed(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    # Not allowed in XML 1.0 even as a character reference, so one bad caption would
    # otherwise make every reader refuse the whole feed.
    _pic(clean_db, 1, caption="a\x0bb\x08c\ud7ffd\ufffe", description="de\x1bf\tg\nh")
    sql(
        clean_db,
        "insert into tags (image_id, tag, source) select id, 'x\x0by', 'model' from images",
    )
    got = entries(await feed(client))
    assert got[0].findtext(f"{AT}title") == "abc\ud7ffd"
    assert got[0].findtext(f"{AT}summary") == "def g h"
    assert len(await sitemap(client)) == 3
