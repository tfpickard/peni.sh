import html
import json
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pyvips
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from penish.api.haiku_render import Gen, render_haiku_html
from penish.api.media import preview_image
from penish.api.routes_collections import published
from penish.api.routes_galleries import gallery_card, parse_serial
from penish.api.routes_images import resolve_live
from penish.api.routes_meta import random_haiku
from penish.categories import SLUGS as CATEGORY_SLUGS
from penish.collection.numerals import roman
from penish.collection.store import leaf_for_image
from penish.config import get_settings

router = APIRouter()

_FALLBACK = (
    "<!doctype html><html><head><meta charset=utf-8><title>peni.sh</title>"
    "<!--og--></head><body></body></html>"
)
_BODY_OPEN = re.compile(r"<body[^>]*>", re.I)
_TITLE = re.compile(r"<title>.*?</title>", re.I | re.S)
SITE = "peni.sh"
_IMMUTABLE = "public, max-age=31536000, immutable"

# Paths (no leading slash) that are real pages. Everything else gets the shell with a 404 so
# crawlers do not see infinite pages; tests/test_api_meta.py walks web/src/routes to keep it honest.
_FIXED_PAGES = frozenset(
    {
        "",
        "categories",
        "tags",
        "galleries",
        "mycelium",
        "collections",
        "search",
        "admin",
        "admin/comments",
        "admin/mycelium",
        "admin/pictures",
        "admin/haikus",
        "admin/models",
        "admin/system",
        "admin/logs",
        "admin/queue",
        "admin/collections",
        "admin/stats",
    }
)

# The titles the SPA gives its list pages, so the server's <title> and the client's agree.
_PAGE_TITLES = {
    "categories": "categories",
    "tags": "tags",
    "galleries": "galleries",
    "mycelium": "map",
    "collections": "collections",
    "search": "search",
}


def _template(build: Path) -> str:
    f = build / "index.html"
    return f.read_text() if f.is_file() else _FALLBACK


def _script_json(value: object) -> str:
    # "<" is escaped so a haiku containing "</script>" cannot end the tag early.
    return json.dumps(value).replace("<", "\\u003c")


def _set_title(page: str, text: str) -> str:
    tag = f"<title>{html.escape(text, quote=False)}</title>"
    if _TITLE.search(page):
        return _TITLE.sub(lambda _: tag, page, count=1)
    return page.replace("</head>", tag + "</head>", 1)


async def render_index(
    request: Request,
    extra_head: str = "",
    title: str = SITE,
    status_code: int = 200,
    page_title: str | None = None,
) -> HTMLResponse:
    """Per-image pages pass their og tags through extra_head and their caption as title.

    The <title> is `<title> -- peni.sh`, as the SPA sets it client-side; page_title is for a
    page whose <title> differs from its og:title."""
    page = _template(get_settings().web_build_dir)
    haiku = await random_haiku(request)

    og = f'<meta property="og:title" content="{html.escape(title)}">' + extra_head
    page = page.replace("<!--og-->", og, 1)
    if page_title is None and title != SITE:
        page_title = f"{title} -- {SITE}"
    if page_title is not None:
        page = _set_title(page, page_title)

    # A fresh seed per request: the shape differs on every load, with or without JS.
    seed = secrets.randbits(32)
    gen: Gen = "ou" if request.query_params.get("gen") == "ou" else "noise"
    data = {"id": haiku["id"], "lines": haiku["lines"], "seed": seed, "gen": gen} if haiku else None
    script = (
        '<link rel="alternate" type="application/atom+xml" href="/feed.xml">'
        f"<script>window.__HAIKU__={_script_json(data)}</script>"
    )
    page = page.replace("</head>", script + "</head>", 1)

    # Only home draws a haiku; the client removes this copy once it has rendered the same one.
    if haiku and request.url.path == "/":
        h1 = render_haiku_html(haiku["lines"], seed=seed, gen=gen)
        page = _BODY_OPEN.sub(lambda m: m.group(0) + h1, page, count=1)

    # The haiku changes every load, so nothing may cache this.
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store"})


_OLD_LABEL = re.compile(r"^\s*(Subject|Setting|Style|Mood):\s*", re.M)


