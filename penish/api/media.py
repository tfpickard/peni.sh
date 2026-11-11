import os
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from penish.config import get_settings

router = APIRouter()

_IMMUTABLE = "public, max-age=31536000, immutable"
# Strict on purpose: the name is the only thing between a request and the filesystem.
_NAME = re.compile(r"^[0-9a-f]{64}-\d+\.(avif|webp)$")
_TYPES = {"avif": "image/avif", "webp": "image/webp"}
_PUB_URL = "/media/pub/"


def _scan(shas: Iterable[str]) -> dict[str, dict[int, dict[str, str]]]:
    """One directory scan for any number of shas: sha -> width -> format -> filename."""
    wanted = set(shas)
    found: dict[str, dict[int, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    try:
        it = os.scandir(get_settings().media_pub)
    except FileNotFoundError:
        return {}
    with it:
        for entry in it:
            name = entry.name
            if not _NAME.match(name) or name[:64] not in wanted:
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
    """sha -> variants for a batch, so a page of neighbors costs one directory scan."""
    dims = {sha: (w, h) for sha, w, h in images}
    found = _scan(dims)
    return {sha: _shape(found.get(sha, {}), *dims[sha]) for sha in dims}


def variants_for(
    sha: str, width: int | None = None, height: int | None = None
) -> list[dict[str, Any]]:
    """Widths that exist on disk for one image, smallest first.

    Listing the directory keeps the API honest about small sources that only have
    a native width. If the scan gets slow, swap this for a variants column.
    """
    return variants_for_many([(sha, width, height)])[sha]


@router.api_route("/media/pub/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def pub(name: str) -> FileResponse:
    m = _NAME.match(name)
    if not m:
        raise HTTPException(status_code=404, detail="not here")
    path = get_settings().media_pub / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not here")
    return FileResponse(path, media_type=_TYPES[m.group(1)], headers={"Cache-Control": _IMMUTABLE})
