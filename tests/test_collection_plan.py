"""The filer without a database: numerals, the sidecar line, book choice, the chain."""

from __future__ import annotations

import numpy as np
import pytest

from penish.collection.numerals import Request, format_line, parse_line, parse_number, roman
from penish.collection.plan import (
    Book,
    Leaf,
    Shape,
    Tuning,
    assign_books,
    choose_book,
    lay_books,
    plan_resort,
    positions,
    tag_overlap,
)

DIM = 12


def e(*ix: int, w: tuple[float, ...] | None = None) -> np.ndarray:
    v = np.zeros(DIM)
    for k, i in enumerate(ix):
        v[i] = 1.0 if w is None else w[k]
    return v / np.linalg.norm(v)


def shape(n_books: int = 7, cap: int = 77, **t: float | bool) -> Shape:
    return Shape([Book(i + 1, e(i)) for i in range(n_books)], cap, Tuning(**t))  # type: ignore[arg-type]


def leaf(image_id: int, book: int, position: int, vec: np.ndarray | None, **kw: bool) -> Leaf:
    return Leaf(image_id, book, position, vec, frozenset(), **kw)


# -- numerals and the sidecar line ------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "r"),
    [(1, "i"), (3, "iii"), (4, "iv"), (7, "vii"), (9, "ix"), (14, "xiv"), (77, "lxxvii")],
)
def test_roman_round_trip(n: int, r: str) -> None:
    assert roman(n) == r
    assert parse_number(r) == n
    assert parse_number(r.upper()) == n
    assert parse_number(str(n)) == n


@pytest.mark.parametrize("bad", ["", "0", "-3", "iiii", "vx", "iiv", "3a", "x1", "½", "١"])
def test_parse_number_refuses(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_number(bad)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("pansophicon", Request("pansophicon")),
        ("pansophicon\n", Request("pansophicon")),
        ("  Pansophicon  ", Request("pansophicon")),
        ("pansophicon iii", Request("pansophicon", 3)),
        ("pansophicon 3", Request("pansophicon", 3)),
        ("pansophicon III 14", Request("pansophicon", 3, 14)),
        ("pansophicon iii xiv", Request("pansophicon", 3, 14)),
        ("pansophicon 3, 14", Request("pansophicon", 3, 14)),
        ("\n\npansophicon vii 77\n\n", Request("pansophicon", 7, 77)),
    ],
)
def test_parse_line_valid(text: str, want: Request) -> None:
    assert parse_line(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n ",
        "pansophicon iii 14 extra",
        "pansophicon\nanother",
        "pansophicon three",
        "pansophicon iii fourteen",
        "pansophicon 0",
        "the pansophicon!",
        "��",
    ],
)
def test_parse_line_malformed(text: str) -> None:
    with pytest.raises(ValueError):
        parse_line(text)


def test_format_line() -> None:
    assert format_line(Request("pansophicon")) == "pansophicon"
    assert format_line(Request("pansophicon", 3)) == "pansophicon iii"
    assert format_line(Request("pansophicon", 3, 14)) == "pansophicon iii 14"
    assert parse_line(format_line(Request("pansophicon", 7, 77))) == Request("pansophicon", 7, 77)


# -- choosing a book ---------------------------------------------------------------


def test_empty_books_follow_the_anchors() -> None:
    s = shape()
    got = choose_book(s, [], e(3), frozenset())
    assert got is not None and got[0] == 4
    # Not just the right book: every other book is clearly behind.
    assert all(v < got[1][4] - 0.5 for k, v in got[1].items() if k != 4)


def test_leaves_take_over_from_the_anchor() -> None:
    # Book 1's anchor says one thing, but it has been filled with something else; a
    # picture of that something else belongs with the leaves, not with book 3's anchor
    # that is only slightly further off than book 1's leaves.
    s = Shape([Book(1, e(0)), Book(2, e(1)), Book(3, e(5, 6, w=(1.0, 0.7)))], 77)
    leaves = [leaf(i, 1, i, e(5)) for i in range(1, 7)]
    got = choose_book(s, leaves, e(5), frozenset())
    assert got is not None and got[0] == 1
    # With no leaves the same picture would have gone to book 3.
    alone = choose_book(s, [], e(5), frozenset())
    assert alone is not None and alone[0] == 3


