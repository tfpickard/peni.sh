"""Collection tables: requests, filing, owner moves, re-sorts and their history.

Every change that plans takes the collection's advisory lock inside its transaction, so a
filing job, an admin move and a re-sort never plan against each other's half-written
state. Shared by the api (owner actions) and the worker (filing, anchors, re-sort).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish.collection import judge
from penish.collection.judge import Failed, Judgement, ShownBook, Texts, Verdict
from penish.collection.numerals import parse_line, roman
from penish.collection.plan import (
    Book,
    Leaf,
    Placement,
    Shape,
    Tuning,
    Weighing,
    lay_books,
    plan_resort,
    weigh,
    weigh_leaf,
    weigh_leaves,
)
from penish.config import Settings

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Reason = Literal["auto", "resort", "owner"]
# How a leaf's book was decided, kept on its history row.
ChosenBy = Literal["score", "owner", "judge"]
# A runtime setting by key (penish.runtime.getter).
Get = Callable[[str], Awaitable[Any]]

FILE_JOB = "collection_file"
ANCHOR_JOB = "collection_anchor"
RESORT_JOB = "collection_resort"
JUDGE_JOB = "collection_judge"
NOTE_JOB = "collection_note"
# An owner note is shown as written; this only keeps a runaway paste out of the page.
NOTE_MAX = 4000
# The embedding kind the similar lists use; the vectors under it are made from the analysis.
KIND = "description"


class Refused(Exception):
    """An owner request that cannot be carried out as asked. The message is shown as is."""


class AnchorsWaiting(Refused):
    """A book's anchor has no vector yet, so planning now would score that book blind."""

    def __init__(self) -> None:
        super().__init__("anchors")


class Judging(Refused):
    """A leaf has no judgement for what it is now. The worker asks; the api never calls a
    model, since only the worker holds the keys."""

    def __init__(self) -> None:
        super().__init__("judging")


@dataclass(frozen=True)
class Move:
    image_id: int
    before: tuple[int, int] | None
    after: tuple[int, int] | None
    reason: Reason
    # Set when this move is where the leaf's book was decided.
    chosen_by: ChosenBy | None = None
    scores: dict[str, Any] | None = None


@dataclass(frozen=True)
class Choice:
    """A book for a picture, how it was decided, and what the scores said then."""

    book: int
    chosen_by: ChosenBy
    scores: dict[str, Any] | None = None


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
                        '{}') as tags,
               coalesce((select h.chosen_by = 'judge' from collection_leaf_history h
                          where h.image_id = l.image_id and h.collection_id = l.collection_id
                            and h.chosen_by is not null
                          order by h.id desc limit 1), false) as judged
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
            r["judged"],
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
    basis: Mapping[int, Choice] | None = None,
) -> list[Move]:
    """Bring the rows in line with the plan and append a history row per move. `owner` is
    the leaf the owner moved; the leaves it pushes aside move for the filer's reason.
    `touched` are leaves whose pins changed without necessarily moving. `basis` says how
    a leaf's book was chosen, for the leaves whose book this event decided."""
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
            c = (basis or {}).get(leaf.image_id)
            moves.append(
                Move(
                    leaf.image_id,
                    old,
                    new,
                    why,
                    c.chosen_by if c else None,
                    c.scores if c else None,
                )
            )
    for m in moves:
        await _history(conn, cid, m)
    return moves


