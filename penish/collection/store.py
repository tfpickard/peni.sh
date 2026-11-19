"""Collection tables: requests, filing, owner moves, re-sorts and their history.

Every change that plans takes the collection's advisory lock inside its transaction, so a
filing job, an admin move and a re-sort never plan against each other's half-written
state. Shared by the api (owner actions) and the worker (filing, anchors, re-sort).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish.collection.numerals import parse_line, roman
from penish.collection.plan import (
    Book,
    Leaf,
    Placement,
    Shape,
    Tuning,
    choose_book,
    lay_books,
    plan_resort,
)
from penish.config import Settings

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Reason = Literal["auto", "resort", "owner"]

FILE_JOB = "collection_file"
ANCHOR_JOB = "collection_anchor"
RESORT_JOB = "collection_resort"
# The embedding kind the similar lists use; the vectors under it are made from the analysis.
KIND = "description"


class Refused(Exception):
    """An owner request that cannot be carried out as asked. The message is shown as is."""


@dataclass(frozen=True)
class Move:
    image_id: int
    before: tuple[int, int] | None
    after: tuple[int, int] | None
    reason: Reason


@dataclass
class State:
    collection: dict[str, Any]
    shape: Shape
    leaves: list[Leaf]
    # Centering for vectors that join later (the picture being filed).
    mean: np.ndarray | None

    def leaf(self, image_id: int) -> Leaf | None:
        return next((x for x in self.leaves if x.image_id == image_id), None)

    def occupied(self, book: int, besides: int | None = None) -> int:
        return sum(1 for x in self.leaves if x.book == book and x.image_id != besides)


# -- reading ---------------------------------------------------------------------


async def collection_by_slug(conn: Conn, slug: str) -> dict[str, Any] | None:
    cur = await conn.execute("select * from collections where slug = %s", (slug,))
    row: dict[str, Any] | None = await cur.fetchone()
    return row


async def collection_by_id(conn: Conn, collection_id: int) -> dict[str, Any] | None:
    cur = await conn.execute("select * from collections where id = %s", (collection_id,))
    row: dict[str, Any] | None = await cur.fetchone()
    return row


def _prep(raw: Any, mean: np.ndarray | None) -> np.ndarray | None:
    if raw is None:
        return None
    v = np.asarray(raw, dtype=np.float64)
    if mean is not None and len(mean) == len(v):
        c = v - mean
        n = float(np.linalg.norm(c))
        # A vector equal to the mean has no direction once centered; keep it raw, as the
        # similar lists do.
        if n > 1e-12:
            out: np.ndarray = c / n
            return out
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else None


async def _mean(conn: Conn, settings: Settings) -> np.ndarray | None:
    """The corpus mean when the site centers its similarity, None when it does not."""
    cur = await conn.execute(
        """
        select (select count(*) from embeddings e join images i on i.id = e.image_id
                 where e.kind = %(k)s and e.model = %(m)s and e.dim = %(d)s
                   and i.status = 'live') as n,
               (select mean::real[] from corpus_stats
                 where kind = %(k)s and model = %(m)s and vector_dims(mean) = %(d)s) as mean
        """,
        {"k": KIND, "m": settings.embed_model, "d": settings.embed_dim},
    )
    row = await cur.fetchone()
    if row is None or row["mean"] is None or int(row["n"]) < settings.centered_min_images:
        return None
    return np.asarray(row["mean"], dtype=np.float64)


async def lock(conn: Conn, collection_id: int) -> None:
    await conn.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"collection:{collection_id}",)
    )


async def load(conn: Conn, collection: dict[str, Any], settings: Settings) -> State:
    """Everything the planner needs, centered as the similar lists would center it."""
    mean = await _mean(conn, settings)
    params = {"c": collection["id"], "m": settings.embed_model, "d": settings.embed_dim}
    cur = await conn.execute(
        """
        select number, case when anchor_model = %(m)s and anchor_dim = %(d)s
                            then anchor_embedding::real[] end as anchor,
               tag_hints
          from collection_books where collection_id = %(c)s order by number
        """,
        params,
    )
    books = [
        Book(r["number"], _prep(r["anchor"], mean), frozenset(r["tag_hints"] or ()))
        for r in await cur.fetchall()
    ]
    cur = await conn.execute(
        """
        select l.image_id, l.book, l.position, l.book_pinned, l.pinned,
               case when i.status = 'live' then e.vec::real[] end as vec,
               coalesce((select array_agg(t.tag) from tags t where t.image_id = l.image_id),
                        '{}') as tags
          from collection_leaves l
          join images i on i.id = l.image_id
          left join embeddings e on e.image_id = l.image_id and e.kind = 'description'
                                and e.model = %(m)s and e.dim = %(d)s
         where l.collection_id = %(c)s
         order by l.image_id
        """,
        params,
    )
    leaves = [
        Leaf(
            r["image_id"],
            r["book"],
            r["position"],
            _prep(r["vec"], mean),
            frozenset(r["tags"]),
            r["book_pinned"],
            r["pinned"],
        )
        for r in await cur.fetchall()
    ]
    shape = Shape(books, int(collection["leaves_per_book"]), Tuning.from_json(collection["tuning"]))
    return State(collection, shape, leaves, mean)


async def _candidate(
    conn: Conn, image_id: int, state: State, settings: Settings
) -> tuple[str | None, np.ndarray | None, frozenset[str]]:
    cur = await conn.execute(
        """
        select i.status::text as status, e.vec::real[] as vec,
               coalesce((select array_agg(t.tag) from tags t where t.image_id = i.id),
                        '{}') as tags
          from images i
          left join embeddings e on e.image_id = i.id and e.kind = 'description'
                                and e.model = %(m)s and e.dim = %(d)s
         where i.id = %(id)s
        """,
        {"id": image_id, "m": settings.embed_model, "d": settings.embed_dim},
    )
    row = await cur.fetchone()
    if row is None:
        return None, None, frozenset()
    return row["status"], _prep(row["vec"], state.mean), frozenset(row["tags"])


# -- writing ---------------------------------------------------------------------


async def _write(
    conn: Conn,
    state: State,
    placement: Placement,
    reason: Reason,
    *,
    owner: int | None = None,
    before: dict[int, tuple[int, int] | None] | None = None,
    touched: frozenset[int] = frozenset(),
) -> list[Move]:
    """Bring the rows in line with the plan and append a history row per move. `owner` is
    the leaf the owner moved; the leaves it pushes aside move for the filer's reason.
    `touched` are leaves whose pins changed without necessarily moving."""
    if before is None:
        before = {x.image_id: (x.book, x.position) for x in state.leaves}
    cid = state.collection["id"]
    moves: list[Move] = []
    for leaf in state.leaves:
        new = placement[leaf.image_id]
        old = before.get(leaf.image_id)
        why: Reason = "owner" if leaf.image_id == owner else reason
        if old is None:
            await conn.execute(
                """
                insert into collection_leaves
                  (image_id, collection_id, book, position, book_pinned, pinned)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (leaf.image_id, cid, new[0], new[1], leaf.book_pinned, leaf.pinned),
            )
        elif old != new or leaf.image_id in touched:
            await conn.execute(
                """
                update collection_leaves
                   set book = %s, position = %s, book_pinned = %s, pinned = %s, updated_at = now()
                 where image_id = %s and collection_id = %s
                """,
                (new[0], new[1], leaf.book_pinned, leaf.pinned, leaf.image_id, cid),
            )
        if old != new:
            moves.append(Move(leaf.image_id, old, new, why))
    for m in moves:
        await _history(conn, cid, m)
    return moves


