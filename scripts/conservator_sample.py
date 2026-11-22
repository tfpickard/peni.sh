"""Write conservator notes for a few live leaves, without storing them anywhere.

Usage: uv run python scripts/conservator_sample.py [slug ...] [--base https://peni.sh]

Reads each leaf's place and earlier place from the public api, its 960px picture, and the
collection's record (from the public api once it carries one, else from the local
database), then runs the same call the collection_note job makes. Leaves of one book are
written in leaf order, each told the notes before it, as the job would be. Needs
ANTHROPIC_API_KEY. Model and prompt are the worker's: the runtime settings in the database,
overrides from /admin/models included; with no database reachable, the defaults, and it
says so.
"""

import argparse
import asyncio
import json
import sys
import urllib.request
from typing import Any

import psycopg

from penish.collection.store import NoteContext, record_pairs
from penish.config import get_settings
from penish.db import make_pool
from penish.worker.conservator import write

DEFAULT = (
    "furniture-being-moved-in-a-row",
    "plate-one-with-one-blank-panel",
    "meal-service-at-a-table-inside-the-initial",
)
UA = {"User-Agent": "penish-conservator-sample/1 (read-only)"}


def get(base: str, path: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(base + path, headers=UA), timeout=30) as r:
        data: bytes = r.read()
        return data


def record(base: str, slug: str) -> list[tuple[str, str]]:
    try:
        c = json.loads(get(base, f"/api/collections/{slug}"))
        if c.get("record"):
            return record_pairs(c["record"])
    except OSError:
        pass
    # Not deployed yet: the seed as the local database holds it.
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=5) as conn:
            row = conn.execute("select record from collections where slug = %s", (slug,)).fetchone()
    except psycopg.OperationalError:
        sys.exit(f"no record for {slug}: the live api has none yet and no database is reachable")
    if row is None:
        sys.exit(f"no record for {slug}")
    return record_pairs(row[0])


async def open_pool() -> Any:
    """The database the worker reads its settings from, or None when there is none here."""
    pool = make_pool(get_settings().database_url)
    try:
        await pool.open(wait=True, timeout=5)
    except Exception as e:
        await pool.close()
        print(
            f"no database ({e.__class__.__name__}); using default model and prompt", file=sys.stderr
        )
        return None
    return pool


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    pool = await open_pool()
    try:
        return await notes(args, settings, pool)
    finally:
        if pool is not None:
            await pool.close()


async def notes(args: argparse.Namespace, settings: Any, pool: Any) -> int:
    leaves: list[tuple[dict[str, Any], bytes]] = []
    for slug in args.slugs:
        d = json.loads(get(args.base, f"/api/images/{slug}"))
        if not d.get("leaf"):
            print(f"{slug}: not a leaf", file=sys.stderr)
            continue
        v = min(d["variants"], key=lambda x: abs(x["w"] - 960))
        leaves.append((d, get(args.base, v["webp"])))
    if not leaves:
        return 1
    rec = record(args.base, leaves[0][0]["leaf"]["collection"]["slug"])
    leaves.sort(key=lambda x: (x[0]["leaf"]["book"], x[0]["leaf"]["position"]))
    written: dict[int, list[tuple[int, str]]] = {}
    for d, pixels in leaves:
        leaf = d["leaf"]
        prev = leaf.get("previous")
        ctx = NoteContext(
            sha256="",
            crop=None,
            record=rec,
            book=leaf["book"],
            position=leaf["position"],
            leaves_per_book=77,
            loose_title=leaf["collection"].get("loose_title"),
            note=None,
            note_source=None,
            previous=(prev["book"], prev["position"]) if prev else None,
            book_notes=list(written.get(leaf["book"], [])),
        )
        note = await write(pixels, "image/webp", ctx, settings=settings, pool=pool)
        written.setdefault(leaf["book"], []).append((leaf["position"], note.text))
        print(f"{d['slug']} (book {leaf['book']}, leaf {leaf['position']}, {note.model})")
        print(f"  {note.text}\n")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*", default=list(DEFAULT))
    ap.add_argument("--base", default="https://peni.sh")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