def test_full_book_is_excluded() -> None:
    s = shape(n_books=2, cap=2)
    leaves = [leaf(1, 1, 1, e(0)), leaf(2, 1, 2, e(0))]
    got = choose_book(s, leaves, e(0), frozenset())
    assert got is not None and got[0] == 2
    assert set(got[1]) == {2}


def test_every_book_full() -> None:
    s = shape(n_books=2, cap=1)
    assert choose_book(s, [leaf(1, 1, 1, e(0)), leaf(2, 2, 1, e(1))], e(0), frozenset()) is None


def test_tag_hints_break_a_tie() -> None:
    s = Shape([Book(1, e(0), frozenset({"serpent"})), Book(2, e(1), frozenset({"moon"}))], 77)
    tie = e(0, 1)
    got = choose_book(s, [], tie, frozenset({"coiled serpent", "brass"}))
    assert got is not None and got[0] == 1
    got = choose_book(s, [], tie, frozenset({"moon"}))
    assert got is not None and got[0] == 2


def test_tag_overlap() -> None:
    assert tag_overlap(["coiled serpent", "brass"], ["serpent", "sun"]) == 0.5
    assert tag_overlap([], ["serpent"]) == 0.0
    assert tag_overlap(["x"], []) == 0.0
    assert tag_overlap(["Moon"], ["moon"]) == 1.0


def test_fill_pressure_spreads_a_near_tie() -> None:
    # Two books almost equally good; the fuller one loses the next picture.
    s = Shape([Book(1, e(0)), Book(2, e(0, 1, w=(1.0, 0.02)))], 10, Tuning(fill_beta=0.2))
    leaves = [leaf(i, 1, i, e(0)) for i in range(1, 9)]
    got = choose_book(s, leaves, e(0), frozenset())
    assert got is not None and got[0] == 2


# -- positions ---------------------------------------------------------------------


def _check(pos: dict[int, int], cap: int) -> None:
    assert len(set(pos.values())) == len(pos)
    assert all(1 <= p <= cap for p in pos.values())


def test_positions_spread_across_the_book() -> None:
    b = Book(1, e(0))
    leaves = [leaf(1, 1, 0, e(0)), leaf(2, 1, 0, e(0, 1)), leaf(3, 1, 0, e(1))]
    pos = positions(b, leaves, 77)
    _check(pos, 77)
    # The one nearest the anchor opens the book and the far one closes it.
    assert pos[1] == 1 and pos[3] == 77 and 1 < pos[2] < 77


def test_one_leaf_opens_the_book() -> None:
    assert positions(Book(1, e(0)), [leaf(9, 1, 40, e(3))], 77) == {9: 1}


def test_related_leaves_sit_together() -> None:
    rng = np.random.default_rng(1)
    a = [e(2) + rng.normal(scale=0.05, size=DIM) for _ in range(5)]
    b = [e(7) + rng.normal(scale=0.05, size=DIM) for _ in range(5)]
    vs = [v / np.linalg.norm(v) for v in a + b]
    order = [3, 7, 1, 9, 0, 5, 2, 8, 4, 6]
    leaves = [leaf(i + 1, 1, 0, vs[k]) for i, k in enumerate(order)]
    pos = positions(Book(1, e(2)), leaves, 77)
    _check(pos, 77)
    ranked = sorted(leaves, key=lambda x: pos[x.image_id])
    groups = ["a" if order[x.image_id - 1] < 5 else "b" for x in ranked]
    assert groups == ["a"] * 5 + ["b"] * 5
    # The jump between the two subjects is the widest gap in the book.
    slots = sorted(pos.values())
    gaps = [y - x for x, y in zip(slots, slots[1:], strict=False)]
    assert gaps.index(max(gaps)) == 4


def test_pinned_slots_are_fixed_points() -> None:
    b = Book(1, e(0))
    leaves = [
        leaf(1, 1, 40, e(5), book_pinned=True, pinned=True),
        leaf(2, 1, 41, e(0)),
        leaf(3, 1, 3, e(5)),
        leaf(4, 1, 0, e(5, 6)),
        leaf(5, 1, 0, e(0, 1)),
    ]
    pos = positions(b, leaves, 77)
    _check(pos, 77)
    assert pos[1] == 40
    assert all(p != 40 for k, p in pos.items() if k != 1)


