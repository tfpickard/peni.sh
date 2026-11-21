"""The pure parts of scripts/ptp_filing_report.py, on synthetic vectors. No embedder, no
database."""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from penish.collection.plan import Book, Leaf, Shape, Tuning

_spec = importlib.util.spec_from_file_location(
    "ptp_filing_report",
    Path(__file__).resolve().parent.parent / "scripts" / "ptp_filing_report.py",
)
assert _spec and _spec.loader
rep = importlib.util.module_from_spec(_spec)
# Its dataclasses look their module up while the class is built.
sys.modules[_spec.name] = rep
_spec.loader.exec_module(rep)

DIM = 6


def e(i: int) -> np.ndarray:
    v = np.zeros(DIM)
    v[i] = 1.0
    return v


def unit(v: np.ndarray) -> np.ndarray:
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def plain(
    anchors: list[np.ndarray], k: float = 3.0, alpha: float = 0.0, beta: float = 0.0
) -> Shape:
    books = [Book(n + 1, a, frozenset()) for n, a in enumerate(anchors)]
    return Shape(books, 77, Tuning(anchor_k=k, tag_alpha=alpha, fill_beta=beta, book_margin=0.03))


# -- gold and accuracy ------------------------------------------------------------


def test_gold_parses_several_books_and_the_flag() -> None:
    g = rep.load_gold(
        {
            "confirmed": False,
            "leaves": {
                "a": {"books": ["iv"], "why": "zodiac man"},
                "b": {"books": ["i", "vii"], "why": "either"},
            },
        }
    )
    assert g.confirmed is False
    assert g.books == {"a": frozenset({4}), "b": frozenset({1, 7})}
    assert g.why["a"] == "zodiac man"
    assert rep.load_gold({"confirmed": True, "leaves": {}}).confirmed is True
    with pytest.raises(ValueError):
        rep.parse_book("viii")


def test_accuracy_counts_any_gold_book_as_right() -> None:
    gold = {"a": frozenset({1}), "b": frozenset({1, 7}), "c": frozenset({2})}
    t = rep.accuracy({"a": 1, "b": 7, "c": 3}, gold, ["a", "b", "c"])
    assert (t.right, t.n) == (2, 3)
    assert t.rights == ["a", "b"]
    assert t.wrongs == [("c", 3)]
    # Only the slugs asked for, and only those with both a choice and a label.
    t = rep.accuracy({"a": 2, "b": 1}, gold, ["a", "b", "zz"])
    assert (t.right, t.n) == (1, 2)


def test_chance_weights_leaves_with_two_books() -> None:
    gold = {"a": frozenset({1}), "b": frozenset({1, 7})}
    assert rep.chance(gold, ["a", "b"]) == pytest.approx(3 / 7)


# -- scores ----------------------------------------------------------------------


def test_pick_gives_runner_up_margin_and_the_lower_book_on_a_tie() -> None:
    p = rep.pick({1: 0.2, 2: 0.5, 3: 0.4})
    assert (p.book, p.runner_up) == (2, 3)
    assert p.margin == pytest.approx(0.1)
    tie = rep.pick({4: 0.3, 2: 0.3, 1: 0.1})
    assert (tie.book, tie.runner_up, tie.margin) == (2, 4, 0.0)


def test_breakdown_terms_sum_to_the_filers_total() -> None:
    hints = frozenset({"zodiac", "map"})
    shape = Shape(
        [Book(1, e(0), hints), Book(2, e(1), frozenset())],
        10,
        Tuning(anchor_k=3.0, tag_alpha=0.05, fill_beta=0.05),
    )
    leaves = [Leaf(10, 1, 1, unit(e(0) + e(2))), Leaf(11, 1, 2, e(0))]
    vec = unit(e(0) + e(1))
    terms = rep.breakdown(shape, leaves, vec, frozenset({"zodiac"}))
    for t in terms.values():
        assert t.cos + t.tag + t.fill == pytest.approx(t.total)
    # Book 2 is all anchor and empty: plain cosine, no tag, no fill.
    assert terms[2].cos == pytest.approx(float(np.dot(vec, e(1))))
    assert terms[2].tag == pytest.approx(0.0)
    assert terms[2].fill == pytest.approx(0.0)
    # The one tag matches a hint (overlap coefficient 1); two of ten slots are filled.
    assert terms[1].tag == pytest.approx(0.05 * 1 / 1)
    assert terms[1].fill == pytest.approx(-0.05 * (2 / 10) ** 2)


def test_leave_one_out_drops_the_leaf_from_its_own_book() -> None:
    # Book 1 holds only the leaf, which looks like neither anchor; book 2's anchor is
    # nearer the leaf than book 1's. With itself in book 1 the leaf stays there.
    shape = plain([e(0), unit(e(1) + e(2))], k=0.5)
    leaf = Leaf(1, 1, 1, e(1))
    with_self = rep.with_self(shape, [leaf], leaf)
    alone = rep.loo(shape, [leaf], leaf)
    assert with_self.book == 1
    assert alone.book == 2
    assert alone.scores[1] == pytest.approx(0.0)
    assert alone.margin == pytest.approx(math.sqrt(0.5))


