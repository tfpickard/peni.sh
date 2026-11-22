"""The filer without a database: numerals, the sidecar line, book choice, the chain."""

from __future__ import annotations

import numpy as np
import pytest

from penish.collection.numerals import (
    LOOSE,
    Request,
    book_name,
    format_line,
    parse_line,
    parse_number,
    roman,
)
from penish.collection.plan import (
    Book,
    Leaf,
    Shape,
    Tuning,
    accession,
    assign_books,
    centroid,
    choose_book,
    lay_books,
    plan_resort,
    positions,
    tag_overlap,
    weigh,
    weigh_leaf,
    weigh_leaves,
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
        # Incertae sedis: book 0, as `0` or `n` (nulla), with no cap on its leaf.
        ("pansophicon 0", Request("pansophicon", LOOSE)),
        ("pansophicon n", Request("pansophicon", LOOSE)),
        ("pansophicon N 4", Request("pansophicon", LOOSE, 4)),
        ("pansophicon 0 120", Request("pansophicon", LOOSE, 120)),
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
        "pansophicon iii 0",
        "pansophicon iii n",
        "pansophicon nn",
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
    assert format_line(Request("pansophicon", LOOSE, 4)) == "pansophicon n 4"
    assert parse_line(format_line(Request("pansophicon", LOOSE))) == Request("pansophicon", LOOSE)


def test_book_name() -> None:
    assert book_name(3, "incertae sedis") == "iii"
    assert book_name(LOOSE, "of uncertain place") == "of uncertain place"
    # The row's title gone by hand: the line still names something.
    assert book_name(LOOSE, None) == "incertae sedis"


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


def test_tag_overlap_reads_hyphens_as_spaces() -> None:
    # Live tags are hyphenated; none has a space.
    assert tag_overlap(["zodiac-man", "woodcut"], ["zodiac", "dream"]) == 0.5
    assert tag_overlap(["zodiac-man"], ["zodiac-man"]) == 1.0
    assert tag_overlap(["Moon"], ["moon"]) == 1.0
    # One tag meeting two hints is a full match, not a double one.
    assert tag_overlap(["alchemy lab"], ["alchemy", "lab", "flask"]) == 1.0


def test_fill_pressure_spreads_a_near_tie() -> None:
    # Two books almost equally good; the fuller one loses the next picture.
    s = Shape([Book(1, e(0)), Book(2, e(0, 1, w=(1.0, 0.02)))], 10, Tuning(fill_beta=0.2))
    leaves = [leaf(i, 1, i, e(0)) for i in range(1, 9)]
    got = choose_book(s, leaves, e(0), frozenset())
    assert got is not None and got[0] == 2


# -- the scores in parts -------------------------------------------------------------


def _old_choose_book(
    s: Shape, leaves: list[Leaf], vec: np.ndarray, tags: frozenset[str]
) -> tuple[int, dict[int, float]] | None:
    """choose_book as it was before the parts were kept, formula and all."""
    t = s.tuning
    out: dict[int, float] = {}
    for b in s.books:
        mine = [x for x in leaves if x.book == b.number]
        if len(mine) >= s.cap:
            continue
        vecs = [x.vec for x in mine if x.vec is not None]
        c = centroid(b.anchor, np.sum(vecs, axis=0) if vecs else None, len(vecs), t.anchor_k)
        cos = float(np.dot(vec, c)) if c is not None else 0.0
        out[b.number] = (
            cos + t.tag_alpha * tag_overlap(tags, b.hints) - t.fill_beta * (len(mine) / s.cap) ** 2
        )
    if not out:
        return None
    return min(out, key=lambda n: (-out[n], n)), out


def _random_case(seed: int) -> tuple[Shape, list[Leaf], np.ndarray, frozenset[str]]:
    rng = np.random.default_rng(seed)
    hints = ["serpent", "moon", "lab", "zodiac", "flask", "saint", "bath"]
    books = [
        Book(n, e(n - 1) if n != 4 else None, frozenset(rng.choice(hints, 2, replace=False)))
        for n in range(1, 8)
    ]
    s = Shape(books, 6, Tuning(anchor_k=float(rng.integers(0, 12)), tag_alpha=0.3, fill_beta=0.2))
    leaves = []
    for i in range(1, int(rng.integers(0, 30))):
        v = rng.normal(size=DIM)
        vec = None if i % 9 == 0 else v / np.linalg.norm(v)
        leaves.append(Leaf(i, int(rng.integers(1, 8)), i, vec, frozenset()))
    # Never more than a book holds.
    leaves = [x for x in leaves if sum(1 for y in leaves if y.book == x.book) <= s.cap]
    v = rng.normal(size=DIM)
    tags = frozenset(str(t) for t in rng.choice([*hints, "zodiac-man", "coiled serpent"], 3))
    return s, leaves, v / np.linalg.norm(v), tags


@pytest.mark.parametrize("seed", range(40))
def test_parts_add_up_and_the_choice_is_unchanged(seed: int) -> None:
    s, leaves, vec, tags = _random_case(seed)
    got = choose_book(s, leaves, vec, tags)
    old = _old_choose_book(s, leaves, vec, tags)
    w = weigh(s, leaves, vec, tags)
    if old is None:
        assert got is None and w is None
        return
    assert got is not None and w is not None
    assert got[0] == old[0] == w.best
    assert set(got[1]) == set(old[1]) == set(w.terms)
    for n, t in w.terms.items():
        assert t.cos + t.tag + t.fill == t.total == got[1][n]
        assert got[1][n] == pytest.approx(old[1][n], abs=1e-12)
        assert t.tag >= 0 and t.fill <= 0
    if w.runner_up is not None:
        assert w.margin == w.totals[w.best] - w.totals[w.runner_up] >= 0
        assert w.runner_up == max(
            (n for n in w.terms if n != w.best), key=lambda n: (w.totals[n], -n)
        )


def test_terms_name_each_part() -> None:
    s = Shape([Book(1, e(0), frozenset({"moon"})), Book(2, e(1))], 4, Tuning(tag_alpha=0.1))
    w = weigh(s, [leaf(9, 1, 1, e(5)), leaf(8, 1, 2, e(5))], e(0), frozenset({"moon"}))
    assert w is not None and w.best == 1 and w.runner_up == 2
    one = w.terms[1]
    assert one.tag == pytest.approx(0.1)
    assert one.fill == pytest.approx(-0.05 * (2 / 4) ** 2)
    assert w.terms[2].cos == pytest.approx(0.0) and w.terms[2].fill == 0.0
    assert w.lead(2) == pytest.approx(-(w.margin or 0))
    js = w.to_json()
    assert [b["number"] for b in js["books"]] == [1, 2] and js["best"] == 1


def test_weigh_leaf_leaves_it_out_of_its_own_book() -> None:
    s = Shape([Book(1, e(0)), Book(2, e(2))], 2, Tuning(anchor_k=1.0))
    # Its book is full with it; without it, the book is one other leaf and has room.
    me = leaf(1, 1, 1, e(1))
    other = leaf(2, 1, 2, e(0))
    w = weigh_leaf(s, [me, other], 1)
    assert w is not None and set(w.terms) == {1, 2}
    # Anchor and the other leaf are the same direction, so the centroid is that direction:
    # no pull at all toward the leaf itself.
    assert w.terms[1].cos == pytest.approx(0.0)
    assert w.terms[1].fill == pytest.approx(-0.05 * (1 / 2) ** 2)
    # With everyone placed as a plan would place them.
    moved = weigh_leaf(s, [me, other], 1, {1: 2, 2: 2})
    assert moved is not None and moved.terms[1].fill == 0.0
    assert moved.terms[2].fill == pytest.approx(-0.05 * (1 / 2) ** 2)
    assert weigh_leaf(s, [leaf(3, 1, 1, None)], 3) is None


@pytest.mark.parametrize("seed", range(10))
def test_weighing_every_leaf_at_once_is_weighing_each_alone(seed: int) -> None:
    s, leaves, _, _ = _random_case(seed)
    every = weigh_leaves(s, leaves)
    for x in leaves:
        alone = None
        if x.vec is not None:
            others = [y for y in leaves if y.image_id != x.image_id]
            alone = weigh(s, others, x.vec, x.tags, own=x.book)
        got = every[x.image_id]
        assert (got is None) == (alone is None)
        if got is not None and alone is not None:
            assert set(got.terms) == set(alone.terms)
            for n in got.terms:
                assert got.totals[n] == pytest.approx(alone.totals[n], abs=1e-9)


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


def test_a_lone_pin_leaves_the_long_side_to_the_rest() -> None:
    # No anchor, so both gaps beside the pin are equally near every free leaf.
    book = Book(1, None)
    pin = leaf(1, 1, 14, e(0), pinned=True, book_pinned=True)
    free = [leaf(10 + k, 1, 0, e(1, k + 2, w=(1.0, 0.2))) for k in range(4)]
    pos = positions(book, [pin, *free], 77)
    _check(pos, 77)
    assert pos[1] == 14
    assert all(pos[x.image_id] > 14 for x in free) and max(pos.values()) == 77


def test_a_tie_between_gaps_goes_to_the_roomier_one() -> None:
    # Two pins on one vector: the gaps on either side of each tie. The anchor points
    # elsewhere, so it does not pull anyone into the opening gap.
    book = Book(1, e(5))
    pins = [
        leaf(1, 1, 20, e(0), pinned=True, book_pinned=True),
        leaf(2, 1, 60, e(0), pinned=True, book_pinned=True),
    ]
    free = [leaf(10 + k, 1, 0, e(1, k + 6, w=(1.0, 0.2))) for k in range(4)]
    pos = positions(book, [*pins, *free], 77)
    _check(pos, 77)
    assert all(20 < pos[x.image_id] < 60 for x in free)


# -- the loose leaves (incertae sedis) ----------------------------------------------


def test_loose_leaves_fill_no_book_and_pull_none() -> None:
    s = shape(n_books=2, cap=2)
    # Book 1 is full; two loose leaves sit exactly on book 2's subject.
    leaves = [leaf(1, 1, 1, e(0)), leaf(2, 1, 2, e(0))]
    loose = [leaf(3, LOOSE, 1, e(5)), leaf(4, LOOSE, 2, e(5))]
    w = weigh(s, leaves + loose, e(1), frozenset())
    assert w is not None and set(w.terms) == {2}
    assert w.terms[2].fill == 0.0
    assert w.terms[2] == weigh(s, leaves, e(1), frozenset()).terms[2]  # type: ignore[union-attr]
    # Every book full: loose leaves do not open one.
    full = [*leaves, leaf(5, 2, 1, e(1)), leaf(6, 2, 2, e(1))]
    assert weigh(s, full + loose, e(1), frozenset()) is None


def test_a_loose_leaf_is_weighed_against_the_books_only() -> None:
    s = shape(n_books=3)
    leaves = [leaf(1, LOOSE, 4, e(1)), leaf(2, 2, 1, e(1))]
    w = weigh_leaf(s, leaves, 1)
    assert w is not None and set(w.terms) == {1, 2, 3} and w.best == 2
    # It is in no book, so it leads none.
    assert w.lead(LOOSE) is None


def test_a_loose_leaf_has_no_weighing_when_every_book_is_full() -> None:
    s = shape(n_books=1, cap=1)
    leaves = [leaf(1, 1, 1, e(0)), leaf(2, LOOSE, 1, e(0))]
    assert weigh_leaf(s, leaves, 2) is None
    # The full book's own leaf is still weighed: it may stay where it is.
    assert weigh_leaf(s, leaves, 1) is not None


def test_resort_never_moves_a_leaf_into_or_out_of_the_loose_leaves() -> None:
    s, leaves = _scrambled()
    # Loose leaves that would score clearly in book 1, and one with no business anywhere.
    loose = [leaf(100, LOOSE, 1, e(0)), leaf(101, LOOSE, 3, e(0)), leaf(102, LOOSE, 2, e(11))]
    plan = plan_resort(s, leaves + loose)
    for x in loose:
        assert plan[x.image_id] == (LOOSE, x.position)
    assert all(plan[x.image_id][0] != LOOSE for x in leaves)
    # And their presence changes nothing about where the rest go.
    assert {i: plan[i] for i in plan if i < 100} == plan_resort(s, leaves)
    where = assign_books(s, leaves + loose)
    assert all(where[x.image_id] == LOOSE for x in loose)


def test_accession_numbers_in_order_of_arrival() -> None:
    there = [leaf(5, LOOSE, 1, e(0)), leaf(9, LOOSE, 4, e(1))]
    # Arrivals carry no number; they go after the highest, in image id order.
    got = accession([*there, leaf(7, LOOSE, 0, None), leaf(3, LOOSE, 0, e(2))])
    assert got == {5: 1, 9: 4, 3: 5, 7: 6}


def test_accession_gives_an_owner_pin_its_number() -> None:
    there = [leaf(5, LOOSE, 1, e(0)), leaf(9, LOOSE, 2, e(1))]
    got = accession([*there, leaf(3, LOOSE, 2, e(2), book_pinned=True, pinned=True)])
    # The leaf already at 2 goes to the end; the gap the pin filled is not closed.
    assert got == {3: 2, 5: 1, 9: 3}
    with pytest.raises(ValueError):
        accession(
            [
                leaf(1, LOOSE, 2, e(0), book_pinned=True, pinned=True),
                leaf(2, LOOSE, 2, e(1), book_pinned=True, pinned=True),
            ]
        )


def test_lay_books_takes_leaves_into_and_out_of_the_loose_leaves() -> None:
    s = shape(n_books=2)
    leaves = [
        leaf(1, LOOSE, 1, e(0)),
        leaf(2, LOOSE, 2, e(0)),
        leaf(3, LOOSE, 3, e(0)),
        leaf(4, 1, 5, e(0)),
        leaf(5, 1, 9, e(0, 1)),
    ]
    # 2 leaves for book 2 and 4 is made loose: 2's number stays a gap, 4 takes 4.
    out = lay_books(s, leaves, {1: LOOSE, 2: 2, 3: LOOSE, 4: LOOSE, 5: 1}, [], settled=True)
    assert out[1] == (LOOSE, 1) and out[3] == (LOOSE, 3)
    assert out[4] == (LOOSE, 4)
    assert out[2][0] == 2 and out[5] == (1, 9)
    # Named on its own, the loose leaves are not laid again.
    again = lay_books(s, leaves, {x.image_id: x.book for x in leaves}, [LOOSE, 1])
    assert [again[i] for i in (1, 2, 3)] == [(LOOSE, 1), (LOOSE, 2), (LOOSE, 3)]
