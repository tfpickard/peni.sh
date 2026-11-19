"""Collections: the sidecar line, the filer's arithmetic, and filing against the database.

The arithmetic runs on hand-made unit vectors so each expectation can be read off the
geometry: anchors on axes, pictures near the axis of the book they should land in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import psycopg
import pytest

from penish.collections import Placement, format_spec, parse_number, parse_spec, roman, store
from penish.collections.filing import (
    BookSpace,
    Leaf,
    Params,
    choose_book,
    positions,
    resort_books,
    tag_overlap,
)
from penish.config import Settings
from penish.worker.sidecars import parse_group, stem_of

DIM = 16
CAP = 77


def e(i: int, dim: int = DIM) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


def near(i: int, seed: int, noise: float = 0.15, dim: int = DIM) -> np.ndarray:
    v = e(i, dim) + np.random.default_rng(seed).normal(scale=noise, size=dim).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def seven() -> list[BookSpace]:
    return [BookSpace(number=n, anchor=e(n - 1)) for n in range(1, 8)]


# -- the sidecar line --------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("pansophicon", Placement("pansophicon")),
        ("pansophicon iii", Placement("pansophicon", 3)),
        ("pansophicon iii 14", Placement("pansophicon", 3, 14)),
        ("Pansophicon III 14\n", Placement("pansophicon", 3, 14)),
        ("pansophicon 3 14", Placement("pansophicon", 3, 14)),
        ("pansophicon vii xiv", Placement("pansophicon", 7, 14)),
        ("\n  pansophicon   iv  \n\n", Placement("pansophicon", 4)),
    ],
)
def test_parse_spec(line: str, want: Placement) -> None:
    assert parse_spec(line) == want


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   \n",
        "pansophicon iiii",
        "pansophicon vv",
        "pansophicon 0",
        "pansophicon -1",
        "pansophicon three",
        "pansophicon iii 14 2",
        "pansophicon\npansophicon iii",
        "the pansophicon!",
    ],
)
def test_parse_spec_refuses(line: str) -> None:
    with pytest.raises(ValueError):
        parse_spec(line)


def test_roman_round_trip() -> None:
    for n in range(1, 400):
        assert parse_number(roman(n)) == n
        assert parse_number(roman(n).upper()) == n
    assert [roman(n) for n in range(1, 8)] == ["i", "ii", "iii", "iv", "v", "vi", "vii"]


def test_format_spec_round_trips() -> None:
    for p in (
        Placement("pansophicon"),
        Placement("pansophicon", 3),
        Placement("pansophicon", 7, 77),
    ):
        assert parse_spec(format_spec(p)) == p
    assert format_spec(Placement("pansophicon", 3, 14)) == "pansophicon iii 14"


def test_collection_sidecar_is_a_sidecar(tmp_path: Path) -> None:
    assert stem_of("x_collection.txt") == ("x", "_collection")
    (tmp_path / "x.png").write_bytes(b"not decoded here")
    # Not utf-8: still read, so ingest can report it instead of failing the picture.
    (tmp_path / "x_collection.txt").write_bytes(b"pansophicon \xff\xfe\n")
    g = parse_group(tmp_path)
    assert g.values["collection"].startswith("pansophicon")
    with pytest.raises(ValueError):
        parse_spec(g.values["collection"])


# -- choosing a book ---------------------------------------------------------------


def test_empty_books_follow_their_anchors() -> None:
    books = seven()
    for n in range(1, 8):
        assert choose_book(near(n - 1, n), [], books, CAP, Params()) == n


def test_a_filled_book_becomes_its_leaves() -> None:
    # Book i says creatures (axis 0) but holds ten pictures along axis 8; book ii says
    # axis 1. A picture along axis 8 belongs with the leaves, not with any anchor.
    books = seven()
    for k in range(10):
        books[0].add(near(8, 100 + k))
    assert choose_book(near(8, 1), [], books, CAP, Params()) == 1


def test_full_books_are_not_candidates() -> None:
    books = seven()
    vec = near(2, 7)
    for _ in range(CAP):
        books[2].add(near(2, 3))
    got = choose_book(vec, [], books, CAP, Params())
    assert got is not None and got != 3
    for b in books:
        b.n = CAP
    assert choose_book(vec, [], books, CAP, Params()) is None


def test_tag_hints_and_fill_pressure_break_near_ties() -> None:
    a, b = BookSpace(1, anchor=e(0)), BookSpace(2, anchor=e(1))
    vec = (e(0) + e(1)) / np.sqrt(2)
    p = Params(alpha=0.05, beta=0.05)
    assert choose_book(vec, ["alchemy lab"], [a, b], CAP, p) == 1
    b.hints = frozenset({"alchemy"})
    assert choose_book(vec, ["alchemy lab"], [a, b], CAP, p) == 2
    # Equal hints; the fuller book loses a dead heat.
    a.hints = frozenset({"alchemy"})
    for _ in range(60):
        a.add(vec)
        b.add(vec)
    b.total, b.n = None, 0
    for _ in range(10):
        b.add(vec)
    assert choose_book(vec, [], [a, b], CAP, p) == 2


def test_tag_overlap() -> None:
    assert tag_overlap(["Gold leaf", "saint"], ["gold", "saint", "relic", "icon"]) == 0.5
    assert tag_overlap(["x"], []) == 0.0


# -- positions -----------------------------------------------------------------


def _check(slots: list[int], fixed: list[int | None], cap: int = CAP) -> None:
    assert len(set(slots)) == len(slots)
    assert all(1 <= s <= cap for s in slots)
    for s, f in zip(slots, fixed, strict=True):
        if f is not None:
            assert s == f


def test_positions_spread_over_the_book() -> None:
    assert positions([e(0)], [None], CAP) == [1]
    two = positions([e(0), e(1)], [None, None], CAP, anchor=e(1))
    assert sorted(two) == [1, 77] and two[1] == 1


def test_positions_keep_neighbours_together() -> None:
    vecs = [near(0, 1), near(5, 2), near(0, 3), near(5, 4), near(0, 5)]
    slots = positions(vecs, [None] * 5, CAP, anchor=e(0))
    _check(slots, [None] * 5)
    zeros = sorted(slots[i] for i in (0, 2, 4))
    fives = sorted(slots[i] for i in (1, 3))
    # The jump between subjects is where the gap falls: one run each, not interleaved.
    assert zeros[-1] < fives[0]


@pytest.mark.parametrize("seed", range(5))
def test_positions_unique_in_range_with_pins(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(5, CAP + 1))
    vecs = [near(int(rng.integers(0, DIM)), seed * 1000 + i, 0.4) for i in range(n)]
    pinned_slots = rng.choice(np.arange(1, CAP + 1), size=min(4, n), replace=False)
    fixed: list[int | None] = [None] * n
    for i, s in enumerate(pinned_slots):
        fixed[i * 2 % n] = int(s)
    if len({f for f in fixed if f is not None}) != sum(f is not None for f in fixed):
        pytest.skip("two pins landed on one leaf")
    _check(positions(vecs, fixed, CAP), fixed)


def test_positions_full_book_with_pins() -> None:
    vecs = [near(i % DIM, i, 0.3) for i in range(CAP)]
    fixed: list[int | None] = [None] * CAP
    fixed[3], fixed[50], fixed[10] = 77, 1, 40
    slots = positions(vecs, fixed, CAP)
    _check(slots, fixed)
    assert sorted(slots) == list(range(1, CAP + 1))


def test_positions_refuse_colliding_pins() -> None:
    with pytest.raises(ValueError):
        positions([e(0), e(1)], [5, 5], CAP)


# -- re-sorting books -------------------------------------------------------------


def _scatter(seed: int, n: int = 60) -> list[Leaf]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        axis = int(rng.integers(0, 7))
        out.append(
            Leaf(
                image_id=i + 1,
                vec=near(axis, seed * 100 + i, 0.5),
                # Deliberately filed at random, so the first re-sort has work to do.
                book=int(rng.integers(1, 8)),
                position=0,
                pinned_book=i % 9 == 0,
            )
        )
    return out


@pytest.mark.parametrize("seed", range(4))
def test_resort_twice_moves_nothing_the_second_time(seed: int) -> None:
    leaves = _scatter(seed)
    first = resort_books(leaves, seven(), CAP, Params())
    assert first
    for lf in leaves:
        lf.book = first.get(lf.image_id, lf.book)
    assert resort_books(leaves, seven(), CAP, Params()) == {}


def test_resort_never_moves_a_pinned_book() -> None:
    leaves = _scatter(1)
    pinned = {lf.image_id: lf.book for lf in leaves if lf.pinned_book}
    moved = resort_books(leaves, seven(), CAP, Params())
    assert not set(moved) & set(pinned)


def test_margin_holds_a_leaf_in_a_nearly_as_good_book() -> None:
    vec = (e(0) * 0.99 + e(1) * 1.0).astype(np.float32)
    vec /= np.linalg.norm(vec)
    leaf = Leaf(image_id=1, vec=vec, book=1, position=1)
    books = [BookSpace(1, anchor=e(0)), BookSpace(2, anchor=e(1))]
    assert resort_books([leaf], books, CAP, Params(margin=0.03)) == {}
    assert resort_books([leaf], books, CAP, Params(margin=0.0)) == {1: 2}


# -- against the database ----------------------------------------------------------


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


def sql(dsn: str, q: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(q.encode(), args)
        return cur.fetchall() if cur.description else []


def picture(
    dsn: str, n: int, vec: np.ndarray, *, status: str = "live", tags: tuple[str, ...] = ()
) -> int:
    rows = sql(
        dsn,
        "insert into images (sha256, pub_sha, slug, width, height, status, published_at)"
        " values (%s, %s, %s, 100, 100, %s, now()) returning id",
        f"{n:064x}",
        f"{n:064x}",
        f"pic-{n}",
        status,
    )
    iid = int(rows[0][0])
    sql(
        dsn,
        "insert into embeddings (image_id, kind, vec, model, dim) values (%s, 'description',"
        " %s::vector, 'test/embed', %s)",
        iid,
        str([float(x) for x in vec]),
        DIM,
    )
    for t in tags:
        sql(dsn, "insert into tags (image_id, tag, source) values (%s, %s, 'owner')", iid, t)
    return iid


def anchor_axes(dsn: str) -> None:
    """Book n's anchor on axis n-1, embedded under the test model."""
    for n in range(1, 8):
        sql(
            dsn,
            "update collection_books set anchor_vec = %s::vector, anchor_model = 'test/embed'"
            " where number = %s",
            str([float(x) for x in e(n - 1)]),
            n,
        )