async def _history(conn: Conn, collection_id: int, m: Move) -> None:
    await conn.execute(
        """
        insert into collection_leaf_history
          (image_id, collection_id, from_book, from_position, to_book, to_position, reason,
           chosen_by, scores)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            m.image_id,
            collection_id,
            m.before[0] if m.before else None,
            m.before[1] if m.before else None,
            m.after[0] if m.after else None,
            m.after[1] if m.after else None,
            m.reason,
            m.chosen_by,
            Jsonb(m.scores) if m.scores is not None else None,
        ),
    )


def _check_shape(collection: dict[str, Any], book: int | None, position: int | None) -> None:
    if book is not None and not 1 <= book <= int(collection["book_count"]):
        raise Refused(f"no book {roman(book) if book >= 1 else book}")
    if position is not None and not 1 <= position <= int(collection["leaves_per_book"]):
        raise Refused(f"no leaf {position}")
    if position is not None and book is None:
        raise Refused("a leaf needs a book")


def _record(w: Weighing, tuning: Tuning) -> dict[str, Any]:
    # The tuning goes with the scores: they mean nothing once it has been changed.
    return {**w.to_json(), "tuning": tuning.to_json()}


def choose(
    state: State,
    image_id: int,
    vec: np.ndarray | None,
    tags: frozenset[str],
    book: int | None,
    verdict: Verdict | None = None,
) -> Choice:
    """Which book a picture is filed into: the owner's when given, else the judge's when it
    gave one for a book that still has room, else the best score among books with room.
    Every filing decides here. `verdict` was asked before the lock was taken; nothing here
    calls a model. The scores are kept either way, so the admin can show what the filer
    thought of a book the owner or the judge chose, and why the judge did not decide."""
    others = [x for x in state.leaves if x.image_id != image_id]
    w = weigh(state.shape, others, vec, tags) if vec is not None else None
    scores = _record(w, state.shape.tuning) if w is not None else None
    if book is not None:
        return Choice(book, "owner", scores)
    if vec is None:
        raise Refused("no vector")
    if w is None:
        raise Refused("every book is full")
    if isinstance(verdict, Judgement) and verdict.book in w.terms:
        return Choice(verdict.book, "judge", {**(scores or {}), "judge": verdict.to_json()})
    if isinstance(verdict, Judgement):
        # The book filled between the question and the lock.
        verdict = Failed("full", verdict.model, f"book {roman(verdict.book)}")
    if verdict is not None:
        log.log(
            logging.INFO if verdict.error == "off" else logging.WARNING,
            "collection %s: image %s filed by score, judge %s",
            state.collection["id"],
            image_id,
            verdict.error,
        )
        scores = {**(scores or {}), "judge": verdict.to_json()}
    return Choice(w.best, "score", scores)


async def _place(
    conn: Conn,
    state: State,
    image_id: int,
    vec: np.ndarray | None,
    tags: frozenset[str],
    book: int | None,
    position: int | None,
    reason: Reason,
    verdict: Verdict | None = None,
) -> list[Move]:
    """File or move one picture. A book given pins the book; a leaf given pins the leaf.
    With neither, the filer picks the book; then only its own book is re-laid."""
    _check_shape(state.collection, book, position)
    current = state.leaf(image_id)
    pinned_book = book is not None
    if book is None and current is not None:
        return []
    choice = choose(state, image_id, vec, tags, book, verdict)
    book = choice.book
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
        judged=choice.chosen_by == "judge",
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
        basis={image_id: choice},
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


def _diff(state: State, placement: Placement) -> tuple[list[Move], dict[int, float | None]]:
    """Every leaf the placement moves, and how far its new book leads the best other book
    with the leaf left out of its own (None without a vector). A leaf that changes book
    carries its scores, since this is where its book was decided."""
    where = {i: bp[0] for i, bp in placement.items()}
    moving = [x.image_id for x in state.leaves if placement[x.image_id] != (x.book, x.position)]
    weighed = weigh_leaves(state.shape, state.leaves, where, moving)
    moves: list[Move] = []
    leads: dict[int, float | None] = {}
    for x in state.leaves:
        after = placement[x.image_id]
        if after == (x.book, x.position):
            continue
        w = weighed[x.image_id]
        leads[x.image_id] = w.lead(after[0]) if w is not None else None
        crossed = after[0] != x.book
        moves.append(
            Move(
                x.image_id,
                (x.book, x.position),
                after,
                "resort",
                "score" if crossed else None,
                _record(w, state.shape.tuning) if crossed and w is not None else None,
            )
        )
    return moves, leads


def _basis(moves: list[Move]) -> dict[int, Choice]:
    return {
        m.image_id: Choice(m.after[0], m.chosen_by, m.scores)
        for m in moves
        if m.chosen_by is not None and m.after is not None
    }


async def _commit_resort(
    conn: Conn, state: State, placement: Placement, moves: list[Move]
) -> list[Move]:
    written = await _write(conn, state, placement, "resort", basis=_basis(moves))
    await conn.execute(
        "update collections set last_resort_at = now() where id = %s", (state.collection["id"],)
    )
    return written


async def resort(
    conn: Conn, collection: dict[str, Any], settings: Settings, *, dry_run: bool
) -> list[Move]:
    """Book for every leaf the owner did not pin to one, then positions in every book.
    The weekly job's path: it plans and writes in one go, after embedding the anchors."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        state = await load(conn, collection, settings)
        placement = plan_resort(state.shape, state.leaves)
        moves, _ = _diff(state, placement)
        if dry_run:
            return moves
        return await _commit_resort(conn, state, placement, moves)


# -- the owner's re-sort: a plan, then exactly that plan ---------------------------------


@dataclass(frozen=True)
class Plan:
    # Names the plan and the state it was planned from; apply refuses any other state.
    id: str
    moves: list[Move]
    leads: dict[int, float | None]


async def _digest(conn: Conn, collection: dict[str, Any]) -> dict[str, Any]:
    """What a plan was made from: the shape and tuning, each book's anchor text, hints and
    vector, and every leaf's place and pins, and whether the judge chose its book. A
    picture's own vector is left out: a plan made from it is still the plan the owner was
    shown, and the corpus mean moves with every upload."""
    cid = collection["id"]
    tuning = Tuning.from_json(collection["tuning"]).to_json()
    # A switch, not a weight: it changes no plan.
    tuning.pop("weekly_resort", None)
    cur = await conn.execute(
        """
        select number, anchor_text, tag_hints, anchor_model, anchor_dim,
               md5(anchor_embedding::text) as anchor
          from collection_books where collection_id = %s order by number
        """,
        (cid,),
    )
    books = [
        [
            r["number"],
            r["anchor_text"],
            sorted(r["tag_hints"] or ()),
            r["anchor_model"],
            r["anchor_dim"],
            r["anchor"],
        ]
        for r in await cur.fetchall()
    ]
    cur = await conn.execute(
        """
        select l.image_id, l.book, l.position, l.book_pinned, l.pinned,
               coalesce((select h.chosen_by = 'judge' from collection_leaf_history h
                          where h.image_id = l.image_id and h.collection_id = l.collection_id
                            and h.chosen_by is not null
                          order by h.id desc limit 1), false) as judged
          from collection_leaves l
         where l.collection_id = %s order by l.image_id
        """,
        (cid,),
    )
    leaves = [
        [r["image_id"], r["book"], r["position"], r["book_pinned"], r["pinned"], r["judged"]]
        for r in await cur.fetchall()
    ]
    return {
        "collection": cid,
        "book_count": collection["book_count"],
        "cap": collection["leaves_per_book"],
        "tuning": tuning,
        "books": books,
        "leaves": leaves,
    }


def _stale(digest: dict[str, Any], settings: Settings) -> bool:
    # As stale_anchors: written text with no vector under today's model.
    return any(
        text and (vec is None or model != settings.embed_model or dim != settings.embed_dim)
        for _, text, _, model, dim, vec in digest["books"]
    )


PlanMove = tuple[int, tuple[int, int], tuple[int, int]]


def plan_id(digest: dict[str, Any], moves: list[PlanMove]) -> str:
    body = {
        "v": 1,
        "state": digest,
        "moves": sorted([i, list(b), list(a)] for i, b, a in moves),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _plan_moves(moves: list[Move]) -> list[PlanMove]:
    return [(m.image_id, m.before, m.after) for m in moves if m.before and m.after]


async def _fresh(conn: Conn, collection: dict[str, Any]) -> dict[str, Any]:
    # Re-read under the lock: the row the caller holds may predate a tuning edit.
    row = await collection_by_id(conn, collection["id"])
    if row is None:
        raise Refused("not here")
    return row


async def plan(conn: Conn, collection: dict[str, Any], settings: Settings) -> Plan:
    """The re-sort as it would be made now, and its id. Writes nothing. Refuses while an
    anchor waits for its vector; the caller queues the anchor job."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        fresh = await _fresh(conn, collection)
        before = await _digest(conn, fresh)
        if _stale(before, settings):
            raise AnchorsWaiting()
        state = await load(conn, fresh, settings)
        # Owner text and tuning are edited without the lock; a plan read across such an
        # edit would carry an id for a state it was not made from.
        if await _digest(conn, await _fresh(conn, fresh)) != before:
            raise Refused("changed")
        placement = plan_resort(state.shape, state.leaves)
        moves, leads = _diff(state, placement)
        return Plan(plan_id(before, _plan_moves(moves)), moves, leads)


def _check_plan(state: State, placement: Placement, *, hold_judged: bool = True) -> None:
    """A plan that came back over the wire still has to keep the owner's pins and the
    book's shape, and a vector re-sort the judge's books; the id alone does not prove it
    was ever made here."""
    books = {b.number for b in state.shape.books}
    for x in state.leaves:
        book, pos = placement[x.image_id]
        if x.pinned and (book, pos) != (x.book, x.position):
            raise Refused("bad plan")
        if (x.book_pinned or (hold_judged and x.judged)) and book != x.book:
            raise Refused("bad plan")
        if book not in books or not 1 <= pos <= state.shape.cap:
            raise Refused("bad plan")
    if len(set(placement.values())) != len(placement):
        raise Refused("bad plan")


async def apply(
    conn: Conn,
    collection: dict[str, Any],
    settings: Settings,
    given_id: str,
    moves: list[PlanMove],
) -> list[Move]:
    """Exactly the plan the owner was shown, or nothing: `changed` when the collection is
    no longer the one it was planned from."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        fresh = await _fresh(conn, collection)
        if plan_id(await _digest(conn, fresh), moves) != given_id:
            raise Refused("changed")
        state = await load(conn, fresh, settings)
        placement: Placement = {x.image_id: (x.book, x.position) for x in state.leaves}
        for image_id, before, after in moves:
            if placement.get(image_id) != tuple(before):
                raise Refused("changed")
            placement[image_id] = (int(after[0]), int(after[1]))
        _check_plan(state, placement)
        diff, _ = _diff(state, placement)
        return await _commit_resort(conn, state, placement, diff)


async def enqueue_anchor_job(conn: Conn, collection_id: int) -> None:
    await conn.execute(
        "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s) on conflict do nothing",
        (ANCHOR_JOB, Jsonb({"collection_id": collection_id}), f"{ANCHOR_JOB}:{collection_id}"),
    )


# -- the judge: questions, and the owner's re-judging ------------------------------------


async def book_texts(conn: Conn, collection_id: int) -> dict[int, tuple[str | None, str]]:
    """Each book's title and anchor text, which is how the judge is shown a book."""
    cur = await conn.execute(
        "select number, title, anchor_text from collection_books where collection_id = %s",
        (collection_id,),
    )
    return {r["number"]: (r["title"], r["anchor_text"]) for r in await cur.fetchall()}


def shown_books(state: State, texts: Mapping[int, tuple[str | None, str]]) -> list[ShownBook]:
    """Every book of the collection, full or not, as the judge is shown it. Room is
    checked after the answer: store.choose and judge_plan both hold a full book's leaf."""
    out = []
    for b in state.shape.books:
        title, anchor = texts.get(b.number, (None, ""))
        out.append(ShownBook(b.number, title, anchor))
    return out


async def filing_inputs(
    conn: Conn, image_id: int, collection_id: int, settings: Settings
) -> tuple[Texts, list[ShownBook]] | None:
    """What the judge is asked before a filing, read without the lock so the call never
    holds it. None when there is nothing to ask: no request, an owner's book, a picture
    already filed, or no book with room."""
    cur = await conn.execute(
        "select book from collection_requests where image_id = %s and collection_id = %s",
        (image_id, collection_id),
    )
    req = await cur.fetchone()
    collection = await collection_by_id(conn, collection_id)
    if req is None or req["book"] is not None or collection is None:
        return None
    state = await load(conn, collection, settings)
    if state.leaf(image_id) is not None:
        return None
    if all(state.occupied(b.number) >= state.shape.cap for b in state.shape.books):
        return None
    books = shown_books(state, await book_texts(conn, collection_id))
    texts = (await judge.texts_of(conn, [image_id])).get(image_id, Texts())
    return texts, books


@dataclass(frozen=True)
class Question:
    image_id: int
    texts: Texts
    books: list[ShownBook]


async def questions(conn: Conn, state: State) -> tuple[list[Question], dict[int, str]]:
    """What re-judging asks of each leaf the owner did not hold to a book, and the leaves
    it cannot ask about, with why. A leaf with no vector stays put, as in a re-sort."""
    meta = await book_texts(conn, state.collection["id"])
    held = [x for x in state.leaves if not x.book_pinned]
    texts = await judge.texts_of(conn, [x.image_id for x in held])
    asks: list[Question] = []
    skipped: dict[int, str] = {}
    for x in sorted(held, key=lambda y: y.image_id):
        t = texts.get(x.image_id, Texts())
        if x.vec is None:
            skipped[x.image_id] = "no vector"
        elif not t.any():
            skipped[x.image_id] = "no text"
        else:
            asks.append(Question(x.image_id, t, shown_books(state, meta)))
    return asks, skipped


async def enqueue_judge_job(conn: Conn, collection_id: int) -> None:
    await conn.execute(
        "insert into jobs (type, payload, dedupe_key) values (%s, %s, %s) on conflict do nothing",
        (JUDGE_JOB, Jsonb({"collection_id": collection_id}), f"{JUDGE_JOB}:{collection_id}"),
    )


@dataclass(frozen=True)
class Held:
    """A leaf re-judging leaves where it is, and why: the book it was judged into is
    full, or there is no judgement for it."""

    image_id: int
    before: tuple[int, int]
    book: int | None
    why: str


@dataclass(frozen=True)
class JudgePlan:
    id: str
    moves: list[Move]
    leads: dict[int, float | None]
    held: list[Held]
    verdicts: dict[int, Verdict]
    # Some answer is a failure that is asked again; the caller queues the asking.
    retry: bool


async def _kept(
    conn: Conn, state: State, asks: list[Question], get: Get
) -> tuple[dict[int, Verdict | None], dict[int, str]]:
    """The kept answer to each question (None when it was never asked), and each one's
    input hash. Reads only: the worker asks."""
    model = str(await get(judge.MODEL_KEY))
    prompt = str(await get(judge.PROMPT_KEY))
    found: dict[int, Verdict | None] = {}
    hashes: dict[int, str] = {}
    for a in asks:
        h = judge.input_hash(model, prompt, a.texts, a.books)
        hashes[a.image_id] = h
        found[a.image_id] = await judge.lookup(conn, state.collection["id"], a.image_id, h)
    return found, hashes


def _judged_moves(moves: list[Move], verdicts: Mapping[int, Verdict]) -> list[Move]:
    """A move to another book is the judge's: its scores keep the weighing beside the
    judgement. A leaf only shifted within its book decides nothing."""
    out = []
    for m in moves:
        v = verdicts.get(m.image_id)
        if m.before and m.after and m.before[0] != m.after[0] and isinstance(v, Judgement):
            m = Move(
                m.image_id,
                m.before,
                m.after,
                "resort",
                "judge",
                {**(m.scores or {}), "judge": v.to_json()},
            )
        out.append(m)
    return out


def judge_plan_id(digest: dict[str, Any], moves: list[PlanMove], judged: Mapping[int, str]) -> str:
    """As plan_id, with the question behind each move to another book: a picture whose
    text changed, a retitled book or another model is not what the owner was shown."""
    body = {
        "v": 1,
        "kind": "judge",
        "state": digest,
        "moves": sorted([i, list(b), list(a)] for i, b, a in moves),
        "judged": sorted([i, h] for i, h in judged.items()),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def judge_plan(
    conn: Conn, collection: dict[str, Any], settings: Settings, get: Get
) -> JudgePlan:
    """Every leaf not held to its book by the owner, in the book the judge names for it,
    planned from kept judgements only. Refuses `judging` while a leaf has none (the caller
    queues the worker's asking) and `anchors` as the re-sort's dry run does. Writes
    nothing."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        fresh = await _fresh(conn, collection)
        digest = await _digest(conn, fresh)
        if _stale(digest, settings):
            raise AnchorsWaiting()
        state = await load(conn, fresh, settings)
        if await _digest(conn, await _fresh(conn, fresh)) != digest:
            raise Refused("changed")
        asks, skipped = await questions(conn, state)
        found, hashes = await _kept(conn, state, asks, get)
        if any(v is None for v in found.values()):
            raise Judging()
        verdicts: dict[int, Verdict] = {i: v for i, v in found.items() if v is not None}

        here = {x.image_id: x for x in state.leaves}
        where = {x.image_id: x.book for x in state.leaves}
        room = Counter(where.values())
        want = {
            i: v.book
            for i, v in sorted(verdicts.items())
            if isinstance(v, Judgement) and v.book != where[i]
        }
        # Room a leaf leaves behind can let another in, so go round until nothing fits.
        moved = True
        while moved:
            moved = False
            for i, book in list(want.items()):
                if room[book] < state.shape.cap:
                    room[where[i]] -= 1
                    room[book] += 1
                    where[i] = book
                    del want[i]
                    moved = True
        held = [Held(i, (here[i].book, here[i].position), book, "full") for i, book in want.items()]
        held += [
            Held(i, (here[i].book, here[i].position), None, v.error)
            for i, v in sorted(verdicts.items())
            if isinstance(v, Failed)
        ]
        held += [Held(i, (here[i].book, here[i].position), None, why) for i, why in skipped.items()]
        placement = lay_books(state.shape, state.leaves, where, [])
        diff, leads = _diff(state, placement)
        moves = _judged_moves(diff, verdicts)
        judged = {m.image_id: hashes[m.image_id] for m in moves if m.chosen_by == "judge"}
        return JudgePlan(
            judge_plan_id(digest, _plan_moves(moves), judged),
            moves,
            leads,
            held,
            verdicts,
            retry=not all(judge.settled(v) for v in verdicts.values()),
        )


async def apply_judged(
    conn: Conn,
    collection: dict[str, Any],
    settings: Settings,
    get: Get,
    given_id: str,
    moves: list[PlanMove],
) -> list[Move]:
    """Exactly the re-judging the owner was shown, or `changed`. Each leaf it moves to
    another book is the judge's from then on, so a vector re-sort leaves it there."""
    async with conn.transaction():
        await lock(conn, collection["id"])
        fresh = await _fresh(conn, collection)
        digest = await _digest(conn, fresh)
        state = await load(conn, fresh, settings)
        crossing = {i for i, b, a in moves if int(b[0]) != int(a[0])}
        asks, _ = await questions(conn, state)
        found, hashes = await _kept(conn, state, [a for a in asks if a.image_id in crossing], get)
        if crossing - set(hashes):
            raise Refused("changed")
        if judge_plan_id(digest, moves, hashes) != given_id:
            raise Refused("changed")
        placement: Placement = {x.image_id: (x.book, x.position) for x in state.leaves}
        for image_id, before, after in moves:
            if placement.get(image_id) != tuple(before):
                raise Refused("changed")
            placement[image_id] = (int(after[0]), int(after[1]))
        for i in crossing:
            v = found.get(i)
            if not isinstance(v, Judgement) or v.book != placement[i][0]:
                raise Refused("changed")
        _check_plan(state, placement, hold_judged=False)
        diff, _ = _diff(state, placement)
        verdicts: dict[int, Verdict] = {i: v for i, v in found.items() if v is not None}
        judged = _judged_moves(diff, verdicts)
        # A re-judging is not a re-sort: last_resort_at, which the weekly one runs by, stays.
        return await _write(conn, state, placement, "resort", basis=_basis(judged))


# -- why a leaf is where it is ------------------------------------------------------


async def explain(
    conn: Conn,
    collection: dict[str, Any],
    image_id: int,
    settings: Settings,
    get: Get | None = None,
) -> dict[str, Any] | None:
    """One leaf's scores by book as a re-sort would see them now, and what they were when
    it was filed; whether the judge chose its book, and the judgement kept for what the
    leaf and the books are now (with `get`). None when the picture is not a leaf here."""
    state = await load(conn, collection, settings)
    leaf = state.leaf(image_id)
    if leaf is None:
        return None
    judgement: dict[str, Any] | None = None
    if get is not None:
        texts = (await judge.texts_of(conn, [image_id])).get(image_id, Texts())
        books = shown_books(state, await book_texts(conn, collection["id"]))
        found, _ = await _kept(conn, state, [Question(image_id, texts, books)], get)
        v = found.get(image_id)
        judgement = v.to_json() if v is not None else None
    w = weigh_leaf(state.shape, state.leaves, image_id)
    cur = await conn.execute(
        """
        select at, to_book, to_position, reason, chosen_by, scores
          from collection_leaf_history
         where image_id = %s and collection_id = %s and from_book is null
         order by id desc limit 1
        """,
        (image_id, collection["id"]),
    )
    filed = await cur.fetchone()
    return {
        "image_id": image_id,
        "book": leaf.book,
        "position": leaf.position,
        "pinned": leaf.pinned,
        "book_pinned": leaf.book_pinned,
        "judged": leaf.judged,
        "judgement": judgement,
        "now": None if w is None else {**w.to_json(), "lead": w.lead(leaf.book)},
        "filed": None
        if filed is None
        else {
            "at": filed["at"].isoformat(),
            "book": filed["to_book"],
            "position": filed["to_position"],
            "reason": filed["reason"],
            "chosen_by": filed["chosen_by"],
            "scores": filed["scores"],
        },
    }


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
    note: str | None = None,
) -> None:
    """Mark a picture for the collection. A live picture is queued for filing now; one
    still in ingest is queued when it goes live. `note` is an owner note, written to the
    leaf once it is filed."""
    _check_shape(collection, book, position)
    note = clean_note(note)
    await conn.execute(
        """
        insert into collection_requests (image_id, collection_id, book, position, note)
        values (%s, %s, %s, %s, %s)
        on conflict (image_id, collection_id) do update set
          book = excluded.book, position = excluded.position,
          note = coalesce(excluded.note, collection_requests.note),
          error = null, updated_at = now()
        """,
        (image_id, collection["id"], book, position, note),
    )
    cur = await conn.execute("select status::text as s from images where id = %s", (image_id,))
    row = await cur.fetchone()
    if row is not None and row["s"] == "live":
        await enqueue_file(conn, image_id, collection["id"])


async def request_from_line(conn: Conn, image_id: int, line: str, note: str | None = None) -> None:
    """A `_collection.txt` sidecar, with the `_note.txt` beside it if there was one.
    Anything wrong with the line is logged and left as a failed job on the queue page,
    and the picture carries on without it (and without the note, which has no leaf)."""
    try:
        req = parse_line(line)
        collection = await collection_by_slug(conn, req.collection)
        if collection is None:
            raise ValueError(f"no collection {req.collection!r}")
        await request(conn, collection, image_id, req.book, req.position, note)
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
    conn: Conn,
    image_id: int,
    collection_id: int,
    settings: Settings,
    verdict: Verdict | None = None,
) -> Literal["filed", "waiting", "gone"] | str:
    """Carry out a waiting request. Returns `filed`, `waiting` (not live or not embedded
    yet), `gone` (no request, or no picture), or the reason it was refused, which is also
    kept on the request for the admin. `verdict` is the judge's, asked before this took
    the lock (filing_inputs says what to ask); an owner's book ignores it."""
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
                conn, state, image_id, vec, tags, req["book"], req["position"], reason, verdict
            )
        except Refused as e:
            await conn.execute(
                "update collection_requests set error = %s, updated_at = now()"
                " where image_id = %s and collection_id = %s",
                (str(e), image_id, collection_id),
            )
            log.warning("collection %s: image %s not filed: %s", collection_id, image_id, e)
            return str(e)
        if req["note"]:
            await set_note(conn, collection_id, image_id, req["note"])
        # Same transaction as the filing, so a filed leaf never waits on a lost job.
        await enqueue_note(conn, image_id, collection_id, only_missing=True)
        await conn.execute(
            "delete from collection_requests where image_id = %s and collection_id = %s",
            (image_id, collection_id),
        )
    log.info("collection %s: image %s filed, %d moves", collection_id, image_id, len(moves))
    return "filed"


