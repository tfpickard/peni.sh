import html
import json
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from penish.api.haiku_render import Gen, render_haiku_html
from penish.api.media import variants_for
from penish.api.routes_galleries import gallery_card, parse_serial
from penish.api.routes_images import resolve_live
from penish.api.routes_meta import random_haiku
from penish.config import get_settings

router = APIRouter()

_FALLBACK = (
    "<!doctype html><html><head><meta charset=utf-8><title>peni.sh</title>"
    "<!--og--></head><body></body></html>"
)
_BODY_OPEN = re.compile(r"<body[^>]*>", re.I)
_IMMUTABLE = "public, max-age=31536000, immutable"


def _template(build: Path) -> str:
    f = build / "index.html"
    return f.read_text() if f.is_file() else _FALLBACK


def _script_json(value: object) -> str:
    # "<" is escaped so a haiku containing "</script>" cannot end the tag early.
    return json.dumps(value).replace("<", "\\u003c")


async def render_index(
    request: Request, extra_head: str = "", title: str = "peni.sh", status_code: int = 200
) -> HTMLResponse:
    """Per-image pages pass their og tags through extra_head and their caption as title."""
    page = _template(get_settings().web_build_dir)
    haiku = await random_haiku(request)

    og = f'<meta property="og:title" content="{html.escape(title)}">' + extra_head
    page = page.replace("<!--og-->", og, 1)

    # A fresh seed per request: the shape differs on every load, with or without JS.
    seed = secrets.randbits(32)
    gen: Gen = "ou" if request.query_params.get("gen") == "ou" else "noise"
    data = {"id": haiku["id"], "lines": haiku["lines"], "seed": seed, "gen": gen} if haiku else None
    script = f"<script>window.__HAIKU__={_script_json(data)}</script>"
    page = page.replace("</head>", script + "</head>", 1)

    # Only home draws a haiku; the client removes this copy once it has rendered the same one.
    if haiku and request.url.path == "/":
        h1 = render_haiku_html(haiku["lines"], seed=seed, gen=gen)
        page = _BODY_OPEN.sub(lambda m: m.group(0) + h1, page, count=1)

    # The haiku changes every load, so nothing may cache this.
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store"})


_OLD_LABEL = re.compile(r"^\s*(Subject|Setting|Style|Mood):\s*", re.M)


def _summary(description: str, limit: int = 200) -> str:
    """The opening of the description, flattened to one line and cut at a word boundary."""
    # Descriptions written before the analysis split still carry the old labels.
    text = " ".join(_OLD_LABEL.sub("", description).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


async def _image_page(request: Request, slug: str) -> HTMLResponse:
    async with request.app.state.pool.connection() as conn:
        img = await resolve_live(conn, slug)
        if img is None:
            # The SPA draws "not here"; the status is for crawlers.
            return await render_index(request, status_code=404)
        cur = await conn.execute(
            "select field, body from image_texts where image_id = %s "
            "and field in ('caption', 'description')",
            (img["id"],),
        )
        texts = {r["field"]: r["body"] for r in await cur.fetchall()}

    def tag(prop: str, value: object, attr: str = "property") -> str:
        return f'<meta {attr}="{prop}" content="{html.escape(str(value))}">'

    caption = texts.get("caption") or "peni.sh"
    tags = [tag("og:type", "article"), tag("twitter:card", "summary_large_image", "name")]
    if "description" in texts:
        tags.append(tag("og:description", _summary(texts["description"])))
    variants = variants_for(img["pub_sha"], img["width"], img["height"])
    webps = [v for v in variants if v["webp"]]
    if webps:
        big = webps[-1]
        base = str(request.base_url).rstrip("/")
        tags.append(tag("og:image", base + big["webp"]))
        tags.append(tag("og:image:width", big["w"]))
        if "h" in big:
            tags.append(tag("og:image:height", big["h"]))
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

    build = get_settings().web_build_dir.resolve()
    if path:
        target = (build / path).resolve()
        # resolve() collapses ".." so a traversal attempt falls out of the build dir.
        if target.is_file() and target.is_relative_to(build) and target.name != "index.html":
            headers = {"Cache-Control": _IMMUTABLE} if path.startswith("_app/immutable/") else {}
            return FileResponse(target, headers=headers)
    return await render_index(request)