async def coll(p: Any) -> store.Collection:
    async with p.connection() as conn:
        c = await store.get(conn, slug="pansophicon")
    assert c is not None
    return c


async def file(
    p: Any, settings: Settings, iid: int, book: int | None = None, pos: int | None = None
) -> tuple[int, int] | None:
    c = await coll(p)
    async with p.connection() as conn, conn.transaction():
        await store.request(conn, c, iid, book, pos)
        return await store.file_one(conn, c, iid, settings, Params())


async def where(p: Any) -> dict[int, tuple[int, int, bool, bool]]:
    async with p.connection() as conn:
        cur = await conn.execute(
            "select l.image_id, b.number, l.position, l.pinned_book, l.pinned_position"
            " from collection_leaves l join collection_books b on b.id = l.book_id"
        )
        return {
            r["image_id"]: (r["number"], r["position"], r["pinned_book"], r["pinned_position"])
            for r in await cur.fetchall()
        }


async def test_seed(p: Any) -> None:
    c = await coll(p)
    assert (c.title, c.abbrev, c.abbrev_latin) == ("Πανσοφικὸν τῶν πάντων", "ΠΤΠ", "PTP")
    assert (c.book_count, c.leaves_per_book, c.published) == (7, 77, False)
    assert c.preamble.startswith("Acquired incomplete.")
    async with p.connection() as conn:
        books = await store.books(conn, c)
    assert [b["number"] for b in books] == list(range(1, 8))
    assert all(b["anchor_text"] and b["tag_hints"] and b["title"] is None for b in books)
    assert "\u2014" not in c.preamble and all("\u2014" not in b["anchor_text"] for b in books)


