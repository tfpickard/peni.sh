"""/sitemap.xml and /feed.xml: what a crawler or a reader may list. Only live pictures,
published collections and public galleries are named; nothing here says what the site is."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from penish.api.index import SITE, summary
from penish.api.media import preview_image
from penish.categories import SLUGS as CATEGORY_SLUGS
from penish.collection.numerals import roman
from penish.mycelium.store import galleries_public

router = APIRouter()

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_ATOM_NS = "http://www.w3.org/2005/Atom"
FEED_SIZE = 30
# Entry ids outlive a slug rename and a change of host: tag URIs on the id of the thing.
_TAG = "tag:peni.sh,2026:"
# A description is not cut in the feed; the limit only has to be larger than any of them.
_WHOLE = 1_000_000

# XML 1.0 has no place for most control characters, and a caption with one in it would make
# the whole document unparseable.
_FORBIDDEN = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

_LIVE = "i.status = 'live' and i.published_at is not null"


def _clean(text: str) -> str:
    return _FORBIDDEN.sub("", text)


def _w3c(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _xml(root: ET.Element, media_type: str) -> Response:
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return Response(body, media_type=media_type, headers={"Cache-Control": "public, max-age=600"})


async def _rows(conn: Any, query: str, *args: Any) -> list[dict[str, Any]]:
    cur = await conn.execute(query.encode(), args)
    rows: list[dict[str, Any]] = await cur.fetchall()
    return rows


async def _sitemap_urls(conn: Any) -> list[tuple[str, datetime | None]]:
    urls: list[tuple[str, datetime | None]] = [("/", None)]

    for r in await _rows(
        conn,
        f"select i.slug, greatest(i.updated_at, i.published_at) as at from images i"
        f" where {_LIVE} and i.slug is not null order by i.published_at desc, i.id desc",
    ):
        urls.append((f"/i/{quote(r['slug'], safe='')}", r["at"]))

    known = set(CATEGORY_SLUGS)
    for r in await _rows(
        conn,
        f"select c.category, max(i.published_at) as at from image_categories c"
        f" join images i on i.id = c.image_id where {_LIVE} group by c.category"
        f" order by c.category",
    ):
        if r["category"] in known:
            urls.append((f"/c/{r['category']}", r["at"]))

    for r in await _rows(
        conn,
        f"select t.tag, max(i.published_at) as at from tags t"
        f" join images i on i.id = t.image_id where {_LIVE} group by t.tag order by t.tag",
    ):
        urls.append((f"/t/{quote(r['tag'], safe='')}", r["at"]))

    if await galleries_public(conn):
        for r in await _rows(
            conn,
            "select serial, updated_at as at from colonies"
            " where serial is not null and state in ('alive', 'fading') order by serial",
        ):
            urls.append((f"/g/{r['serial']}", r["at"]))

    for c in await _rows(
        conn, "select id, slug, updated_at as at from collections where published order by id"
    ):
        urls.append((f"/collections/{c['slug']}", c["at"]))
        for b in await _rows(
            conn,
            "select b.number, (select max(l.updated_at) from collection_leaves l"
            "  join images i on i.id = l.image_id"
            f" where l.collection_id = b.collection_id and l.book = b.number and {_LIVE}) as at"
            " from collection_books b where b.collection_id = %s order by b.number",
            c["id"],
        ):
            urls.append((f"/collections/{c['slug']}/{b['number']}", b["at"]))
    return urls


# HEAD too, as the shell answers it: crawlers probe before they fetch.
@router.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def sitemap(request: Request) -> Response:
    async with request.app.state.pool.connection() as conn:
        urls = await _sitemap_urls(conn)
    base = str(request.base_url).rstrip("/")
    ET.register_namespace("", _SITEMAP_NS)
    root = ET.Element(f"{{{_SITEMAP_NS}}}urlset")
    for path, at in urls:
        url = ET.SubElement(root, f"{{{_SITEMAP_NS}}}url")
        ET.SubElement(url, f"{{{_SITEMAP_NS}}}loc").text = _clean(base + path)
        if at is not None:
            ET.SubElement(url, f"{{{_SITEMAP_NS}}}lastmod").text = _w3c(at)
    return _xml(root, "application/xml")


_PICTURES = f"""
    select i.id, i.slug, i.pub_sha, i.width, i.height, i.published_at as at,
           c.body as caption, d.body as description
      from images i
      left join image_texts c on c.image_id = i.id and c.field = 'caption'
      left join image_texts d on d.image_id = i.id and d.field = 'description'
     where {_LIVE} and i.slug is not null
     order by i.published_at desc, i.id desc
     limit %s