# -- conservator notes --------------------------------------------------------------


def clean_note(text: str | None) -> str | None:
    """Owner text as written, trimmed; empty means no note."""
    if text is None:
        return None
    text = text.strip()[:NOTE_MAX].strip()
    return text or None


async def set_note(conn: Conn, collection_id: int, image_id: int, text: str | None) -> bool:
    """The owner's note, which nothing generated replaces. Empty clears it, and the leaf
    is then the conservator's to write again. False when the picture is not filed."""
    text = clean_note(text)
    cur = await conn.execute(
        """
        update collection_leaves
           set note = %s, note_source = case when %s::text is null then null else 'owner' end,
               note_model = null, updated_at = now()
         where image_id = %s and collection_id = %s
        """,
        (text, text, image_id, collection_id),
    )
    return cur.rowcount > 0


async def store_model_note(
    conn: Conn,
    collection_id: int,
    image_id: int,
    text: str,
    model: str,
    *,
    rewrite: bool = False,
) -> bool:
    """A conservator's note. Conditional in the statement itself, so an owner note saved
    while the model ran is never overwritten; without `rewrite` a model note is kept too."""
    cur = await conn.execute(
        """
        update collection_leaves
           set note = %(t)s, note_source = 'model', note_model = %(m)s, updated_at = now()
         where image_id = %(i)s and collection_id = %(c)s
           and note_source is distinct from 'owner'
           and (%(rewrite)s or note is null)
        """,
        {"t": text, "m": model, "i": image_id, "c": collection_id, "rewrite": rewrite},
    )
    return cur.rowcount > 0


