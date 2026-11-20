"""Uploads: every arrival at the inbox door, kept whether or not it became a picture.

A row is written before the image is decoded, so a file that never decodes still says
what it was and which collection it was meant for. Where an upload has got to is not
stored: `stages` reads it from the images row, the leaves and the data dirs every time,
so it cannot drift from them. First match wins: filed, unfiled, live, down, ingesting
(a processing folder or a waiting ingest job), inbox, failed (an error, a failed image or
a failed folder), ingesting (any other image row), received. Shared by the api (form
uploads, the admin's view) and the worker (rsync drops, failures).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from penish.collection.numerals import parse_line
from penish.config import Settings
from penish.worker.sidecars import stem_of

Conn = AsyncConnection[DictRow]

# Enough to say what went wrong on the admin page; a traceback-length reason is noise.
ERROR_MAX = 500
_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")


async def record(
    conn: Conn,
    *,
    stem: str,
    sha256: str,
    filename: str | None,
    size: int | None,
    collection_line: str | None,
    source: str,
) -> None:
    """Note an arrival. The form and the worker both see a form upload, in either order;
    the second never erases what the first knew, and the form's word on the filename and
    the source wins because only it saw the sender's file."""
    await conn.execute(
        """
        insert into uploads (stem, sha256, filename, bytes, collection_line, source)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (stem, sha256) do update set
          filename = case
            when excluded.source = 'form' and uploads.source <> 'form'
              then coalesce(excluded.filename, uploads.filename)
            else coalesce(uploads.filename, excluded.filename)
          end,
          bytes = coalesce(uploads.bytes, excluded.bytes),
          collection_line = coalesce(uploads.collection_line, excluded.collection_line),
          source = case when 'form' in (uploads.source, excluded.source)
                        then 'form' else excluded.source end,
          updated_at = now()
        """,
        (stem, sha256, filename, size, collection_line, source),
    )


async def fail(conn: Conn, *, stem: str, sha256: str, error: str) -> None:
    # The row should already be there from record; a failure is still worth keeping when
    # that write was lost, and only the worker gives up on a picture.
    await conn.execute(
        """
        insert into uploads (stem, sha256, source, error) values (%s, %s, 'rsync', %s)
        on conflict (stem, sha256) do update set error = excluded.error, updated_at = now()
        """,
        (stem, sha256, error[:ERROR_MAX]),
    )


async def clear(conn: Conn, *, stem: str, sha256: str) -> None:
    await conn.execute(
        "update uploads set error = null, updated_at = now()"
        " where stem = %s and sha256 = %s and error is not null",
        (stem, sha256),
    )


def collection_of(line: str | None) -> str | None:
    """The collection a sidecar line names. A line the filer will refuse still names one
    when its first word can be read, so the upload is not lost from that collection."""
    if not line:
        return None
    try:
        return parse_line(line).collection
    except ValueError:
        pass
    tokens = line.strip().lower().replace(",", " ").split()
    if tokens and _SLUG.fullmatch(tokens[0]):
        return tokens[0]
    return None


# -- where an upload has got to ----------------------------------------------------


@dataclass
class _Dirs:
    inbox: set[str] = field(default_factory=set)
    processing: set[str] = field(default_factory=set)
    # stem -> the newest failed folder holding it, and its mtime.
    failed: dict[str, tuple[float, Path]] = field(default_factory=dict)


