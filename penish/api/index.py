import html
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from penish.api.media import variants_for
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

    data = {"id": haiku["id"], "lines": haiku["lines"]} if haiku else None
    script = f"<script>window.__HAIKU__={_script_json(data)}</script>"
    page = page.replace("</head>", script + "</head>", 1)

    if haiku:
        lines = "<br>".join(html.escape(line) for line in haiku["lines"])
        h1 = f'<h1 class="haiku-ssr">{lines}</h1>'
        page = _BODY_OPEN.sub(lambda m: m.group(0) + h1, page, count=1)

    # The haiku changes every load, so nothing may cache this.
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store"})


def _subject(description: str, limit: int = 200) -> str:
    """The Subject paragraph without its label, cut at a word boundary."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", description) if p.strip()]
    para = next((p for p in paras if p.lower().startswith("subject:")), paras[0] if paras else "")
    para = re.sub(r"^subject:\s*", "", para, flags=re.I)
    if len(para) <= limit:
        return para
    return para[:limit].rsplit(" ", 1)[0]


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
        tags.append(tag("og:description", _subject(texts["description"])))
    variants = variants_for(img["sha256"], img["width"], img["height"])
    webps = [v for v in variants if v["webp"]]
    if webps:
        big = webps[-1]
        base = str(request.base_url).rstrip("/")
        tags.append(tag("og:image", base + big["webp"]))
        tags.append(tag("og:image:width", big["w"]))
        if "h" in big:
            tags.append(tag("og:image:height", big["h"]))
    return await render_index(request, "".join(tags), title=caption)


@router.get("/{path:path}", include_in_schema=False)
async def catch_all(path: str, request: Request) -> Response:
    if path == "api" or path.startswith(("api/", "media/")) or path == "media":
        raise HTTPException(status_code=404, detail="not here")

    if path.startswith("i/") and "/" not in path[2:] and path[2:]:
        return await _image_page(request, path[2:])

    build = get_settings().web_build_dir.resolve()
    if path:
        target = (build / path).resolve()
        # resolve() collapses ".." so a traversal attempt falls out of the build dir.
        if target.is_file() and target.is_relative_to(build) and target.name != "index.html":
            headers = {"Cache-Control": _IMMUTABLE} if path.startswith("_app/immutable/") else {}
            return FileResponse(target, headers=headers)
    return await render_index(request)
