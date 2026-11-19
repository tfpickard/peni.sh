"""Where leaves go: which book, and which leaf of it. Pure numpy, no database.

Vectors arrive unit length and already centered (or not) by the caller, in the same space
the similar lists use, so "near" here means what it means everywhere else on the site.

A book's centroid blends its written anchor with its real leaves. An empty book is all
anchor; as it fills the leaves take over and the book becomes whatever its pictures are.
Within a book, leaves sit on a nearest-neighbor chain stretched across the slots, so
related leaves are adjacent and the gaps fall where the subject jumps.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np

# Two of the same picture are distance 0; an unknown pair is treated as unrelated.
_UNKNOWN = 1.0
_EPS = 1e-12
_TWO_OPT_SWEEPS = 50
RESORT_PASSES = 20


@dataclass(frozen=True)
class Tuning:
    # How many leaves the anchor is worth: w_anchor = k / (k + n).
    anchor_k: float = 3.0
    # Weight of the tag hints. Small: a nudge, not a vote.
    tag_alpha: float = 0.05
    # Fill pressure, beta * (n / cap) ** 2, so one book cannot swallow everything.
    fill_beta: float = 0.05
    # A re-sort moves a leaf to another book only if it scores this much better there.
    book_margin: float = 0.03
    weekly_resort: bool = False

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> Tuning:
        """Known keys of the right type override the defaults; anything else is ignored,
        so a bad value in the row can never stop filing."""
        out: dict[str, Any] = {}
        for f in fields(cls):
            v = (raw or {}).get(f.name)
            if f.name in LIMITS and isinstance(v, int | float) and not isinstance(v, bool):
                lo, hi = LIMITS[f.name]
                if lo <= v <= hi:
                    out[f.name] = float(v)
            elif f.type == "bool" and isinstance(v, bool):
                out[f.name] = v
        return cls(**out)

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


LIMITS: dict[str, tuple[float, float]] = {
    "anchor_k": (0.0, 1000.0),
    "tag_alpha": (0.0, 1.0),
    "fill_beta": (0.0, 1.0),
    "book_margin": (0.0, 1.0),
}


@dataclass
class Leaf:
    image_id: int
    book: int
    position: int
    # None when the picture has no vector under the current model, or is not live: such a
    # leaf keeps its place and counts toward its book's fill, but pulls nothing.
    vec: np.ndarray | None
    tags: frozenset[str] = frozenset()
    book_pinned: bool = False
    pinned: bool = False

    @property
    def stays(self) -> bool:
        """Never moved by the filer, inside its book or out of it."""
        return self.pinned or self.vec is None


@dataclass
class Book:
    number: int
    anchor: np.ndarray | None
    hints: frozenset[str] = frozenset()


@dataclass
class Shape:
    books: list[Book]
    cap: int
    tuning: Tuning = field(default_factory=Tuning)

    def book(self, number: int) -> Book:
        for b in self.books:
            if b.number == number:
                return b
        raise KeyError(number)


# -- scoring ------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray | None:
    n = float(np.linalg.norm(v))
    return None if n < _EPS else v / n


def _words(tags: Iterable[str]) -> set[str]:
    # A hint matches a whole tag or any word of one, so `serpent` finds `coiled serpent`.
    out: set[str] = set()
    for t in tags:
        t = t.lower().strip()
        if t:
            out.add(t)
            out.update(t.split())
    return out


def tag_overlap(tags: Iterable[str], hints: Iterable[str]) -> float:
    """Share of the smaller side that matches, 0..1. The overlap coefficient rather than
    Jaccard, so a picture with fourteen tags is not punished for having many."""
    h = {x.lower().strip() for x in hints if x.strip()}
    if not h:
        return 0.0
    words = _words(tags)
    if not words:
        return 0.0
    hit = len(h & words)
    # Capped: one tag can match several hints (`alchemy lab` meets `alchemy` and `lab`),
    # which would otherwise score past 1 and give the hints more say than alpha means.
    return min(1.0, hit / min(len(h), len(set(tags)) or 1))


def centroid(
    anchor: np.ndarray | None, vec_sum: np.ndarray | None, n_vec: int, k: float
) -> np.ndarray | None:
    """normalize(w * anchor + (1 - w) * mean(leaves)), w = k / (k + n)."""
    mean = None if vec_sum is None or n_vec == 0 else vec_sum / n_vec
    if anchor is None:
        return None if mean is None else _unit(mean)
    if mean is None:
        return anchor
    w = k / (k + n_vec) if k + n_vec > 0 else 0.0
    return _unit(w * anchor + (1.0 - w) * mean)


def score(
    vec: np.ndarray,
    tags: frozenset[str],
    book: Book,
    center: np.ndarray | None,
    occupied: int,
    cap: int,
    t: Tuning,
) -> float:
    cos = float(np.dot(vec, center)) if center is not None else 0.0
    return cos + t.tag_alpha * tag_overlap(tags, book.hints) - t.fill_beta * (occupied / cap) ** 2


class _Tally:
    """Per-book running sums, so scoring a leaf against every book is O(books * dim)."""

    def __init__(self, shape: Shape, leaves: Iterable[Leaf], where: Mapping[int, int]) -> None:
        dim = next((len(b.anchor) for b in shape.books if b.anchor is not None), None)
        self.sums: dict[int, np.ndarray | None] = {b.number: None for b in shape.books}
        self.n_vec = {b.number: 0 for b in shape.books}
        self.occupied = {b.number: 0 for b in shape.books}
        self.dim = dim
        for leaf in leaves:
            self.add(leaf, where[leaf.image_id])

    def add(self, leaf: Leaf, book: int, sign: int = 1) -> None:
        self.occupied[book] += sign
        if leaf.vec is not None:
            s = self.sums[book]
            self.sums[book] = leaf.vec * sign if s is None else s + leaf.vec * sign
            self.n_vec[book] += sign

    def scores(
        self, shape: Shape, vec: np.ndarray, tags: frozenset[str], allowed: Iterable[int]
    ) -> dict[int, float]:
        out = {}
        for number in allowed:
            b = shape.book(number)
            c = centroid(b.anchor, self.sums[number], self.n_vec[number], shape.tuning.anchor_k)
            out[number] = score(vec, tags, b, c, self.occupied[number], shape.cap, shape.tuning)
        return out


def _best(scores: Mapping[int, float]) -> int:
    # Ties go to the lower book, so the result never depends on dict order.
    return min(scores, key=lambda n: (-scores[n], n))


def choose_book(
    shape: Shape, leaves: list[Leaf], vec: np.ndarray, tags: frozenset[str]
) -> tuple[int, dict[int, float]] | None:
    """Best book with room for one more, and every score; None when all are full."""
    where = {leaf.image_id: leaf.book for leaf in leaves}
    tally = _Tally(shape, leaves, where)
    room = [b.number for b in shape.books if tally.occupied[b.number] < shape.cap]
    if not room:
        return None
    s = tally.scores(shape, vec, tags, room)
    return _best(s), s


def assign_books(shape: Shape, leaves: list[Leaf]) -> dict[int, int]:
    """Book for every leaf after a full re-sort.

    One leaf at a time against everyone else's current book, repeated until a pass moves
    nothing, so the result is a fixed point and an immediate second re-sort moves nothing
    either. A leaf changes book only when it beats its own book by the margin; without
    that, near-ties would flap on every run.
    """
    where = {leaf.image_id: leaf.book for leaf in leaves}
    tally = _Tally(shape, leaves, where)
    movers = sorted(
        (leaf for leaf in leaves if not leaf.book_pinned and leaf.vec is not None),
        key=lambda x: x.image_id,
    )
    for _ in range(RESORT_PASSES):
        moved = False
        for leaf in movers:
            assert leaf.vec is not None
            cur = where[leaf.image_id]
            tally.add(leaf, cur, -1)
            allowed = [
                b.number
                for b in shape.books
                if b.number == cur or tally.occupied[b.number] < shape.cap
            ]
            s = tally.scores(shape, leaf.vec, leaf.tags, allowed)
            best = _best(s)
            if best != cur and s[best] > s[cur] + shape.tuning.book_margin:
                where[leaf.image_id] = best
                moved = True
            tally.add(leaf, where[leaf.image_id])
        if not moved:
            break
    return where


# -- positions ------------------------------------------------------------------


def _dist(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return _UNKNOWN
    return max(0.0, 1.0 - float(np.dot(a, b)))


def _round(x: float) -> int:
    # Half up, not Python's half to even, so equal gaps land on predictable slots.
    return math.floor(x + 0.5)


@dataclass
class _Segment:
    lo: Leaf | None
    hi: Leaf | None
    a: int
    b: int
    members: list[Leaf] = field(default_factory=list)

    @property
    def room(self) -> int:
        return self.b - self.a + 1 - len(self.members)


def _two_opt(path: list[np.ndarray | None], fixed_end: bool) -> list[int]:
    """Indices of path reordered by 2-opt, with path[0] held first (and the last held last
    when fixed_end). Cheap and local: an exact tour is not the point."""
    order = list(range(len(path)))
    m = len(order)
    last = m - 2 if fixed_end else m - 1

    def d(i: int, j: int) -> float:
        return _dist(path[order[i]], path[order[j]])

    for _ in range(_TWO_OPT_SWEEPS):
        better = False
        for i in range(1, last):
            for j in range(i + 1, last + 1):
                before = d(i - 1, i)
                after = d(i - 1, j)
                if j + 1 < m:
                    before += d(j, j + 1)
                    after += d(i, j + 1)
                if after < before - _EPS:
                    order[i : j + 1] = reversed(order[i : j + 1])
                    better = True
        if not better:
            break
    return order


def _greedy(start: np.ndarray | None, rest: list[Leaf]) -> list[Leaf]:
    out: list[Leaf] = []
    left = sorted(rest, key=lambda x: x.image_id)
    here = start
    while left:
        nxt = min(left, key=lambda x: (_dist(here, x.vec), x.image_id))
        out.append(nxt)
        left.remove(nxt)
        here = nxt.vec
    return out


def _lay(seg: _Segment, anchor: np.ndarray | None) -> dict[int, int]:
    free = seg.members
    if not free:
        return {}
    if seg.lo is not None:
        chain = _greedy(seg.lo.vec, free)
    else:
        # The opening run of the book starts at the leaf nearest what the book is about.
        first = min(free, key=lambda x: (_dist(anchor, x.vec), x.image_id))
        chain = [first, *_greedy(first.vec, [x for x in free if x is not first])]

    head = [seg.lo] if seg.lo is not None else []
    tail = [seg.hi] if seg.hi is not None else []
    nodes: list[Leaf] = [*head, *chain, *tail]
    if len(nodes) > 2:
        order = _two_opt([n.vec for n in nodes], fixed_end=bool(tail))
        nodes = [nodes[i] for i in order]

    cum = [0.0]
    for x, y in zip(nodes, nodes[1:], strict=False):
        cum.append(cum[-1] + _dist(x.vec, y.vec))
    total = cum[-1]
    if total < _EPS:
        # Identical leaves, or one alone: spread them evenly instead of stacking.
        cum = [float(i) for i in range(len(nodes))]
        total = cum[-1]

    start = seg.lo.position if seg.lo is not None else seg.a
    end = seg.hi.position if seg.hi is not None else seg.b
    lo_i = 1 if seg.lo is not None else 0
    hi_i = len(nodes) - 1 if seg.hi is not None else len(nodes)
    targets = []
    for i in range(lo_i, hi_i):
        frac = cum[i] / total if total > 0 else 0.0
        targets.append(min(seg.b, max(seg.a, start + _round(frac * (end - start)))))

    # Collisions nudge to the nearest free slot while keeping chain order: forward so no
    # two share, then backward so none spills past the segment.
    slots = list(targets)
    for i in range(len(slots)):
        floor = seg.a if i == 0 else slots[i - 1] + 1
        slots[i] = max(slots[i], floor)
    for i in range(len(slots) - 1, -1, -1):
        ceil = seg.b if i == len(slots) - 1 else slots[i + 1] - 1
        slots[i] = min(slots[i], ceil)
    return {leaf.image_id: s for leaf, s in zip(nodes[lo_i:hi_i], slots, strict=True)}


def positions(book: Book, leaves: list[Leaf], cap: int) -> dict[int, int]:
    """Leaf position for every leaf in one book.

    Leaves that stay are fixed points. The chain runs in the segments between them, so
    the free leaves never take a fixed slot and each free leaf sits in the gap beside the
    fixed leaf (or the anchor, for the opening gap) it is nearest.
    """
    if len(leaves) > cap:
        raise ValueError(f"book {book.number} holds {len(leaves)} leaves, room for {cap}")
    fixed: dict[int, Leaf] = {}
    free: list[Leaf] = []
    for leaf in sorted(leaves, key=lambda x: (not x.pinned, x.image_id)):
        # A vectorless leaf stays put unless a pin has taken its slot; then it goes free.
        if leaf.stays and 1 <= leaf.position <= cap and leaf.position not in fixed:
            fixed[leaf.position] = leaf
        elif leaf.pinned:
            raise ValueError(f"pinned leaf {leaf.image_id} has no slot {leaf.position}")
        else:
            free.append(leaf)

    out = {leaf.image_id: pos for pos, leaf in fixed.items()}
    bounds = sorted(fixed.values(), key=lambda x: x.position)
    segs: list[_Segment] = []
    prev: Leaf | None = None
    for hi in [*bounds, None]:
        a = prev.position + 1 if prev is not None else 1
        b = hi.position - 1 if hi is not None else cap
        if b >= a:
            segs.append(_Segment(prev, hi, a, b))
        prev = hi

    def near(leaf: Leaf, s: _Segment) -> float:
        ds = [_dist(leaf.vec, x.vec) for x in (s.lo, s.hi) if x is not None]
        if s.lo is None and book.anchor is not None:
            ds.append(_dist(leaf.vec, book.anchor))
        return min(ds) if ds else 0.0

    # Most decided first, so a leaf that clearly belongs in a small gap gets it. Two gaps
    # on either side of one fixed leaf are equally near every leaf that is nearest it; the
    # tie goes to the gap with more room, or a lone pin at 14 would crowd the whole book
    # into leaves 1 to 13 and leave 15 to 77 empty.
    ranked = sorted(free, key=lambda x: (min((near(x, s) for s in segs), default=0.0), x.image_id))
    for leaf in ranked:
        options = sorted(
            (s for s in segs if s.room > 0), key=lambda s: (near(leaf, s), -s.room, s.a)
        )
        options[0].members.append(leaf)
    for s in segs:
        out |= _lay(s, book.anchor)
    return out


# -- whole plans -------------------------------------------------------------------


Placement = dict[int, tuple[int, int]]


def lay_books(
    shape: Shape, leaves: list[Leaf], where: Mapping[int, int], books: Iterable[int]
) -> Placement:
    """Positions for the named books, and for any book a leaf moves into, given each
    leaf's book. Every other leaf keeps its place; a book a leaf left keeps its gap."""
    redo = set(books) | {where[x.image_id] for x in leaves if where[x.image_id] != x.book}
    out: Placement = {
        x.image_id: (x.book, x.position) for x in leaves if where[x.image_id] not in redo
    }
    for number in sorted(redo):
        members = [
            Leaf(
                x.image_id,
                number,
                # An arrival has no claim to a slot number it held in another book.
                x.position if x.book == number else 0,
                x.vec,
                x.tags,
                x.book_pinned,
                x.pinned,
            )
            for x in leaves
            if where[x.image_id] == number
        ]
        for image_id, pos in positions(shape.book(number), members, shape.cap).items():
            out[image_id] = (number, pos)
    return out


def plan_resort(shape: Shape, leaves: list[Leaf]) -> Placement:
    where = assign_books(shape, leaves)
    return lay_books(shape, leaves, where, [b.number for b in shape.books])
