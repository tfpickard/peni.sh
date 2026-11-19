"""Collections in the database: requests, filing, moves and re-sorts.

Shared by the worker (filing and the weekly re-sort) and the admin api (moves, pins and
the re-sort button), so every write to a collection's leaves goes through here, under
one advisory lock per collection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from penish import runtime
from penish.collections import roman
from penish.collections.filing import (
    BookSpace,
    Leaf,
    Params,
    choose_book,
    positions,
    resort_books,
    unit,
)
from penish.config import Settings

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

# The embedding space pictures live in; see ingest.KIND for why it is not "analysis".
KIND = "description"
FILE_JOB = "collection_file"
ANCHOR_JOB = "collection_anchor"
RESORT_JOB = "collection_resort"


class FilingError(Exception):
    """The owner asked for something the collection cannot do: a full book, a taken leaf,
    a book that does not exist. Retrying will not help until the owner changes something."""


class NotReady(Exception):
    """The picture is not live or has no vector yet; filing waits for it."""


@dataclass(frozen=True)
class Collection:
    id: int
    slug: str
    title: str
    abbrev: str
    abbrev_latin: str
    preamble: str
    book_count: int
    leaves_per_book: int
    published: bool
    resorted_at: datetime | None


Pos = tuple[int, int]  # (book number, leaf)


def _coll(row: dict[str, Any]) -> Collection:
    return Collection(**{k: row[k] for k in Collection.__dataclass_fields__})


async def get(conn: Conn, *, slug: str | None = None, cid: int | None = None) -> Collection | None:
    if slug is not None:
        cur = await conn.execute("select * from collections where slug = %s", (slug,))
    else:
        cur = await conn.execute("select * from collections where id = %s", (cid,))
    row = await cur.fetchone()
    return None if row is None else _coll(row)


async def all_collections(conn: Conn) -> list[Collection]:
    cur = await conn.execute("select * from collections order by id")
    return [_coll(r) for r in await cur.fetchall()]


async def load_params(pool: Pool | None, settings: Settings | None = None) -> Params:
    g = runtime.getter(pool, settings)
    return Params(
        k=float(await g("collections.k")),
        alpha=float(await g("collections.alpha")),
        beta=float(await g("collections.beta")),
        margin=float(await g("collections.margin")),
    )


def check_numbers(c: Collection, book: int | None, position: int | None) -> None:
    """ValueError when a requested book or leaf does not exist in this collection."""
    if position is not None and book is None:
        raise ValueError("a leaf needs a book")
    if book is not None and not 1 <= book <= c.book_count:
        raise ValueError(f"no book {roman(book)}")
    if position is not None and not 1 <= position <= c.leaves_per_book:
        raise ValueError(f"no leaf {position}")


async def lock(conn: Conn, c: Collection) -> None:
    # Transaction-scoped, so a crash or rollback can never leave it held.
    await conn.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"collection:{c.id}",)
    )


# -- requests ---------------------------------------------------------------


async def request(
    conn: Conn, c: Collection, image_id: int, book: int | None, position: int | None
) -> None:
    """Mark a picture for the collection. Validated here so a bad one never waits."""
    check_numbers(c, book, position)
    await conn.execute(
        """
        insert into collection_requests (image_id, collection_id, book, position)
        values (%s, %s, %s, %s)
        on conflict (image_id, collection_id) do update set
          book = excluded.book, position = excluded.position, error = null, updated_at = now()
        """,
        (image_id, c.id, book, position),
    )


# One filing job per request of a live picture that has not failed. Idempotent through
# the dedupe key, so sweeping again is harmless.
_ENQUEUE = """
    insert into jobs (type, payload, dedupe_key)
    select %(t)s,
           jsonb_build_object('image_id', r.image_id, 'collection_id', r.collection_id),
           %(t)s || ':' || r.image_id || ':' || r.collection_id
      from collection_requests r join images i on i.id = r.image_id
     where i.status = 'live' and r.error is null {only}
    on conflict do nothing