def _stems(names: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for n in names:
        stem = stem_of(n)[0]
        if stem is not None:
            out.add(stem)
        elif n.endswith(".err") and len(n) > 4:
            out.add(n[:-4])
    return out


def _files(d: Path) -> list[str]:
    try:
        return [e.name for e in os.scandir(d) if e.is_file(follow_symlinks=False)]
    except OSError:
        # Moved or removed between the parent listing and this one.
        return []


def _subdirs(root: Path) -> list[os.DirEntry[str]]:
    try:
        return [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _scan(settings: Settings) -> _Dirs:
    out = _Dirs()
    out.inbox = _stems(_files(settings.inbox))
    for e in _subdirs(settings.processing):
        out.processing |= _stems(_files(Path(e.path)))
    for e in _subdirs(settings.failed):
        try:
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        # The folder is named for the stem (`x`, `x-2`, an orphan's `x-1a2b3c4d`), but the
        # files inside say it exactly, where a stem ending in `-2` would read ambiguously.
        for stem in _stems(_files(Path(e.path))) | {e.name}:
            prev = out.failed.get(stem)
            if prev is None or mtime > prev[0]:
                out.failed[stem] = (mtime, Path(e.path))
    return out


def failed_folder(settings: Settings, stem: str) -> Path | None:
    """The newest failed folder holding this stem's files. Unlike `_scan`, a folder's name
    alone does not count when its files say otherwise: `x-2` may hold an earlier `x`."""
    best: tuple[float, Path] | None = None
    for e in _subdirs(settings.failed):
        stems = _stems(_files(Path(e.path)))
        if stem not in stems and not (not stems and e.name == stem):
            continue
        try:
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, Path(e.path))
    return best[1] if best else None


def _err_texts(wanted: dict[str, Path]) -> dict[str, str | None]:
    return {stem: _err_text(folder, stem) for stem, folder in wanted.items()}


def _err_text(folder: Path, stem: str) -> str | None:
    named = folder / f"{stem}.err"
    candidates = [named] if named.is_file() else sorted(folder.glob("*.err"))
    for p in candidates:
        try:
            with p.open("rb") as f:
                text = f.read(ERROR_MAX * 4).decode("utf-8", errors="replace")
        except OSError:
            continue
        return text.strip()[:ERROR_MAX] or None
    return None


def _stage(
    row: dict[str, Any], wanted: str | None, leaves: set[str], dirs: _Dirs, jobs: set[str]
) -> str:
    status = row["status"]
    stem = row["stem"]
    line = row["collection_line"]
    # With no line, a leaf the owner filed by hand still counts; with one, only a leaf in
    # the collection it asked for does.
    if leaves and (wanted in leaves if wanted else not line):
        return "filed"
    if status == "live":
        return "unfiled" if line else "live"
    if status == "unpublished":
        return "down"
    # Work under way now outranks any failure: a retry puts the files back while the error
    # on the row and an older failed folder of the same stem are still there.
    if stem in dirs.processing or stem in jobs:
        return "ingesting"
    if stem in dirs.inbox:
        return "inbox"
    if row["error"] or status == "failed" or stem in dirs.failed:
        return "failed"
    if status is not None:
        return "ingesting"
    return "received"


async def stages(
    conn: Conn,
    settings: Settings,
    *,
    collection: str | None = None,
    only_unfiled: bool = False,
) -> list[dict[str, Any]]:
    """One entry per distinct upload, newest first. A retry of a failed folder comes back
    under a new stem with the same bytes; the newest row speaks for them."""
    cur = await conn.execute(
        """
        select * from (
          select distinct on (u.sha256)
                 u.id, u.stem, u.sha256, u.filename, u.bytes, u.collection_line, u.source,
                 u.error, u.created_at, i.id as image_id, i.slug, i.status::text as status
            from uploads u
            left join images i on i.sha256 = u.sha256
           order by u.sha256, u.created_at desc, u.id desc
        ) latest
        order by created_at desc, id desc
        """
    )
    rows: list[dict[str, Any]] = list(await cur.fetchall())
    named = [(r, collection_of(r["collection_line"])) for r in rows]
    if collection is not None:
        named = [(r, s) for r, s in named if s == collection]
    if not named:
        return []

    image_ids = [int(r["image_id"]) for r, _ in named if r["image_id"] is not None]
    leaves: dict[int, set[str]] = {}
    if image_ids:
        cur = await conn.execute(
            "select l.image_id, c.slug from collection_leaves l"
            " join collections c on c.id = l.collection_id where l.image_id = any(%s)",
            (image_ids,),
        )
        for r in await cur.fetchall():
            leaves.setdefault(int(r["image_id"]), set()).add(r["slug"])
    cur = await conn.execute(
        "select distinct payload->>'stem' as stem from jobs"
        " where type = 'ingest' and status in ('queued', 'running') and payload ? 'stem'"
    )
    jobs = {r["stem"] for r in await cur.fetchall() if r["stem"]}
    dirs = await asyncio.to_thread(_scan, settings)

    staged: list[tuple[dict[str, Any], str]] = []
    for r, wanted in named:
        held = leaves.get(int(r["image_id"])) if r["image_id"] is not None else None
        stage = _stage(r, wanted, held or set(), dirs, jobs)
        if not (only_unfiled and stage == "filed"):
            staged.append((r, stage))
    need = {
        r["stem"]: dirs.failed[r["stem"]][1]
        for r, stage in staged
        if stage == "failed" and r["error"] is None and r["stem"] in dirs.failed
    }
    errs = await asyncio.to_thread(_err_texts, need) if need else {}

    out: list[dict[str, Any]] = []
    for r, stage in staged:
        out.append(
            {
                "stem": r["stem"],
                "sha256": r["sha256"],
                "filename": r["filename"],
                "bytes": r["bytes"],
                "collection_line": r["collection_line"],
                "source": r["source"],
                "stage": stage,
                "error": r["error"] if r["error"] is not None else errs.get(r["stem"]),
                "image_id": r["image_id"],
                "slug": r["slug"],
                "created_at": r["created_at"],
            }
        )
    return out