async def _history(conn: Conn, collection_id: int, m: Move) -> None:
    await conn.execute(
        """
        insert into collection_leaf_history
          (image_id, collection_id, from_book, from_position, to_book, to_position, reason)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            m.image_id,
            collection_id,
            m.before[0] if m.before else None,
            m.before[1] if m.before else None,
            m.after[0] if m.after else None,
            m.after[1] if m.after else None,
            m.reason,
        ),
    )


def _check_shape(collection: dict[str, Any], book: int | None, position: int | None) -> None:
    if book is not None and not 1 <= book <= int(collection["book_count"]):
        raise Refused(f"no book {roman(book) if book >= 1 else book}")
    if position is not None and not 1 <= position <= int(collection["leaves_per_book"]):
        raise Refused(f"no leaf {position}")
    if position is not None and book is None:
        raise Refused("a leaf needs a book")


async def _place(
    conn: Conn,
    state: State,
    image_id: int,
    vec: np.ndarray | None,
    tags: frozenset[str],
    book: int | None,
    position: int | None,
    reason: Reason,
) -> list[Move]:
    """File or move one picture. A book given pins the book; a leaf given pins the leaf.
    With neither, the filer picks the book; then only its own book is re-laid."""
    _check_shape(state.collection, book, position)
    current = state.leaf(image_id)
    pinned_book = book is not None
    if book is None:
        if current is not None:
            return []
        if vec is None:
            raise Refused("no vector")
        others = [x for x in state.leaves if x.image_id != image_id]
        chosen = choose_book(state.shape, others, vec, tags)
        if chosen is None:
            raise Refused("every book is full")
        book = chosen[0]
    if state.occupied(book, besides=image_id) >= state.shape.cap:
        raise Refused(f"book {roman(book)} is full")
    if position is not None:
        holder = next(
            (
                x
                for x in state.leaves
                if x.book == book and x.position == position and x.image_id != image_id
            ),
            None,
        )
        if holder is not None and holder.pinned:
            raise Refused(f"book {roman(book)} leaf {position} is taken")

    before: dict[int, tuple[int, int] | None] = {
        x.image_id: (x.book, x.position) for x in state.leaves
    }
    before[image_id] = (current.book, current.position) if current else None
    me = Leaf(
        image_id,
        book,
        position or 0,
        vec if current is None else current.vec,
        tags,
        book_pinned=pinned_book,
        pinned=position is not None,
    )
    state.leaves = [x for x in state.leaves if x.image_id != image_id] + [me]
    where = {x.image_id: x.book for x in state.leaves}
    placement = lay_books(state.shape, state.leaves, where, [book])
    # Leaves pushed aside by an owner's pin move for the filer's reason, not the owner's.
    return await _write(
        conn,
        state,
        placement,
        "auto",
        owner=image_id if reason == "owner" else None,
        before=before,
        touched=frozenset({image_id}),
    )


# -- owner actions -------------------------------------------------------------------


async def place(
    conn: Conn,
    collection: dict[str, Any],
    image_id: int,
    book: int,
    position: int | None,
    settings: Settings,
) -> list[Move]:
    """An owner move: pins the book, and the leaf if one is given."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        state = await load(conn, collection, settings)
        if state.leaf(image_id) is None:
            raise Refused("not filed")
        return await _place(conn, state, image_id, None, frozenset(), book, position, "owner")


async def set_pin(
    conn: Conn, collection: dict[str, Any], image_id: int, pinned: bool, settings: Settings
) -> list[Move]:
    """Pin a leaf where it is, or hand it back to the filer, which re-lays its book."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        state = await load(conn, collection, settings)
        leaf = state.leaf(image_id)
        if leaf is None:
            raise Refused("not filed")
        leaf.book_pinned = leaf.pinned = pinned
        where = {x.image_id: x.book for x in state.leaves}
        placement = lay_books(state.shape, state.leaves, where, [] if pinned else [leaf.book])
        return await _write(conn, state, placement, "auto", touched=frozenset({image_id}))


async def unfile(conn: Conn, collection_id: int, image_id: int) -> bool:
    """Out of the collection, and any pending request with it. The picture stays on the
    site and its slot stays empty: the rest of the book does not close up around it."""
    async with conn.transaction():
        await lock(conn, collection_id)
        cur = await conn.execute(
            "delete from collection_requests where image_id = %s and collection_id = %s",
            (image_id, collection_id),
        )
        dropped = cur.rowcount > 0
        cur = await conn.execute(
            "delete from collection_leaves where image_id = %s and collection_id = %s"
            " returning book, position",
            (image_id, collection_id),
        )
        row = await cur.fetchone()
        if row is not None:
            await _history(
                conn, collection_id, Move(image_id, (row["book"], row["position"]), None, "owner")
            )
    return dropped or row is not None


async def resort(
    conn: Conn, collection: dict[str, Any], settings: Settings, *, dry_run: bool
) -> list[Move]:
    """Book for every leaf the owner did not pin to one, then positions in every book."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        state = await load(conn, collection, settings)
        placement = plan_resort(state.shape, state.leaves)
        if dry_run:
            return [
                Move(x.image_id, (x.book, x.position), placement[x.image_id], "resort")
                for x in state.leaves
                if placement[x.image_id] != (x.book, x.position)
            ]
        moves = await _write(conn, state, placement, "resort")
        await conn.execute(
            "update collections set last_resort_at = now() where id = %s", (collection["id"],)
        )
        return moves


# -- requests -------------------------------------------------------------------


async def enqueue_file(conn: Conn, image_id: int, collection_id: int) -> None:
    await conn.execute(
        "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s) on conflict do nothing",
        (
            FILE_JOB,
            Jsonb({"image_id": image_id, "collection_id": collection_id}),
            f"{FILE_JOB}:{image_id}:{collection_id}",
        ),
    )


# For the go-live transaction: every waiting request of the picture gets its job there, so
# a crash between going live and enqueueing cannot strand one.
ENQUEUE_FOR_IMAGE = f"""
    insert into jobs (type, payload, dedupe_key)
    select '{FILE_JOB}',
           jsonb_build_object('image_id', r.image_id, 'collection_id', r.collection_id),
           '{FILE_JOB}:' || r.image_id || ':' || r.collection_id
      from collection_requests r
     where r.image_id = %s and r.error is null
    on conflict do nothing
"""


async def request(
    conn: Conn,
    collection: dict[str, Any],
    image_id: int,
    book: int | None,
    position: int | None,
) -> None:
    """Mark a picture for the collection. A live picture is queued for filing now; one
    still in ingest is queued when it goes live."""
    _check_shape(collection, book, position)
    await conn.execute(
        """
        insert into collection_requests (image_id, collection_id, book, position)
        values (%s, %s, %s, %s)
        on conflict (image_id, collection_id) do update set
          book = excluded.book, position = excluded.position, error = null, updated_at = now()
        """,
        (image_id, collection["id"], book, position),
    )
    cur = await conn.execute("select status::text as s from images where id = %s", (image_id,))
    row = await cur.fetchone()
    if row is not None and row["s"] == "live":
        await enqueue_file(conn, image_id, collection["id"])


async def request_from_line(conn: Conn, image_id: int, line: str) -> None:
    """A `_collection.txt` sidecar. Anything wrong with it is logged and left as a failed
    job on the queue page, and the picture carries on without it."""
    try:
        req = parse_line(line)
        collection = await collection_by_slug(conn, req.collection)
        if collection is None:
            raise ValueError(f"no collection {req.collection!r}")
        await request(conn, collection, image_id, req.book, req.position)
    except (ValueError, Refused) as e:
        log.warning("image %s: collection sidecar %r dropped: %s", image_id, line[:200], e)
        # Once per picture and line: an ingest retried for an unrelated reason reads the
        # same sidecar again.
        await conn.execute(
            """
            insert into jobs (type, payload, status, attempts, last_error)
            select %(t)s, %(p)s, 'failed', 1, %(e)s
             where not exists (select 1 from jobs where type = %(t)s and payload = %(p)s
                                and status = 'failed')
            """,
            {
                "t": FILE_JOB,
                "p": Jsonb({"image_id": image_id, "line": line[:200]}),
                "e": f"collection sidecar: {e}",
            },
        )


async def file_request(
    conn: Conn, image_id: int, collection_id: int, settings: Settings
) -> Literal["filed", "waiting", "gone"] | str:
    """Carry out a waiting request. Returns `filed`, `waiting` (not live or not embedded
    yet), `gone` (no request, or no picture), or the reason it was refused, which is also
    kept on the request for the admin."""
    async with conn.transaction():
        await lock(conn, collection_id)
        cur = await conn.execute(
            "select * from collection_requests where image_id = %s and collection_id = %s",
            (image_id, collection_id),
        )
        req = await cur.fetchone()
        collection = await collection_by_id(conn, collection_id)
        if req is None or collection is None:
            return "gone"
        state = await load(conn, collection, settings)
        status, vec, tags = await _candidate(conn, image_id, state, settings)
        if status is None:
            return "gone"
        if status != "live":
            return "waiting"
        if vec is None and state.leaf(image_id) is None:
            return "waiting"
        reason: Reason = "owner" if req["book"] is not None else "auto"
        try:
            moves = await _place(
                conn, state, image_id, vec, tags, req["book"], req["position"], reason
            )
        except Refused as e:
            await conn.execute(
                "update collection_requests set error = %s, updated_at = now()"
                " where image_id = %s and collection_id = %s",
                (str(e), image_id, collection_id),
            )
            log.warning("collection %s: image %s not filed: %s", collection_id, image_id, e)
            return str(e)
        await conn.execute(
            "delete from collection_requests where image_id = %s and collection_id = %s",
            (image_id, collection_id),
        )
    log.info("collection %s: image %s filed, %d moves", collection_id, image_id, len(moves))
    return "filed"


# -- anchors -------------------------------------------------------------------


async def stale_anchors(conn: Conn, collection_id: int, settings: Settings) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        select number, anchor_text from collection_books
         where collection_id = %(c)s and anchor_text <> ''
           and (anchor_embedding is null or anchor_model is distinct from %(m)s
                or anchor_dim is distinct from %(d)s)
         order by number
        """,
        {"c": collection_id, "m": settings.embed_model, "d": settings.embed_dim},
    )
    return list(await cur.fetchall())


async def store_anchor(
    conn: Conn, collection_id: int, number: int, text: str, vec: np.ndarray, settings: Settings
) -> bool:
    # Conditional on the text: an edit while the model ran would otherwise get the old vector.
    cur = await conn.execute(
        """
        update collection_books set anchor_embedding = %s, anchor_model = %s, anchor_dim = %s
         where collection_id = %s and number = %s and anchor_text = %s
        """,
        (vec, settings.embed_model, settings.embed_dim, collection_id, number, text),
    )
    return cur.rowcount > 0


# -- public reads ---------------------------------------------------------------------


async def leaf_for_image(conn: Conn, image_id: int) -> dict[str, Any] | None:
    """The picture's leaf in the first published collection, with the latest place it
    was filed before (null when it has never been anywhere else)."""
    cur = await conn.execute(
        """
        select c.slug, c.abbrev, c.abbrev_latin, l.book, l.position,
               (select row_to_json(h) from (
                  select from_book as book, from_position as position
                    from collection_leaf_history
                   where image_id = l.image_id and collection_id = l.collection_id
                     and from_book is not null
                   order by id desc limit 1) h) as previous
          from collection_leaves l
          join collections c on c.id = l.collection_id
         where l.image_id = %s and c.published
         order by c.id
         limit 1
        """,
        (image_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    prev = row["previous"]
    if prev is not None and (prev["book"], prev["position"]) == (row["book"], row["position"]):
        # Taken out and put back where it was: nothing to record.
        prev = None
    return {
        "collection": {
            "slug": row["slug"],
            "abbrev": row["abbrev"],
            "abbrev_latin": row["abbrev_latin"],
        },
        "book": row["book"],
        "position": row["position"],
        "previous": prev,
    }