def _note_key(image_id: int, collection_id: int, rewrite: bool) -> str:
    # Separate key space, as nudity's forced check has: an asked-for rewrite must not fold
    # into a queued fill-the-gap job that would keep the note it was asked to replace.
    key = f"{NOTE_JOB}:{image_id}:{collection_id}"
    return key + ":rewrite" if rewrite else key


async def enqueue_note(
    conn: Conn,
    image_id: int,
    collection_id: int,
    *,
    rewrite: bool = False,
    only_missing: bool = False,
) -> bool:
    """A note job for one leaf. Never for an owner note; `only_missing` also skips a leaf
    that already has a model note. True when a job was queued."""
    cur = await conn.execute(
        """
        insert into jobs (type, payload, dedupe_key)
        select %(t)s, %(p)s, %(k)s
          from collection_leaves l
         where l.image_id = %(i)s and l.collection_id = %(c)s
           and l.note_source is distinct from 'owner'
           and (not %(missing)s or l.note is null)
        on conflict do nothing
        """,
        {
            "t": NOTE_JOB,
            "p": Jsonb({"image_id": image_id, "collection_id": collection_id, "rewrite": rewrite}),
            "k": _note_key(image_id, collection_id, rewrite),
            "i": image_id,
            "c": collection_id,
            "missing": only_missing,
        },
    )
    return cur.rowcount > 0