def summary(description: str, limit: int = 200) -> str:
    """The opening of the description, flattened to one line and cut at a word boundary."""
    # Descriptions written before the analysis split still carry the old labels.
    text = " ".join(_OLD_LABEL.sub("", description).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


def _preview_tags(base: str, pub_sha: str | None, w: int | None, h: int | None) -> list[str]:
    """og:image and its size and type for a picture: the JPEG, else the largest webp, else
    nothing. Absolute, as previewers need."""
    p = preview_image(pub_sha, w, h)
    if p is None:
        return []
    out = [_meta("og:image", base + p["url"]), _meta("og:image:width", p["w"])]
    if p["h"] is not None:
        out.append(_meta("og:image:height", p["h"]))
    out.append(_meta("og:image:type", p["type"]))
    return out


async def _image_page(request: Request, slug: str) -> Response:
    async with request.app.state.pool.connection() as conn:
        img = await resolve_live(conn, slug)
        if img is None:
            # The SPA draws "not here"; the status is for crawlers.
            return await render_index(request, status_code=404)
        if img["slug"] != slug:
            # A renamed picture: one address per picture, so crawlers consolidate on the new one.
            # no-store because the target moves again if the picture is renamed again.
            where = "/i/" + quote(img["slug"], safe="")
            if request.url.query:
                where += "?" + request.url.query
            return RedirectResponse(where, status_code=301, headers={"Cache-Control": "no-store"})
        cur = await conn.execute(
            "select field, body from image_texts where image_id = %s "
            "and field in ('caption', 'description')",
            (img["id"],),
        )
        texts = {r["field"]: r["body"] for r in await cur.fetchall()}
        leaf = await leaf_for_image(conn, img["id"])

    caption = texts.get("caption") or SITE
    tags = [_meta("og:type", "article"), _meta("twitter:card", "summary_large_image", "name")]
    # A leaf is a holding before it is a picture: the conservator's note speaks for it.
    note = leaf["note"] if leaf and leaf["note"] and leaf["note"].strip() else None
    if note:
        tags.append(_meta("og:description", summary(note)))
    elif "description" in texts:
        tags.append(_meta("og:description", summary(texts["description"])))
    base = str(request.base_url).rstrip("/")
    tags.extend(_preview_tags(base, img["pub_sha"], img["width"], img["height"]))
    return await render_index(request, "".join(tags), title=caption)


def _og_image(request: Request, variants: list[dict[str, Any]]) -> list[str]:
    webps = [v for v in variants if v["webp"]]
    if not webps:
        return []
    big = webps[-1]
    base = str(request.base_url).rstrip("/")
    out = [_meta("og:image", base + big["webp"]), _meta("og:image:width", big["w"])]
    if "h" in big:
        out.append(_meta("og:image:height", big["h"]))
    return out


def _meta(prop: str, value: object, attr: str = "property") -> str:
    return f'<meta {attr}="{prop}" content="{html.escape(str(value))}">'


def _gallery_line(card: dict[str, Any]) -> str:
    if card["state"] == "dead":
        days = card["died_days"] or 0
        if days == 0:
            return "died today"
        return f"died {days} day ago" if days == 1 else f"died {days} days ago"
    n = card["size"]
    return f"{n} picture" if n == 1 else f"{n} pictures"


async def _gallery_page(request: Request, raw: str) -> HTMLResponse:
    serial = parse_serial(raw)
    card = None
    if serial is not None:
        async with request.app.state.pool.connection() as conn:
            card = await gallery_card(conn, serial)
    if card is None:
        return await render_index(request, status_code=404)
    tags = [_meta("og:type", "website"), _meta("og:description", _gallery_line(card))]
    if card["cover"] is not None:
        tags.append(_meta("twitter:card", "summary_large_image", "name"))
        tags.extend(_og_image(request, card["cover"]["variants"]))
    return await render_index(request, "".join(tags), title=card["title"])


def home_description(haiku: dict[str, Any] | None) -> str | None:
    """The og:description of home: the haiku of the day. Return None to give it no description."""
    return None if haiku is None else " / ".join(haiku["lines"])


async def _haiku_of_the_day(conn: Any) -> dict[str, Any] | None:
    # Home draws a random haiku on every load; a preview must not change under a shared link,
    # so the choice is fixed per UTC day.
    cur = await conn.execute(
        "select l1, l2, l3 from haikus where active and colony_id is null"
        " order by md5(id::text || %s) limit 1",
        (datetime.now(UTC).date().isoformat(),),
    )
    row = await cur.fetchone()
    return None if row is None else {"lines": [row["l1"], row["l2"], row["l3"]]}


async def _home_page(request: Request) -> HTMLResponse:
    async with request.app.state.pool.connection() as conn:
        haiku = await _haiku_of_the_day(conn)
        cur = await conn.execute(
            "select pub_sha, width, height from images"
            " where status = 'live' and published_at is not null"
            " order by published_at desc, id desc limit 1"
        )
        newest = await cur.fetchone()
    tags = [_meta("og:type", "website")]
    desc = home_description(haiku)
    if desc:
        tags.append(_meta("og:description", desc))
    if newest is not None:
        base = str(request.base_url).rstrip("/")
        img = _preview_tags(base, newest["pub_sha"], newest["width"], newest["height"])
        if img:
            tags.append(_meta("twitter:card", "summary_large_image", "name"))
            tags.extend(img)
    return await render_index(request, "".join(tags))


@lru_cache(maxsize=8)
def _png_size(path: str, mtime_ns: int) -> tuple[int, int] | None:
    # mtime_ns keys the cache, so a replaced file is measured again.
    try:
        img = pyvips.Image.new_from_file(path)
        return img.width, img.height
    except pyvips.Error:
        return None


def _static_og(slug: str) -> tuple[str, tuple[int, int] | None] | None:
    """The owner's preview image for a collection, `og/<slug>.png` in the built static dir."""
    path = get_settings().web_build_dir / "og" / f"{slug}.png"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return f"/og/{slug}.png", _png_size(str(path), mtime)


async def _collection_image(conn: Any, c: dict[str, Any], base: str) -> list[str]:
    """The owner's PNG if one was dropped in, else the first leaf's JPEG, else nothing."""
    static = _static_og(c["slug"])
    if static is not None:
        url, size = static
        out = [_meta("og:image", base + url)]
        if size is not None:
            out += [_meta("og:image:width", size[0]), _meta("og:image:height", size[1])]
        return [*out, _meta("og:image:type", "image/png")]
    cur = await conn.execute(
        """
        select i.pub_sha, i.width, i.height
          from collection_leaves l join images i on i.id = l.image_id
         where l.collection_id = %s and i.status = 'live' and i.published_at is not null
         order by l.book, l.position limit 1
        """,
        (c["id"],),
    )
    row = await cur.fetchone()
    if row is None:
        return []
    return _preview_tags(base, row["pub_sha"], row["width"], row["height"])


async def _collection_page(request: Request, slug: str, book: str | None) -> HTMLResponse:
    """A published collection says its gloss and shows an image; nothing else about itself."""
    async with request.app.state.pool.connection() as conn:
        c = await published(conn, slug)
        if c is None:
            return await render_index(request, status_code=404)
        if book is None:
            tags = [_meta("og:type", "website")]
            if c["gloss"] and c["gloss"].strip():
                tags.append(_meta("og:description", " ".join(c["gloss"].split())))
            image = await _collection_image(conn, c, str(request.base_url).rstrip("/"))
            if image:
                tags.append(_meta("twitter:card", "summary_large_image", "name"))
                tags.extend(image)
            return await render_index(request, "".join(tags), title=c["title"])
    if not (book.isascii() and book.isdigit()) or not 1 <= int(book) <= c["book_count"]:
        return await render_index(request, status_code=404)
    title = f"{c['abbrev']} {roman(int(book))}"
    return await render_index(request, _meta("og:type", "website"), title=title)


async def _is_known_page(request: Request, path: str) -> bool:
    if path in _FIXED_PAGES:
        return True
    if path.startswith("c/"):
        return path[2:] in CATEGORY_SLUGS
    if path.startswith("t/") and len(path) > 2:
        # Tags are free text, so the rest of the path is the tag, slashes included.
        async with request.app.state.pool.connection() as conn:
            cur = await conn.execute(
                "select 1 from tags t join images i on i.id = t.image_id "
                "where t.tag = %s and i.status = 'live' and i.published_at is not null limit 1",
                (path[2:],),
            )
            return await cur.fetchone() is not None
    # Digits only, same rule as /g/.
    if path.startswith("admin/pictures/"):
        rest = path[len("admin/pictures/") :]
        return rest.isascii() and rest.isdigit()
    return False


# HEAD too: link unfurlers and uptime checks probe with HEAD before GET.
@router.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def catch_all(path: str, request: Request) -> Response:
    if path == "api" or path.startswith(("api/", "media/")) or path == "media":
        raise HTTPException(status_code=404, detail="not here")

    if path.startswith("i/") and "/" not in path[2:] and path[2:]:
        return await _image_page(request, path[2:])

    # Digits only: anything else under g/ is not an address and falls through.
    if path.startswith("g/") and path[2:].isascii() and path[2:].isdigit():
        return await _gallery_page(request, path[2:])

    if path.startswith("collections/"):
        parts = path.split("/")[1:]
        if 1 <= len(parts) <= 2 and parts[0]:
            return await _collection_page(request, parts[0], parts[1] if len(parts) == 2 else None)

    build = get_settings().web_build_dir.resolve()
    if path:
        target = (build / path).resolve()
        # resolve() collapses ".." so a traversal attempt falls out of the build dir.
        if target.is_file() and target.is_relative_to(build) and target.name != "index.html":
            headers = {"Cache-Control": _IMMUTABLE} if path.startswith("_app/immutable/") else {}
            return FileResponse(target, headers=headers)
    if await _is_known_page(request, path):
        if path == "":
            return await _home_page(request)
        if path in _PAGE_TITLES:
            return await render_index(request, page_title=f"{_PAGE_TITLES[path]} -- {SITE}")
        if path.startswith("c/"):
            return await render_index(request, title=path[2:].replace("-", " "))
        if path.startswith("t/"):
            return await render_index(request, title=path[2:])
        return await render_index(request)
    return await render_index(request, status_code=404)