# -- order -----------------------------------------------------------------------


def _order_pair() -> tuple[Shape, list[Leaf]]:
    # X alone goes to book 1, Y alone to book 2; whichever is filed first pulls the other.
    shape = plain([e(0), e(1)], k=0.1)
    x = Leaf(1, 1, 1, unit(e(0) + 0.9 * e(1) + e(3)))
    y = Leaf(2, 2, 1, unit(e(1) + e(3)))
    return shape, [x, y]


def test_sequential_depends_on_order() -> None:
    shape, leaves = _order_pair()
    assert rep.sequential(shape, leaves, [1, 2], honour_pins=True) == {1: 1, 2: 1}
    assert rep.sequential(shape, leaves, [2, 1], honour_pins=True) == {1: 2, 2: 2}


def test_order_counts_are_seeded_and_honour_pins() -> None:
    shape, leaves = _order_pair()
    a = rep.random_orders([2, 1], 20, seed=7)
    assert a == rep.random_orders([1, 2], 20, seed=7)
    assert a != rep.random_orders([1, 2], 20, seed=8)
    counts = rep.order_counts(shape, leaves, a, honour_pins=True)
    first_x = sum(1 for o in a if o[0] == 1)
    assert counts[1] == {1: first_x, 2: 20 - first_x}
    assert sum(counts[2].values()) == 20
    # Pin Y to book 2: it never moves, and X follows it whenever Y comes first.
    pinned = [leaves[0], Leaf(2, 2, 1, leaves[1].vec, book_pinned=True)]
    counts = rep.order_counts(shape, pinned, a, honour_pins=True)
    assert counts[2] == {2: 20}
    ignored = rep.order_counts(shape, pinned, a, honour_pins=False)
    assert ignored[2] == {1: first_x, 2: 20 - first_x}


# -- rank correlation ---------------------------------------------------------------


def test_ranks_share_ties() -> None:
    assert list(rep.ranks([10, 20, 20, 30])) == [1.0, 2.5, 2.5, 4.0]
    assert list(rep.ranks([3, 1, 2])) == [3.0, 1.0, 2.0]


def test_spearman() -> None:
    assert rep.spearman([1, 2, 3, 4], [10, 20, 30, 400]) == pytest.approx(1.0)
    assert rep.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Ties take average ranks: Pearson of [1,2,3,4] and [1.5,1.5,3.5,3.5].
    assert rep.spearman([1, 2, 3, 4], [0, 0, 1, 1]) == pytest.approx(2 / math.sqrt(5))
    assert math.isnan(rep.spearman([1, 2, 3], [5, 5, 5]))


def test_pair_stat_counts_pairs_once() -> None:
    vecs = [e(0), unit(e(0) + 0.1 * e(1)), e(2)]
    same = [[True, True, False], [True, True, False], [False, False, True]]
    s = rep.pair_stat(vecs, same)
    assert (s.n_pairs, s.n_same) == (3, 1)
    assert s.mean_same == pytest.approx(float(np.dot(vecs[0], vecs[1])))
    assert s.mean_diff == pytest.approx(0.0)
    assert s.rho == pytest.approx(
        rep.spearman([float(np.dot(vecs[0], vecs[1])), 0.0, 0.0], [1, 0, 0])
    )


# -- before and after ----------------------------------------------------------------


def test_before_after_honours_pins() -> None:
    shape = plain([e(0), e(1)], k=3.0)
    leaves = [
        # Belongs in 2, sits in 1, free: moves.
        Leaf(1, 1, 5, e(1)),
        # Belongs in 2, sits in 1, book pinned: stays in 1, its slot may change.
        Leaf(2, 1, 9, e(1), book_pinned=True),
        # Belongs in 2, sits in 1, pinned to its slot: stays exactly.
        Leaf(3, 1, 30, e(1), book_pinned=True, pinned=True),
        Leaf(4, 1, 40, e(0)),
    ]
    rows = {p.image_id: p for p in rep.before_after(shape, leaves)}
    assert rows[1].after[0] == 2
    assert rows[1].gain > 0.03
    assert rows[2].after[0] == 1
    assert rows[3].after == (1, 30)
    assert rows[4].after[0] == 1
    assert rows[4].gain == 0.0
    assert rows[4].margin > 0


# -- inputs -------------------------------------------------------------------------


def test_content_text_cuts_at_style() -> None:
    a = "Subject: a ram.\n\nSetting: a field.\n\nStyle: woodcut.\n\nMood: calm."
    assert rep.content_text(a) == "Subject: a ram.\n\nSetting: a field."
    assert rep.content_text("free text") is None
    assert rep.content_text(None) is None


def test_medium_takes_the_first_rule_that_matches() -> None:
    assert rep.medium("Printed illustration imitating woodcut or risograph work") == (
        "print",
        "woodcut",
    )
    assert rep.medium("Imitation of an illuminated manuscript, with a woodcut border") == (
        "painted manuscript",
        "illuminated",
    )
    assert rep.medium("Fine pencil drawing") == ("drawing", "pencil")
    assert rep.medium("Oil on canvas") == ("other", "")


