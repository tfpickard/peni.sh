import os
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from penish.config import get_settings
from penish.worker.derive import og_key, og_size

router = APIRouter()

_IMMUTABLE = "public, max-age=31536000, immutable"
# Strict on purpose: the name is the only thing between a request and the filesystem.
_NAME = re.compile(r"^[0-9a-f]{64}-\d+\.(avif|webp|jpg)$")
# The responsive ladder. The og JPEG is served but never offered as a variant.
_LADDER = re.compile(r"^[0-9a-f]{64}-\d+\.(avif|webp)$")
_TYPES = {"avif": "image/avif", "webp": "image/webp", "jpg": "image/jpeg"}
_PUB_URL = "/media/pub/"


def _scan(shas: Iterable[str]) -> dict[str, dict[int, dict[str, str]]]:
    """One directory scan for any number of pub_shas: pub_sha -> width -> format -> filename."""
    wanted = set(shas)
    found: dict[str, dict[int, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    try:
        it = os.scandir(get_settings().media_pub)
    except FileNotFoundError:
        return {}
    with it:
        for entry in it:
            name = entry.name
            if not _LADDER.match(name) or name[:64] not in wanted:
                continue
            width, fmt = name[65:].split(".")
            found[name[:64]][int(width)][fmt] = name
    return found


def _shape(
    by_width: dict[int, dict[str, str]], width: int | None, height: int | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in sorted(by_width):
        fmts = by_width[w]
        item: dict[str, Any] = {
            "w": w,
            "avif": _PUB_URL + fmts["avif"] if "avif" in fmts else None,
            "webp": _PUB_URL + fmts["webp"] if "webp" in fmts else None,
        }
        if width and height:
            item["h"] = round(w * height / width)
        out.append(item)
    return out


def variants_for_many(
    images: Iterable[tuple[str, int | None, int | None]],
) -> dict[str, list[dict[str, Any]]]:
    """pub_sha -> variants for a batch, so a page of neighbors costs one directory scan."""
    dims = {sha: (w, h) for sha, w, h in images}
    found = _scan(dims)
    return {sha: _shape(found.get(sha, {}), *dims[sha]) for sha in dims}


def variants_for(
    pub_sha: str, width: int | None = None, height: int | None = None
) -> list[dict[str, Any]]:
    """Widths that exist on disk for one image, smallest first.

    Listing the directory keeps the API honest about small sources that only have
    a native width. If the scan gets slow, swap this for a variants column.
    """
    return variants_for_many([(pub_sha, width, height)])[pub_sha]


def og_image(
    pub_sha: str | None, width: int | None, height: int | None
) -> tuple[str, int, int] | None:
    """The link-preview JPEG for a picture: (url, width, height), or None.

    Pass the picture's images row as it stands: pub_sha, width and height of the displayed
    pixels (crop included). The url is site-relative (`/media/pub/<pub_sha>-<w>.jpg`); the
    caller prefixes the request's base url, as og:image must be absolute. width is
    min(1200, the picture's width), never upscaled; height keeps the aspect ratio and is
    exactly the file's.

    None when the row lacks any of the three or the file is not in pub: not derived yet
    (the worker-start sweep queues it), failed to encode, or held while unpublished. The
    caller then falls back to the largest webp or leaves og:image out. One stat, no read.
    """
    if not pub_sha or not width or not height or len(pub_sha) != 64:
        return None
    key = og_key(pub_sha, width)
    name = key.removeprefix("pub/")
    if not _NAME.match(name) or not (get_settings().media_pub / name).is_file():
        return None
    w, h = og_size(width, height)
    return _PUB_URL + name, w, h


def preview_image(
    pub_sha: str | None, width: int | None, height: int | None
) -> dict[str, Any] | None:
    """What a link preview or a feed enclosure shows of a picture: `{url, w, h, type}`.

    The og JPEG where it exists, else the largest webp (h is None when the row has no
    dimensions to scale it by), else None. Urls are site-relative.
    """
    og = og_image(pub_sha, width, height)
    if og is not None:
        return {"url": og[0], "w": og[1], "h": og[2], "type": "image/jpeg"}
    if not pub_sha:
        return None
    webps = [v for v in variants_for(pub_sha, width, height) if v["webp"]]
    if not webps:
        return None
    big = webps[-1]
    return {"url": big["webp"], "w": big["w"], "h": big.get("h"), "type": "image/webp"}


@router.api_route("/media/pub/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def pub(name: str) -> FileResponse:
    m = _NAME.match(name)
    if not m:
        raise HTTPException(status_code=404, detail="not here")
    path = get_settings().media_pub / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not here")
    return FileResponse(path, media_type=_TYPES[m.group(1)], headers={"Cache-Control": _IMMUTABLE})