async def enqueue_missing_notes(conn: Conn, collection_id: int) -> int:
    """One job per leaf with no note at all, in book and leaf order."""
    cur = await conn.execute(
        """
        insert into jobs (type, payload, dedupe_key)
        select %(t)s,
               jsonb_build_object('image_id', l.image_id, 'collection_id', l.collection_id,
                                  'rewrite', false),
               %(t)s || ':' || l.image_id || ':' || l.collection_id
          from collection_leaves l
         where l.collection_id = %(c)s and l.note is null
         order by l.book, l.position
        on conflict do nothing
        """,
        {"t": NOTE_JOB, "c": collection_id},
    )
    return cur.rowcount


@dataclass(frozen=True)
class NoteContext:
    """What the conservator is told besides the picture."""

    sha256: str
    crop: Any
    record: list[tuple[str, str]]
    book: int
    position: int
    leaves_per_book: int
    note: str | None
    note_source: str | None
    # The latest earlier place, (book, position), when the leaf has moved.
    previous: tuple[int, int] | None
    # (position, note) for the other leaves in the book that have one, in leaf order.
    book_notes: list[tuple[int, str]]


def record_pairs(raw: Any) -> list[tuple[str, str]]:
    """The stored record as pairs, skipping anything that is not a [label, value] pair of
    strings. The admin only writes pairs; this keeps a hand edit from breaking a page."""
    out: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            out.append((item[0], item[1]))
    return out