"""

# The latest filing of each leaf, in the place the leaf holds now: a later move would make
# the place a filing named false.
_LEAVES = f"""
    select * from (
      select distinct on (h.image_id, h.collection_id)
             h.id as history_id, h.at, i.id, i.slug, i.pub_sha, i.width, i.height,
             c.body as caption, d.body as description,
             l.note, co.abbrev_latin, l.book, l.position
        from collection_leaf_history h
        join collections co on co.id = h.collection_id and co.published
        join collection_leaves l on l.image_id = h.image_id and l.collection_id = h.collection_id
        join images i on i.id = h.image_id
        left join image_texts c on c.image_id = i.id and c.field = 'caption'
        left join image_texts d on d.image_id = i.id and d.field = 'description'
       where h.from_book is null and h.to_book is not null and {_LIVE} and i.slug is not null
       order by h.image_id, h.collection_id, h.id desc
    ) f
    order by f.at desc, f.history_id desc
    limit %s
"""


def _entry(
    base: str, r: dict[str, Any], *, id_: str, at: datetime, text: str | None
) -> dict[str, Any]:
    return {
        "id": id_,
        "at": at,
        "title": r["caption"] or SITE,
        "summary": text,
        "href": f"{base}/i/{quote(r['slug'], safe='')}",
        "enclosure": preview_image(r["pub_sha"], r["width"], r["height"]),
    }


async def _feed_entries(conn: Any, base: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for r in await _rows(conn, _PICTURES, FEED_SIZE):
        text = summary(r["description"], _WHOLE) if r["description"] else None
        entries.append(_entry(base, r, id_=f"{_TAG}picture/{r['id']}", at=r["at"], text=text))
    for r in await _rows(conn, _LEAVES, FEED_SIZE):
        line = f"{r['abbrev_latin'].lower()} {roman(r['book'])}, leaf {r['position']}"
        # The note when there is one, as on the leaf's page.
        body = r["note"] if r["note"] and r["note"].strip() else r["description"]
        text = f"{line}\n{summary(body, _WHOLE)}" if body else line
        leaf_id = f"{_TAG}leaf/{r['id']}/{r['history_id']}"
        entries.append(_entry(base, r, id_=leaf_id, at=r["at"], text=text))
    entries.sort(key=lambda e: e["at"], reverse=True)
    return entries[:FEED_SIZE]


@router.api_route("/feed.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def feed(request: Request) -> Response:
    base = str(request.base_url).rstrip("/")
    async with request.app.state.pool.connection() as conn:
        entries = await _feed_entries(conn, base)
    ET.register_namespace("", _ATOM_NS)

    def el(parent: ET.Element, name: str, text: str | None = None, **attrs: str) -> ET.Element:
        e = ET.SubElement(parent, f"{{{_ATOM_NS}}}{name}", {k: _clean(v) for k, v in attrs.items()})
        if text is not None:
            e.text = _clean(text)
        return e

    root = ET.Element(f"{{{_ATOM_NS}}}feed")
    el(root, "title", SITE)
    el(root, "id", f"{_TAG}feed")
    el(root, "updated", _w3c(entries[0]["at"] if entries else datetime.now(UTC)))
    el(root, "link", rel="self", type="application/atom+xml", href=f"{base}/feed.xml")
    el(root, "link", rel="alternate", href=f"{base}/")
    # Required of a feed whose entries name none (RFC 4287, 4.1.1): the site, as everywhere.
    el(el(root, "author"), "name", SITE)
    for e in entries:
        item = el(root, "entry")
        el(item, "title", e["title"])
        el(item, "id", e["id"])
        el(item, "updated", _w3c(e["at"]))
        el(item, "link", rel="alternate", href=e["href"])
        enc = e["enclosure"]
        if enc is not None:
            el(item, "link", rel="enclosure", type=enc["type"], href=base + enc["url"])
        if e["summary"]:
            el(item, "summary", e["summary"], type="text")
    return _xml(root, "application/atom+xml")
