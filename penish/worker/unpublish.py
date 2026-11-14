"""Unpublish and republish: a reversible status for a published picture.

`/media/pub/` is served by filename with no database lookup, so a status alone hides
nothing: the derivatives move to a private holding dir beside pub. The api mounts pub and
orig only and cannot do that move, so both directions are worker jobs the admin enqueues.

Both run under the per-sha lock ingest, crop and delete take, so a re-drop or a crop of
the same picture never sees the status and the files disagree. Both are idempotent: a
rerun after a crash anywhere finishes what the first run started.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.mycelium import facets
from penish.mycelium import store as mycelium
from penish.worker import facet_jobs
from penish.worker.ingest import KIND, Conn, _webp_keys, record_served
from penish.worker.jobs import HardError, Job, Pool
from penish.worker.metrics import refresh_corpus_stats

log = logging.getLogger(__name__)

UNPUBLISH = "unpublish"
REPUBLISH = "republish"
JOBS = (UNPUBLISH, REPUBLISH)
# The api's delete cannot reach the holding dir, so it hands the files to the worker.
SETTLE = "settle_media"

Where = Literal["pub", "held", "gone"]
Spaces = list[tuple[str, str]]
_SHA = re.compile(r"^[0-9a-f]{64}$")


# -- files ------------------------------------------------------------------


def _files(d: Path, pub_sha: str) -> list[Path]:
    if not d.is_dir():
        return []
    # pub_sha is a hex digest the code wrote, never request data, so it is safe in a glob.
    # Storage's temp files start with a dot and never match.
    return sorted(p for p in d.glob(f"{pub_sha}-*") if p.is_file())


def _move(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        # Moved already by an earlier run that died before finishing the rest.
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # Only if someone mounts the two dirs apart; copy then rename keeps a reader
        # from ever seeing half a file.
        tmp = dst.with_name(f".{dst.name}.tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        src.unlink(missing_ok=True)


def _hide(settings: Settings, pub_sha: str) -> int:
    settings.media_held.mkdir(parents=True, exist_ok=True)
    moved = _files(settings.media_pub, pub_sha)
    for p in moved:
        _move(p, settings.media_held / p.name)
    return len(moved)


def _show(settings: Settings, pub_sha: str) -> int:
    settings.media_pub.mkdir(parents=True, exist_ok=True)
    moved = _files(settings.media_held, pub_sha)
    for p in moved:
        _move(p, settings.media_pub / p.name)
    return len(moved)


def _drop(settings: Settings, pub_sha: str) -> int:
    gone = [*_files(settings.media_pub, pub_sha), *_files(settings.media_held, pub_sha)]
    for p in gone:
        p.unlink(missing_ok=True)
    return len(gone)


_PLACE: dict[Where, Callable[[Settings, str], int]] = {
    "pub": _show,
    "held": _hide,
    "gone": _drop,
}


@contextlib.asynccontextmanager
async def _pub_lock(conn: Conn, pub_sha: str) -> AsyncIterator[None]:
    # Two pictures can share a pub_sha (identical crops) under different sha locks; this
    # one keeps one's republish from interleaving with the other's unpublish. Always
    # taken inside a sha lock or alone, and never two at once, so it cannot deadlock.
    key = f"pub:{pub_sha}"
    await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (key,))
    try:
        yield
    finally:
        with contextlib.suppress(psycopg.Error):
            await conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


async def _where(conn: Conn, pub_sha: str) -> Where:
    cur = await conn.execute(
        """
        select count(*)::int as users,
               count(*) filter (where status <> 'unpublished')::int as shown
          from images where pub_sha = %s
        """,
        (pub_sha,),
    )
    row = await cur.fetchone()
    if not row or not row["users"]:
        return "gone"
    # A picture still in ingest counts as shown: its publish gate looks for the files in pub.
    return "pub" if row["shown"] else "held"


async def settle(conn: Conn, settings: Settings, pub_sha: str) -> Where:
    """Put pub_sha's derivatives where the pictures using them need them: pub while any
    of them is not unpublished, held while all of them are, nowhere once none is left.

    The one rule every writer of these files follows, so each call is idempotent and a
    rerun after a crash mid-move finishes the move. `conn` should be in autocommit, so
    no transaction stays open across the file moves.
    """
    async with _pub_lock(conn, pub_sha):
        where = await _where(conn, pub_sha)
        n = await asyncio.to_thread(_PLACE[where], settings, pub_sha)
    if n:
        log.info("media %s: %d files to %s", pub_sha[:10], n, where)
    return where


def is_held(settings: Settings, name: str) -> bool:
    return (settings.media_held / name).is_file()


# -- jobs -------------------------------------------------------------------


def _image_id(job: Job) -> int:
    raw = job.payload.get("image_id")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HardError(f"bad {job.type} payload: image_id must be an integer")
    return raw


async def _row(conn: Conn, image_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "select id, sha256, pub_sha, width, status::text as status from images where id = %s",
        (image_id,),
    )
    row: dict[str, Any] | None = await cur.fetchone()
    return row


async def _superseded(conn: Conn, job: Job, image_id: int) -> bool:
    # The newest press wins, in either direction. The admin withdraws a queued press the
    # other way, but two workers can still hold one of each at once.
    cur = await conn.execute(
        "select exists (select 1 from jobs where type = any(%s) and id > %s"
        " and status <> 'failed' and payload->>'image_id' = %s) as s",
        (list(JOBS), job.id, str(image_id)),
    )
    row = await cur.fetchone()
    return bool(row and row["s"])


async def _spaces(conn: Conn, image_id: int) -> Spaces:
    cur = await conn.execute(
        "select distinct kind, model from embeddings where image_id = %s", (image_id,)
    )
    return sorted((str(r["kind"]), str(r["model"])) for r in await cur.fetchall())


async def _refresh(pool: Pool, spaces: Spaces) -> None:
    """Every embedding space the picture has a vector in, since the live set of each
    just changed. A description refresh already carries the facet kinds of its model."""
    described = {m for k, m in spaces if k == KIND}
    for kind, model in spaces:
        if facets.is_kind(kind) and model in described:
            continue
        await refresh_corpus_stats(pool, kind=kind, model=model)


async def _locked(
    pool: Pool,
    job: Job,
    settings: Settings,
    body: Callable[[Conn, Job, int, Settings], Awaitable[Spaces | None]],
) -> None:
    image_id = _image_id(job)
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        row = await _row(conn, image_id)
        if row is None:
            log.info("%s: image %s is gone; nothing to do", job.type, image_id)
            await conn.set_autocommit(False)
            return
        sha = row["sha256"]
        # The ingest lock: a re-drop, crop or delete of the same picture waits for this.
        await conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (sha,))
        try:
            spaces = await body(conn, job, image_id, settings)
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (sha,))
                await conn.set_autocommit(False)
    # After the lock: the refresh is its own transactions and takes the state row last.
    if spaces is not None:
        await _refresh(pool, spaces)


async def _unpublish_locked(
    conn: Conn, job: Job, image_id: int, settings: Settings
) -> Spaces | None:
    if await _superseded(conn, job, image_id):
        log.info("unpublish job %s for image %s superseded", job.id, image_id)
        return None
    row = await _row(conn, image_id)
    if row is None or row["status"] not in ("live", "unpublished"):
        log.info("unpublish: image %s is not published; nothing to do", image_id)
        return None
    if row["status"] == "live":
        # Status before files: every public route reads the status, so the picture is
        # gone from the site the moment this commits, and a crash before the move only
        # leaves hash-named files a rerun moves.
        async with conn.transaction():
            # First, as the tick takes the state row before the image rows it key-shares.
            await mycelium.mark_dirty(conn)
            cur = await conn.execute(
                "update images set status = 'unpublished', updated_at = now()"
                " where id = %s and status = 'live' returning pub_sha, width",
                (image_id,),
            )
            went = await cur.fetchone()
            if went is not None:
                # With the flip, so a crop or heal while it is down cannot lose the
                # URLs Cloudflare is still holding.
                await record_served(conn, image_id, went["pub_sha"], went["width"])
        log.info("image %s unpublished", image_id)
    await settle(conn, settings, row["pub_sha"])
    return await _spaces(conn, image_id)


async def _republish_locked(
    conn: Conn, job: Job, image_id: int, settings: Settings
) -> Spaces | None:
    if await _superseded(conn, job, image_id):
        log.info("republish job %s for image %s superseded", job.id, image_id)
        return None
    row = await _row(conn, image_id)
    if row is None or row["status"] not in ("live", "unpublished"):
        log.info("republish: image %s is not published; nothing to do", image_id)
        return None
    pub_sha = str(row["pub_sha"])
    names = [k.split("/", 1)[1] for k in _webp_keys(pub_sha, int(row["width"] or 0))]
    async with _pub_lock(conn, pub_sha):
        have = await asyncio.to_thread(
            lambda: all((settings.media_pub / n).is_file() or is_held(settings, n) for n in names)
        )
        if not have:
            # Checked before anything moves, so a failure leaves the picture as it was.
            # A re-drop of the file heals the derivatives into the holding dir.
            raise HardError("media missing; drop the file again")
        # Files before status, so the page never goes live without its pictures.
        n = await asyncio.to_thread(_show, settings, pub_sha)
        if n:
            log.info("media %s: %d files to pub", pub_sha[:10], n)
        if row["status"] == "unpublished":
            async with conn.transaction():
                await mycelium.mark_dirty(conn)
                await conn.execute(
                    "update images set status = 'live', updated_at = now()"
                    " where id = %s and status = 'unpublished'",
                    (image_id,),
                )
                # Back as just seen, as at go-live, not as neglected for the whole time
                # it was down.
                await conn.execute(
                    "insert into picture_attention (image_id, last_viewed_at)"
                    " values (%s, now()) on conflict (image_id)"
                    " do update set last_viewed_at = now()",
                    (image_id,),
                )
                # Facet jobs skip a picture that is not live, so anything invalidated
                # while it was down (a crop, an analysis edit) was never redone. Each
                # returns at once when its recipe is already complete.
                for kind in (facet_jobs.EXTRACT_JOB, facet_jobs.SPLIT_JOB):
                    await conn.execute(
                        "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)"
                        " on conflict do nothing",
                        (kind, Jsonb({"image_id": image_id}), f"{kind}:{image_id}"),
                    )
                await conn.execute(
                    """
                    insert into jobs (type, payload, dedupe_key)
                    select 'categorize', %s, %s
                     where not exists (select 1 from image_categories where image_id = %s)
                    on conflict do nothing
                    """,
                    (Jsonb({"image_id": image_id}), f"categorize:{image_id}", image_id),
                )
            log.info("image %s republished", image_id)
    return await _spaces(conn, image_id)


async def handle_unpublish(job: Job, *, pool: Pool, settings: Settings) -> None:
    await _locked(pool, job, settings, _unpublish_locked)


async def handle_republish(job: Job, *, pool: Pool, settings: Settings) -> None:
    await _locked(pool, job, settings, _republish_locked)


async def handle_settle(job: Job, *, pool: Pool, settings: Settings) -> None:
    pub_sha = job.payload.get("pub_sha")
    if not isinstance(pub_sha, str) or not _SHA.fullmatch(pub_sha):
        raise HardError("bad settle_media payload: pub_sha must be a hex digest")
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            await settle(conn, settings, pub_sha)
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.set_autocommit(False)