def test_gold_centroid_ignores_books_with_no_other_member() -> None:
    shape = plain([e(0), e(1), e(2)])
    leaves = [Leaf(1, 1, 1, e(0)), Leaf(2, 1, 2, unit(e(0) + 0.2 * e(2))), Leaf(3, 3, 1, e(2))]
    space = rep.Space(shape, leaves, {x.image_id: x.vec for x in leaves}, {})
    gold = {1: frozenset({1}), 2: frozenset({1}), 3: frozenset({3})}
    assert rep.gold_centroid_pick(space, gold, leaves[0]) == 1
    # Leaf 3 is book 3's only member; book 1 is the only centroid left.
    assert rep.gold_centroid_pick(space, gold, leaves[2]) == 1


def test_evaluate_on_a_clean_split() -> None:
    shape = plain([e(0), e(1)], k=3.0)
    leaves = [Leaf(1, 1, 1, e(0)), Leaf(2, 2, 1, e(1)), Leaf(3, 1, 2, unit(e(1) + 0.1 * e(2)))]
    space = rep.Space(shape, leaves, {x.image_id: x.vec for x in leaves}, {})
    gold = {1: frozenset({1}), 2: frozenset({2}), 3: frozenset({2})}
    cell = rep.evaluate(space, gold, rep.random_orders([1, 2, 3], 5, seed=1))
    assert cell.acc_loo_all == pytest.approx(1.0)
    assert cell.flip_loo == pytest.approx(1 / 3)
    assert cell.acc_resort_unpinned == pytest.approx(1.0)
    assert cell.acc_seq_unpinned == pytest.approx(1.0)


# -- pins at gold, tags, medium words ---------------------------------------------------


def test_rebook_moves_only_the_named_leaves_and_keeps_pins() -> None:
    leaves = [Leaf(1, 3, 5, e(0), book_pinned=True), Leaf(2, 3, 6, e(1))]
    out = rep.rebook(leaves, {1: 4})
    assert [(x.image_id, x.book, x.book_pinned) for x in out] == [(1, 4, True), (2, 3, False)]
    assert leaves[0].book == 3


def test_dehyphen_lets_a_hint_reach_inside_a_tag() -> None:
    from penish.collection.plan import tag_overlap

    tags = frozenset({"zodiac-man", "aged-paper"})
    assert rep.dehyphen(tags) == frozenset({"zodiac man", "aged paper"})
    assert tag_overlap(tags, ["zodiac"]) == 0.0
    assert tag_overlap(rep.dehyphen(tags), ["zodiac"]) == 1.0


def test_gold_books_sends_a_two_book_leaf_to_the_nearer_one() -> None:
    shape = plain([e(0), e(1), e(2)])
    leaves = [
        Leaf(1, 3, 1, e(0)),
        Leaf(2, 3, 2, e(1)),
        Leaf(3, 3, 3, unit(e(1) + 0.3 * e(0))),
    ]
    gold = {1: frozenset({1}), 2: frozenset({2}), 3: frozenset({1, 2})}
    assert rep.gold_books(shape, leaves, gold) == {1: 1, 2: 2, 3: 2}


def test_upper_bound_scores_each_leaf_against_a_collection_already_in_gold() -> None:
    shape = plain([e(0), e(1)], k=3.0)
    # Stored all in book 1; gold splits them. Against the gold state each leaf finds home.
    leaves = [Leaf(1, 1, 1, e(0)), Leaf(2, 1, 2, e(1)), Leaf(3, 1, 3, unit(e(1) + 0.1 * e(2)))]
    gold = {1: frozenset({1}), 2: frozenset({2}), 3: frozenset({2})}
    where, picks = rep.upper_bound(shape, leaves, gold)
    assert where == {1: 1, 2: 2, 3: 2}
    assert {i: p.book for i, p in picks.items()} == {1: 1, 2: 2, 3: 2}


def test_medium_words_match_whole_words_and_plurals() -> None:
    text = "Two plates and a pen and ink sketch on a printed page; plate armor; a template."
    assert rep.medium_words(text) == ["printed", "pen and ink", "plate", "page"]
    assert rep.medium_words("Woodcuts in a manuscript") == ["woodcut", "manuscript"]
    assert rep.medium_words(None) == []


def test_pair_stat_keeps_only_the_allowed_pairs() -> None:
    vecs = [e(0), unit(e(0) + 0.1 * e(1)), e(2), unit(e(2) + 0.5 * e(0))]
    same = [[i // 2 == j // 2 for j in range(4)] for i in range(4)]
    keep = [[(i + j) % 2 == 1 for j in range(4)] for i in range(4)]
    s = rep.pair_stat(vecs, same, keep)
    # Pairs (0,1), (0,3), (1,2), (2,3).
    assert (s.n_pairs, s.n_same) == (4, 2)