async def test_filing_on_anchors_alone(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    iid = picture(clean_db, 1, near(4, 1))
    assert await file(p, settings, iid) == (5, 1)
    assert (await where(p))[iid] == (5, 1, False, False)
    assert sql(clean_db, "select count(*) from collection_requests") == [(0,)]
    # Placing a new leaf is not a move.
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]


async def test_filing_waits_for_publish(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    iid = picture(clean_db, 1, near(0, 1), status="processing")
    with pytest.raises(store.NotReady):
        await file(p, settings, iid)


async def test_owner_book_and_leaf_are_pinned(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    a = picture(clean_db, 1, near(0, 1))
    b = picture(clean_db, 2, near(0, 2))
    assert await file(p, settings, a, 3, 14) == (3, 14)
    assert (await file(p, settings, b, 3))[0] == 3  # type: ignore[index]
    got = await where(p)
    assert got[a] == (3, 14, True, True)
    assert got[b][0] == 3 and got[b][2:] == (True, False)


async def test_taken_leaf_is_refused_and_kept_as_an_error(
    p: Any, clean_db: str, settings: Settings
) -> None:
    anchor_axes(clean_db)
    a = picture(clean_db, 1, near(0, 1))
    b = picture(clean_db, 2, near(0, 2))
    await file(p, settings, a, 2, 9)
    with pytest.raises(store.FilingError, match="leaf 9 is taken"):
        await file(p, settings, b, 2, 9)


async def test_a_full_book_is_skipped(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    sql(clean_db, "update collections set leaves_per_book = 2")
    for n in (1, 2):
        assert (await file(p, settings, picture(clean_db, n, near(0, n))))[0] == 1  # type: ignore[index]
    third = await file(p, settings, picture(clean_db, 3, near(0, 3)))
    assert third is not None and third[0] != 1
    with pytest.raises(store.FilingError, match="book i is full"):
        await file(p, settings, picture(clean_db, 4, near(0, 4)), 1)


async def test_every_book_full(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    sql(clean_db, "update collections set leaves_per_book = 1")
    for n in range(1, 8):
        await file(p, settings, picture(clean_db, n, near(n - 1, n)))
    with pytest.raises(store.FilingError, match="every book is full"):
        await file(p, settings, picture(clean_db, 8, near(0, 8)))


async def test_pinned_leaves_never_move(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    fixed = picture(clean_db, 1, near(5, 1))
    bound = picture(clean_db, 2, near(6, 2))
    await file(p, settings, fixed, 1, 40)
    # Pinned to book i though everything about it says book vii.
    await file(p, settings, bound, 1)
    for n in range(3, 23):
        await file(p, settings, picture(clean_db, n, near(n % 3, n, 0.3)))
    got = await where(p)
    assert got[fixed][:2] == (1, 40)
    assert got[bound][0] == 1
    c = await coll(p)
    for _ in range(2):
        async with p.connection() as conn, conn.transaction():
            await store.resort(conn, c, settings, Params(), dry_run=False)
        after = await where(p)
        assert after[fixed][:2] == (1, 40)
        assert after[bound][0] == 1
    slots = [(b, s) for b, s, _, _ in after.values()]
    assert len(set(slots)) == len(slots)
    assert all(1 <= s <= 77 for _, s in slots)


async def test_resort_twice_is_stable_and_dry_run_writes_nothing(
    p: Any, clean_db: str, settings: Settings
) -> None:
    anchor_axes(clean_db)
    rng = np.random.default_rng(5)
    for n in range(1, 41):
        await file(p, settings, picture(clean_db, n, near(int(rng.integers(0, 7)), n, 0.6)))
    # Drift the vectors so the books no longer fit, as a re-embed would.
    for n in range(1, 41):
        sql(
            clean_db,
            "update embeddings set vec = %s::vector where image_id = (select id from images"
            " where slug = %s)",
            str([float(x) for x in near(int(rng.integers(0, 7)), 500 + n, 0.6)]),
            f"pic-{n}",
        )
    c = await coll(p)
    before = await where(p)
    async with p.connection() as conn, conn.transaction():
        dry = await store.resort(conn, c, settings, Params(), dry_run=True)
    assert dry and await where(p) == before
    async with p.connection() as conn, conn.transaction():
        moves = await store.resort(conn, c, settings, Params(), dry_run=False)
    assert [(m["image_id"], m["to"]) for m in moves] == [(m["image_id"], m["to"]) for m in dry]
    assert any(m["from"][0] != m["to"][0] for m in moves)
    async with p.connection() as conn, conn.transaction():
        again = await store.resort(conn, c, settings, Params(), dry_run=False)
    assert not [m for m in again if m["from"][0] != m["to"][0]]
    n_hist = sql(clean_db, "select count(*) from collection_leaf_history where reason = 'resort'")
    assert n_hist == [(len(moves) + len(again),)]


async def test_every_move_is_written_down(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    ids = [picture(clean_db, n, near(0, n, 0.4)) for n in range(1, 6)]
    for iid in ids:
        await file(p, settings, iid)
    hist = sql(
        clean_db,
        "select image_id, from_book, from_position, to_book, to_position, reason"
        " from collection_leaf_history order by id",
    )
    now = await where(p)
    # Every auto shift recorded, and each leaf's latest row ends where it is.
    assert hist and {h[5] for h in hist} == {"auto"}
    latest = {h[0]: (h[3], h[4]) for h in hist}
    for iid, to in latest.items():
        assert now[iid][:2] == to

    c = await coll(p)
    async with p.connection() as conn, conn.transaction():
        moves = await store.move(conn, c, ids[0], 6, 70, settings)
    owner = [m for m in moves if m["reason"] == "owner"]
    assert [m["image_id"] for m in owner] == [ids[0]] and owner[0]["to"] == (6, 70)
    assert (await where(p))[ids[0]] == (6, 70, True, True)

    async with p.connection() as conn, conn.transaction():
        assert await store.unfile(conn, c, ids[0])
    row = sql(
        clean_db,
        "select from_book, from_position, to_book, reason from collection_leaf_history"
        " where image_id = %s order by id desc limit 1",
        ids[0],
    )
    assert row == [(6, 70, None, "owner")]
    assert ids[0] not in await where(p)
    assert sql(clean_db, "select status from images where id = %s", ids[0]) == [("live",)]


async def test_pin_and_unpin_move_nothing(p: Any, clean_db: str, settings: Settings) -> None:
    anchor_axes(clean_db)
    iid = picture(clean_db, 1, near(0, 1))
    await file(p, settings, iid)
    c = await coll(p)
    async with p.connection() as conn, conn.transaction():
        assert await store.pin(conn, c, iid, True)
    assert (await where(p))[iid] == (1, 1, True, True)
    async with p.connection() as conn, conn.transaction():
        assert await store.pin(conn, c, iid, False)
    assert (await where(p))[iid] == (1, 1, False, False)


def test_a_lone_pin_leaves_the_long_side_to_the_rest() -> None:
    vecs = [e(0), near(1, 1), near(1, 2), near(1, 3)]
    slots = positions(vecs, [14, None, None, None], CAP)
    assert slots[0] == 14
    # Both runs touch only the pin, so every leaf ties; the longer run takes them.
    assert all(s > 14 for s in slots[1:]) and max(slots) == 77