async def note_context(conn: Conn, collection_id: int, image_id: int) -> NoteContext | None:
    cur = await conn.execute(
        """
        select i.sha256, i.crop, c.record, c.leaves_per_book, l.book, l.position, l.note,
               l.note_source,
               (select array[h.from_book, h.from_position]
                  from collection_leaf_history h
                 where h.image_id = l.image_id and h.collection_id = l.collection_id
                   and h.from_book is not null
                 order by h.id desc limit 1) as previous
          from collection_leaves l
          join images i on i.id = l.image_id
          join collections c on c.id = l.collection_id
         where l.image_id = %s and l.collection_id = %s
        """,
        (image_id, collection_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    cur = await conn.execute(
        """
        select position, note from collection_leaves
         where collection_id = %s and book = %s and image_id <> %s and note is not null
         order by position
        """,
        (collection_id, row["book"], image_id),
    )
    others = [(int(r["position"]), str(r["note"])) for r in await cur.fetchall()]
    prev = row["previous"]
    previous = (int(prev[0]), int(prev[1])) if prev else None
    if previous == (row["book"], row["position"]):
        previous = None
    return NoteContext(
        sha256=row["sha256"],
        crop=row["crop"],
        record=record_pairs(row["record"]),
        book=int(row["book"]),
        position=int(row["position"]),
        leaves_per_book=int(row["leaves_per_book"]),
        note=row["note"],
        note_source=row["note_source"],
        previous=previous,
        book_notes=others,
    )


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
        update collection_books set anchor_embedding = %s, anchor_model = %s, anchor_dim = %s,
               anchor_embedded_at = now()
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
        select c.slug, c.title, c.abbrev, c.abbrev_latin, l.book, l.position, l.note,
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
            "title": row["title"],
            "abbrev": row["abbrev"],
            "abbrev_latin": row["abbrev_latin"],
        },
        "book": row["book"],
        "position": row["position"],
        "previous": prev,
        "note": row["note"],
    }