def test_full_book_of_auto_leaves_fills_every_slot() -> None:
    rng = np.random.default_rng(2)
    leaves = []
    for i in range(1, 12):
        v = rng.normal(size=DIM)
        leaves.append(leaf(i, 1, 0, v / np.linalg.norm(v), pinned=i == 5, book_pinned=i == 5))
    leaves[4].position = 6
    pos = positions(Book(1, e(0)), leaves, 11)
    assert sorted(pos.values()) == list(range(1, 12))
    assert pos[5] == 6


def test_vectorless_leaf_stays_unless_a_pin_takes_its_slot() -> None:
    b = Book(1, e(0))
    pos = positions(b, [leaf(1, 1, 30, None), leaf(2, 1, 0, e(0))], 77)
    assert pos[1] == 30
    pos = positions(
        b, [leaf(1, 1, 30, None), leaf(2, 1, 30, e(0), book_pinned=True, pinned=True)], 77
    )
    assert pos[2] == 30 and pos[1] != 30


def test_two_pins_on_one_slot_refused() -> None:
    with pytest.raises(ValueError):
        positions(
            Book(1, e(0)),
            [
                leaf(1, 1, 3, e(0), book_pinned=True, pinned=True),
                leaf(2, 1, 3, e(1), book_pinned=True, pinned=True),
            ],
            77,
        )


def test_lay_books_touches_only_the_named_book() -> None:
    s = shape(n_books=2)
    leaves = [leaf(1, 1, 5, e(0)), leaf(2, 1, 9, e(0, 1)), leaf(3, 2, 33, e(1))]
    out = lay_books(s, leaves, {1: 1, 2: 1, 3: 2}, [1])
    assert out[3] == (2, 33)
    assert {out[1][0], out[2][0]} == {1}


# -- re-sorting ------------------------------------------------------------------


def _scrambled(seed: int = 0) -> tuple[Shape, list[Leaf]]:
    rng = np.random.default_rng(seed)
    s = shape()
    leaves = []
    for i in range(1, 50):
        true = (i % 7) + 1
        v = e(true - 1) * 2 + rng.normal(scale=0.4, size=DIM)
        leaves.append(leaf(i, ((i * 3) % 7) + 1, 0, v / np.linalg.norm(v)))
    for b in range(1, 8):
        for k, x in enumerate([x for x in leaves if x.book == b]):
            x.position = k + 1
    return s, leaves


def test_resort_puts_leaves_where_they_belong() -> None:
    s, leaves = _scrambled()
    plan = plan_resort(s, leaves)
    right = sum(1 for x in leaves if plan[x.image_id][0] == (x.image_id % 7) + 1)
    assert right >= 45
    for b in range(1, 8):
        _check({k: v[1] for k, v in plan.items() if v[0] == b}, 77)


def test_resort_never_moves_pins() -> None:
    s, leaves = _scrambled()
    leaves[0].pinned = leaves[0].book_pinned = True
    leaves[1].book_pinned = True
    before = [(x.book, x.position) for x in leaves]
    plan = plan_resort(s, leaves)
    assert plan[leaves[0].image_id] == before[0]
    assert plan[leaves[1].image_id][0] == before[1][0]


def test_resort_twice_moves_no_books_the_second_time() -> None:
    s, leaves = _scrambled(3)
    plan = plan_resort(s, leaves)
    assert any(plan[x.image_id][0] != x.book for x in leaves)
    for x in leaves:
        x.book, x.position = plan[x.image_id]
    again = assign_books(s, leaves)
    assert all(again[x.image_id] == x.book for x in leaves)


def test_margin_keeps_a_leaf_that_is_only_slightly_better_elsewhere() -> None:
    s = Shape([Book(1, e(0)), Book(2, e(0, 1, w=(1.0, 0.05)))], 77, Tuning(book_margin=0.03))
    # Closer to book 2's anchor than to book 1's, by less than the margin.
    x = leaf(1, 1, 1, e(0, 1, w=(1.0, 0.05)))
    assert assign_books(s, [x]) == {1: 1}
    s0 = Shape(s.books, 77, Tuning(book_margin=0.0))
    assert assign_books(s0, [x]) == {1: 2}


def test_tuning_from_json_ignores_rubbish() -> None:
    t = Tuning.from_json({"anchor_k": 5, "tag_alpha": "x", "fill_beta": -1, "weekly_resort": 1})
    assert t.anchor_k == 5.0 and t.tag_alpha == 0.05 and t.fill_beta == 0.05
    assert t.weekly_resort is False
    assert Tuning.from_json({"weekly_resort": True}).weekly_resort is True
    assert Tuning.from_json(None) == Tuning()