"""


async def enqueue_for_image(conn: Conn, image_id: int) -> int:
    cur = await conn.execute(
        _ENQUEUE.format(only="and r.image_id = %(id)s").encode(), {"t": FILE_JOB, "id": image_id}
    )
    return cur.rowcount


async def enqueue_pending(pool: Pool) -> int:
    """Start-of-worker sweep: requests of live pictures with no job, e.g. after a crash
    between publish and the enqueue."""
    async with pool.connection() as conn:
        cur = await conn.execute(_ENQUEUE.format(only="").encode(), {"t": FILE_JOB})
        return cur.rowcount


async def note_bad_sidecar(conn: Conn, image_id: int, error: str) -> None:
    """A malformed collection sidecar shows up as a failed filing job on the queue page.
    The picture is unaffected and nothing is requested. A retried ingest writes it once."""
    await conn.execute(
        """
        insert into jobs (type, payload, status, attempts, last_error)
        select %(t)s, %(p)s, 'failed', 1, %(e)s
         where not exists (
           select 1 from jobs where type = %(t)s and status = 'failed'
              and payload->>'image_id' = %(id)s::text and last_error = %(e)s)
        """,
        {
            "t": FILE_JOB,
            "p": Jsonb({"image_id": image_id, "error": error}),
            "e": error,
            "id": image_id,
        },
    )


async def set_error(conn: Conn, c: Collection, image_id: int, error: str | None) -> None:
    await conn.execute(
        "update collection_requests set error = %s, updated_at = now()"
        " where image_id = %s and collection_id = %s",
        (error, image_id, c.id),
    )


# -- state ------------------------------------------------------------------


async def space_mean(conn: Conn, settings: Settings) -> np.ndarray | None:
    """The corpus mean to subtract, or None while the site still compares raw vectors."""
    cur = await conn.execute(
        """
        select m.mean,
               (select count(*) from embeddings e join images i on i.id = e.image_id
                 where e.kind = %(k)s and e.model = %(m)s and i.status = 'live') as n
          from corpus_stats m where m.kind = %(k)s and m.model = %(m)s
        """,
        {"k": KIND, "m": settings.embed_model},
    )
    row = await cur.fetchone()
    if row is None or row["mean"] is None or int(row["n"]) < settings.centered_min_images:
        return None
    mean = _array(row["mean"])
    return mean if mean.shape == (settings.embed_dim,) else None


def _array(v: Any) -> np.ndarray:
    # pgvector hands back its own Vector type for the untyped column.
    return np.asarray(v.to_numpy() if hasattr(v, "to_numpy") else v, dtype=np.float32)


def prep(v: Any, mean: np.ndarray | None) -> np.ndarray | None:
    a = _array(v)
    if mean is not None and a.shape == mean.shape:
        a = a - mean
    return unit(a)


async def vectors(
    conn: Conn, ids: list[int], settings: Settings, mean: np.ndarray | None
) -> dict[int, np.ndarray]:
    if not ids:
        return {}
    cur = await conn.execute(
        "select image_id, vec from embeddings where image_id = any(%s) and kind = %s"
        " and model = %s and dim = %s",
        (ids, KIND, settings.embed_model, settings.embed_dim),
    )
    out: dict[int, np.ndarray] = {}
    for r in await cur.fetchall():
        v = prep(r["vec"], mean)
        if v is not None:
            out[int(r["image_id"])] = v
    return out


async def tags_for(conn: Conn, ids: list[int]) -> dict[int, frozenset[str]]:
    if not ids:
        return {}
    cur = await conn.execute(
        "select image_id, array_agg(tag) as tags from tags where image_id = any(%s)"
        " group by image_id",
        (ids,),
    )
    return {int(r["image_id"]): frozenset(r["tags"]) for r in await cur.fetchall()}


async def books(conn: Conn, c: Collection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "select * from collection_books where collection_id = %s order by number", (c.id,)
    )
    return list(await cur.fetchall())


@dataclass
class State:
    """One collection loaded for the filer. Leaves without a usable vector (a model swap
    halfway through re-embedding) keep their slot and are left out of the arithmetic."""

    c: Collection
    book_ids: dict[int, int]  # number -> id
    spaces: list[BookSpace]
    anchors: dict[int, np.ndarray | None]  # number -> prepped anchor
    leaves: dict[int, Leaf]  # image_id -> leaf, vector or not
    has_vec: set[int]
    before: dict[int, Pos]

    def in_book(self, number: int) -> list[Leaf]:
        return sorted(
            (lf for lf in self.leaves.values() if lf.book == number), key=lambda lf: lf.image_id
        )

    def taken(self, number: int) -> int:
        return sum(1 for lf in self.leaves.values() if lf.book == number)

    def refresh_spaces(self) -> None:
        by_number = {b.number: b for b in self.spaces}
        for b in self.spaces:
            b.total, b.n = None, 0
        for lf in self.leaves.values():
            b = by_number[lf.book]
            if lf.image_id in self.has_vec:
                b.add(lf.vec)
            else:
                # Counted toward fullness, but with no direction to pull the centroid.
                b.n += 1
                if b.total is None:
                    b.total = np.zeros_like(lf.vec)


async def load_state(conn: Conn, c: Collection, settings: Settings) -> State:
    mean = await space_mean(conn, settings)
    rows = await books(conn, c)
    book_ids = {int(r["number"]): int(r["id"]) for r in rows}
    number_of = {v: k for k, v in book_ids.items()}
    anchors: dict[int, np.ndarray | None] = {}
    spaces: list[BookSpace] = []
    for r in rows:
        a = None
        if r["anchor_vec"] is not None and r["anchor_model"] == settings.embed_model:
            a = prep(r["anchor_vec"], mean)
        anchors[int(r["number"])] = a
        spaces.append(
            BookSpace(number=int(r["number"]), anchor=a, hints=frozenset(r["tag_hints"] or ()))
        )
    cur = await conn.execute(
        "select image_id, book_id, position, pinned_book, pinned_position"
        " from collection_leaves where collection_id = %s",
        (c.id,),
    )
    raw = list(await cur.fetchall())
    ids = [int(r["image_id"]) for r in raw]
    vecs = await vectors(conn, ids, settings, mean)
    tags = await tags_for(conn, ids)
    zero = np.zeros(settings.embed_dim, dtype=np.float32)
    leaves: dict[int, Leaf] = {}
    for r in raw:
        iid = int(r["image_id"])
        leaves[iid] = Leaf(
            image_id=iid,
            vec=vecs.get(iid, zero),
            book=number_of[int(r["book_id"])],
            position=int(r["position"]),
            tags=tags.get(iid, frozenset()),
            pinned_book=bool(r["pinned_book"]),
            pinned_position=bool(r["pinned_position"]),
        )
    st = State(
        c=c,
        book_ids=book_ids,
        spaces=spaces,
        anchors=anchors,
        leaves=leaves,
        has_vec=set(vecs),
        before={iid: (lf.book, lf.position) for iid, lf in leaves.items()},
    )
    st.refresh_spaces()
    return st


def rechain(st: State, numbers: set[int], hold: dict[int, int] | None = None) -> None:
    """Recompute leaf positions in the given books, in place on st.leaves.

    Fixed points: owner-pinned leaves, leaves with no vector, and `hold` ({image_id:
    slot}) for a leaf being placed by the owner right now.
    """
    hold = hold or {}
    cap = st.c.leaves_per_book
    for number in sorted(numbers):
        ls = st.in_book(number)
        if not ls:
            continue
        fixed: list[int | None] = []
        for lf in ls:
            if lf.image_id in hold:
                fixed.append(hold[lf.image_id])
            elif lf.pinned_position or lf.image_id not in st.has_vec:
                fixed.append(lf.position)
            else:
                fixed.append(None)
        # A leaf with no vector still needs one for the distance matrix; its slot is
        # fixed, so the stand-in only nudges which run its neighbours join.
        vecs = [lf.vec if lf.image_id in st.has_vec else _stand_in(lf.vec) for lf in ls]
        got = positions(vecs, fixed, cap, st.anchors.get(number))
        for lf, s in zip(ls, got, strict=True):
            lf.position = s


def _stand_in(v: np.ndarray) -> np.ndarray:
    out = np.zeros_like(v)
    out[0] = 1.0
    return out


async def commit(
    conn: Conn, st: State, reason: str, reasons: dict[int, str] | None = None
) -> list[dict[str, Any]]:
    """Write every leaf whose (book, leaf) changed, with a history row for each move.
    Leaves new to st.before are placed without one: filing is not a move."""
    reasons = reasons or {}
    moves: list[dict[str, Any]] = []
    for iid in sorted(st.leaves):
        lf = st.leaves[iid]
        now = (lf.book, lf.position)
        was = st.before.get(iid)
        if was == now:
            continue
        await conn.execute(
            "update collection_leaves set book_id = %s, position = %s, pinned_book = %s,"
            " pinned_position = %s, updated_at = now() where image_id = %s and collection_id = %s",
            (st.book_ids[lf.book], lf.position, lf.pinned_book, lf.pinned_position, iid, st.c.id),
        )
        if was is not None:
            why = reasons.get(iid, reason)
            await conn.execute(
                "insert into collection_leaf_history (image_id, collection_id, from_book,"
                " from_position, to_book, to_position, reason) values (%s, %s, %s, %s, %s, %s, %s)",
                (iid, st.c.id, was[0], was[1], now[0], now[1], why),
            )
            moves.append({"image_id": iid, "from": was, "to": now, "reason": why})
    return moves


def _free_slot(st: State, number: int) -> int:
    used = {lf.position for lf in st.leaves.values() if lf.book == number}
    return next(s for s in range(1, st.c.leaves_per_book + 1) if s not in used)


def _check_room(st: State, number: int, image_id: int, position: int | None) -> None:
    # The leaf itself does not count against room in the book it is already in.
    others = sum(1 for lf in st.leaves.values() if lf.book == number and lf.image_id != image_id)
    if others >= st.c.leaves_per_book:
        raise FilingError(f"book {roman(number)} is full")
    if position is not None:
        for lf in st.leaves.values():
            if (
                lf.image_id != image_id
                and lf.book == number
                and lf.position == position
                and (lf.pinned_position or lf.image_id not in st.has_vec)
            ):
                raise FilingError(f"book {roman(number)}, leaf {position} is taken")


# -- operations ---------------------------------------------------------------


async def file_one(
    conn: Conn, c: Collection, image_id: int, settings: Settings, p: Params
) -> Pos | None:
    """Act on one request inside the caller's transaction. Returns where the leaf is, or
    None when there was no request to act on."""
    await lock(conn, c)
    cur = await conn.execute(
        "select r.book, r.position, i.status::text as status from collection_requests r"
        " join images i on i.id = r.image_id where r.image_id = %s and r.collection_id = %s",
        (image_id, c.id),
    )
    req = await cur.fetchone()
    if req is None:
        return None
    if req["status"] != "live":
        raise NotReady(f"picture {image_id} is {req['status']}")
    book, position = req["book"], req["position"]
    try:
        check_numbers(c, book, position)
    except ValueError as e:
        raise FilingError(str(e)) from e

    st = await load_state(conn, c, settings)
    if image_id in st.leaves:
        # Already a leaf: a request with no book has nothing left to do; one with a book
        # is the owner placing it.
        if book is not None:
            await _move(conn, st, image_id, book, position)
        await _drop_request(conn, c, image_id)
        lf = st.leaves[image_id]
        return (lf.book, lf.position)

    vecs = await vectors(conn, [image_id], settings, await space_mean(conn, settings))
    vec = vecs.get(image_id)
    if vec is None:
        raise NotReady(f"picture {image_id} has no vector")
    tags = (await tags_for(conn, [image_id])).get(image_id, frozenset())

    if book is None:
        book = choose_book(vec, tags, st.spaces, c.leaves_per_book, p)
        if book is None:
            if all(st.taken(b.number) >= c.leaves_per_book for b in st.spaces):
                raise FilingError("every book is full")
            raise FilingError("no book has an anchor or a leaf to file against")
    _check_room(st, book, image_id, position)

    pinned_book = req["book"] is not None
    pinned_position = position is not None
    placeholder = position if position is not None else _free_slot(st, book)
    await conn.execute(
        "insert into collection_leaves (image_id, collection_id, book_id, position,"
        " pinned_book, pinned_position) values (%s, %s, %s, %s, %s, %s)",
        (image_id, c.id, st.book_ids[book], placeholder, pinned_book, pinned_position),
    )
    st.leaves[image_id] = Leaf(
        image_id=image_id,
        vec=vec,
        book=book,
        position=placeholder,
        tags=tags,
        pinned_book=pinned_book,
        pinned_position=pinned_position,
    )
    st.has_vec.add(image_id)
    rechain(st, {book})
    await commit(conn, st, "auto")
    await _drop_request(conn, c, image_id)
    lf = st.leaves[image_id]
    log.info("filed picture %s in %s %s, leaf %s", image_id, c.slug, roman(lf.book), lf.position)
    return (lf.book, lf.position)


async def _drop_request(conn: Conn, c: Collection, image_id: int) -> None:
    await conn.execute(
        "delete from collection_requests where image_id = %s and collection_id = %s",
        (image_id, c.id),
    )


async def _move(
    conn: Conn, st: State, image_id: int, book: int, position: int | None
) -> list[dict[str, Any]]:
    lf = st.leaves[image_id]
    _check_room(st, book, image_id, position)
    old = lf.book
    lf.book = book
    lf.pinned_book = True
    lf.pinned_position = position is not None
    if position is not None:
        lf.position = position
        hold = {image_id: position}
    else:
        if old != book:
            lf.position = _free_slot(st, book)
        hold = {}
    rechain(st, {old, book}, hold)
    return await commit(conn, st, "auto", {image_id: "owner"})


async def move(
    conn: Conn, c: Collection, image_id: int, book: int, position: int | None, settings: Settings
) -> list[dict[str, Any]]:
    """The owner places a filed leaf: it becomes pinned to the book, and to the leaf when
    one is given. Raises FilingError, or LookupError when it is not a leaf."""
    try:
        check_numbers(c, book, position)
    except ValueError as e:
        raise FilingError(str(e)) from e
    await lock(conn, c)
    st = await load_state(conn, c, settings)
    if image_id not in st.leaves:
        raise LookupError(image_id)
    return await _move(conn, st, image_id, book, position)


async def pin(conn: Conn, c: Collection, image_id: int, pinned: bool) -> bool:
    """Pin a leaf where it is, or free it for the filer. Moves nothing."""
    await lock(conn, c)
    cur = await conn.execute(
        "update collection_leaves set pinned_book = %s, pinned_position = %s, updated_at = now()"
        " where image_id = %s and collection_id = %s",
        (pinned, pinned, image_id, c.id),
    )
    return cur.rowcount > 0


async def unfile(conn: Conn, c: Collection, image_id: int) -> bool:
    """Out of the collection; the picture stays on the site. True if anything was there."""
    await lock(conn, c)
    cur = await conn.execute(
        "delete from collection_leaves l using collection_books b"
        " where l.image_id = %s and l.collection_id = %s and b.id = l.book_id"
        " returning b.number, l.position",
        (image_id, c.id),
    )
    row = await cur.fetchone()
    if row is not None:
        await conn.execute(
            "insert into collection_leaf_history (image_id, collection_id, from_book,"
            " from_position, reason) values (%s, %s, %s, %s, 'owner')",
            (image_id, c.id, row["number"], row["position"]),
        )
    cur = await conn.execute(
        "delete from collection_requests where image_id = %s and collection_id = %s",
        (image_id, c.id),
    )
    return row is not None or cur.rowcount > 0


async def resort(
    conn: Conn, c: Collection, settings: Settings, p: Params, *, dry_run: bool
) -> list[dict[str, Any]]:
    """Full re-sort: books for every leaf not pinned to one, then positions in every book.
    A dry run computes the same moves and writes nothing."""
    await lock(conn, c)
    st = await load_state(conn, c, settings)
    scored = [lf for lf in st.leaves.values() if lf.image_id in st.has_vec]
    for iid, number in resort_books(scored, st.spaces, c.leaves_per_book, p).items():
        lf = st.leaves[iid]
        lf.book = number
        lf.position = 0  # placed by the rechain below
    st.refresh_spaces()
    rechain(st, set(st.book_ids))
    if dry_run:
        return [
            {
                "image_id": iid,
                "from": st.before[iid],
                "to": (lf.book, lf.position),
                "reason": "resort",
            }
            for iid, lf in sorted(st.leaves.items())
            if st.before[iid] != (lf.book, lf.position)
        ]
    moves = await commit(conn, st, "resort")
    await conn.execute("update collections set resorted_at = now() where id = %s", (c.id,))
    return moves


# -- anchors ------------------------------------------------------------------


async def stale_anchors(
    conn: Conn, settings: Settings, cid: int | None = None
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        select id, anchor_text from collection_books
         where anchor_text <> '' and (anchor_vec is null or anchor_model is distinct from %s)
           and (%s::int is null or collection_id = %s)
         order by id
        """,
        (settings.embed_model, cid, cid),
    )
    return list(await cur.fetchall())


async def store_anchor(conn: Conn, book_id: int, text: str, vec: np.ndarray, model: str) -> None:
    # Only if the text is still what was embedded: an edit meanwhile queued its own job.
    await conn.execute(
        "update collection_books set anchor_vec = %s, anchor_model = %s, updated_at = now()"
        " where id = %s and anchor_text = %s",
        (vec, model, book_id, text),
    )
