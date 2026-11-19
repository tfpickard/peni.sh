"""The filer's arithmetic: which book a leaf goes in, and which leaf of the book.

Pure numpy over vectors the caller has already prepared (centered when the rest of the
site would center, then unit length), so it can be tested without a database.

Books: each book's centroid blends its written anchor with the leaves it already has.
An empty book is all anchor; a full one is almost all leaves, so a book drifts toward
whatever it actually holds. Positions: an open nearest-neighbor path through a book's
leaves, its length laid out over the slots, so the gaps fall where the subject jumps.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

# Passes of the full re-sort. One pass moves leaves on centroids that the same pass then
# changes, so it repeats until a pass moves nothing; the cap only guards a cycle.
MAX_RESORT_PASSES = 10
_TWO_OPT_PASSES = 20
_EPS = 1e-9


@dataclass(frozen=True)
class Params:
    """The tunables. Defaults match runtime's; the caller passes the live values."""

    k: float = 3.0
    alpha: float = 0.05
    beta: float = 0.05
    margin: float = 0.03


@dataclass
class BookSpace:
    """One book as the scorer sees it: its anchor, hints and running leaf sum."""

    number: int
    anchor: np.ndarray | None
    hints: frozenset[str] = frozenset()
    total: np.ndarray | None = None
    n: int = 0

    def add(self, v: np.ndarray) -> None:
        self.total = v.copy() if self.total is None else self.total + v
        self.n += 1

    def remove(self, v: np.ndarray) -> None:
        assert self.total is not None and self.n > 0
        self.n -= 1
        self.total = None if self.n == 0 else self.total - v


@dataclass
class Leaf:
    image_id: int
    vec: np.ndarray
    book: int
    position: int
    tags: frozenset[str] = field(default_factory=frozenset)
    pinned_book: bool = False
    pinned_position: bool = False


def unit(v: np.ndarray) -> np.ndarray | None:
    n = float(np.linalg.norm(v))
    return None if n < _EPS else v / n


def centroid(book: BookSpace, k: float) -> np.ndarray | None:
    """normalize(w * anchor + (1 - w) * mean(leaves)), w = k / (k + n).

    A book with no anchor is just its leaves; one with neither has no centroid and cannot
    be scored.
    """
    mean = None if book.total is None or book.n == 0 else book.total / book.n
    if book.anchor is None:
        return None if mean is None else unit(mean)
    if mean is None:
        return unit(book.anchor)
    w = k / (k + book.n) if k + book.n > 0 else 0.0
    return unit(w * book.anchor + (1.0 - w) * mean)


def _words(tags: Iterable[str]) -> set[str]:
    # A hint matches a whole tag or any word of one: `alchemy` should meet
    # `alchemical apparatus`'s neighbour `alchemy lab` without fuzzy matching.
    out: set[str] = set()
    for t in tags:
        t = t.lower().strip()
        out.add(t)
        out.update(t.replace("-", " ").split())
    return out


def tag_overlap(tags: Iterable[str], hints: Iterable[str]) -> float:
    """Share of the book's hints that the picture's tags touch, 0..1."""
    hs = {h.lower().strip() for h in hints if h.strip()}
    if not hs:
        return 0.0
    return len(hs & _words(tags)) / len(hs)


def score(
    vec: np.ndarray, tags: Iterable[str], book: BookSpace, capacity: int, p: Params
) -> float | None:
    c = centroid(book, p.k)
    if c is None:
        return None
    fill = book.n / capacity
    return float(np.dot(vec, c)) + p.alpha * tag_overlap(tags, book.hints) - p.beta * fill * fill


def scores(
    vec: np.ndarray,
    tags: Iterable[str],
    books: Sequence[BookSpace],
    capacity: int,
    p: Params,
    *,
    open_only: bool = True,
) -> dict[int, float]:
    tags = list(tags)
    out: dict[int, float] = {}
    for b in books:
        if open_only and b.n >= capacity:
            continue
        s = score(vec, tags, b, capacity, p)
        if s is not None:
            out[b.number] = s
    return out


def best(found: Mapping[int, float]) -> int | None:
    # Lowest number wins a tie, so the same state always files the same way.
    if not found:
        return None
    return min(found, key=lambda n: (-found[n], n))


def choose_book(
    vec: np.ndarray,
    tags: Iterable[str],
    books: Sequence[BookSpace],
    capacity: int,
    p: Params,
) -> int | None:
    """The best book with room, or None when every book is full or none can be scored."""
    return best(scores(vec, tags, books, capacity, p))


def resort_books(
    leaves: Sequence[Leaf], books: Sequence[BookSpace], capacity: int, p: Params
) -> dict[int, int]:
    """New book for each leaf the owner did not pin to one, as {image_id: book}; only
    leaves that move are returned.

    Leaves are visited one at a time against the current state, and a leaf leaves its book
    only when another beats it by `margin`, measured with the leaf itself taken out of its
    own book (otherwise every leaf would favour where it already is). Passes repeat until
    one moves nothing, so running it again straight after moves nothing either.
    """
    by_number = {b.number: b for b in books}
    for b in books:
        b.total, b.n = None, 0
    for leaf in leaves:
        by_number[leaf.book].add(leaf.vec)

    home = {leaf.image_id: leaf.book for leaf in leaves}
    free = sorted((lf for lf in leaves if not lf.pinned_book), key=lambda lf: lf.image_id)
    for _ in range(MAX_RESORT_PASSES):
        moved = False
        for leaf in free:
            cur = by_number[home[leaf.image_id]]
            cur.remove(leaf.vec)
            found = scores(leaf.vec, leaf.tags, books, capacity, p)
            mine = score(leaf.vec, leaf.tags, cur, capacity, p)
            pick = best(found)
            if (
                pick is not None
                and pick != cur.number
                and (mine is None or found[pick] > mine + p.margin)
            ):
                by_number[pick].add(leaf.vec)
                home[leaf.image_id] = pick
                moved = True
            else:
                cur.add(leaf.vec)
        if not moved:
            break
    return {lf.image_id: home[lf.image_id] for lf in leaves if home[lf.image_id] != lf.book}


# -- positions ----------------------------------------------------------------


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    return max(0.0, 1.0 - float(np.dot(a, b)))


def _greedy(start: int, rest: list[int], d: np.ndarray) -> list[int]:
    path = [start]
    left = set(rest)
    while left:
        here = path[-1]
        # Lowest index breaks ties; the caller orders nodes by image id.
        nxt = min(left, key=lambda j: (d[here, j], j))
        path.append(nxt)
        left.remove(nxt)
    return path


def _two_opt(path: list[int], d: np.ndarray, fixed_start: bool, fixed_end: bool) -> list[int]:
    """Reverse sub-runs while it shortens the open path. Ends pinned in place stay put."""
    m = len(path)
    lo = 1 if fixed_start else 0
    hi = m - 2 if fixed_end else m - 1
    for _ in range(_TWO_OPT_PASSES):
        improved = False
        for i in range(lo, hi):
            for j in range(i + 1, hi + 1):
                before = after = 0.0
                if i > 0:
                    before += d[path[i - 1], path[i]]
                    after += d[path[i - 1], path[j]]
                if j < m - 1:
                    before += d[path[j], path[j + 1]]
                    after += d[path[i], path[j + 1]]
                if after < before - 1e-12:
                    path[i : j + 1] = path[i : j + 1][::-1]
                    improved = True
        if not improved:
            break
    return path


def _spread(desired: list[int], lo: int, hi: int) -> list[int]:
    """Strictly increasing slots in [lo, hi] as close to `desired` (non-decreasing) as the
    room allows: collisions are nudged to the nearest free slot, in order."""
    m = len(desired)
    if m > hi - lo + 1:
        raise ValueError("more leaves than slots")
    out = [min(max(s, lo), hi) for s in desired]
    for i in range(m):
        floor = lo if i == 0 else out[i - 1] + 1
        out[i] = max(out[i], floor)
    for i in range(m - 1, -1, -1):
        ceil = hi if i == m - 1 else out[i + 1] - 1
        out[i] = min(out[i], ceil)
    return out


def _lay_out(
    path: list[int], d: np.ndarray, slot_lo: int, slot_hi: int, members: set[int], lo: int, hi: int
) -> dict[int, int]:
    """Cumulative path length mapped onto slot_lo..slot_hi; members land in lo..hi."""
    cum = [0.0]
    for a, b in zip(path, path[1:], strict=False):
        cum.append(cum[-1] + d[a, b])
    total = cum[-1]
    span = slot_hi - slot_lo
    desired: list[int] = []
    order: list[int] = []
    for idx, node in enumerate(path):
        if node not in members:
            continue
        # Identical vectors leave no length to lay out; fall back to even spacing.
        frac = cum[idx] / total if total > _EPS else idx / max(1, len(path) - 1)
        desired.append(slot_lo + round(frac * span))
        order.append(node)
    return dict(zip(order, _spread(desired, lo, hi), strict=True))


def positions(
    vecs: Sequence[np.ndarray],
    fixed: Sequence[int | None],
    capacity: int,
    anchor: np.ndarray | None = None,
) -> list[int]:
    """A slot for every leaf of one book, in input order.

    `fixed[i]` is a slot the owner pinned, or None. Pinned slots are kept and split the
    book into runs; every other leaf is assigned to the run whose pinned ends it is
    nearest, then chained within it.
    """
    n = len(vecs)
    if n == 0:
        return []
    if n > capacity:
        raise ValueError("more leaves than slots")
    taken = [f for f in fixed if f is not None]
    if len(set(taken)) != len(taken) or any(not 1 <= f <= capacity for f in taken):
        raise ValueError("pinned slots collide or fall outside the book")

    mat = np.stack(vecs)
    d = np.clip(1.0 - mat @ mat.T, 0.0, None)
    out: list[int | None] = list(fixed)
    free = [i for i in range(n) if fixed[i] is None]
    if not free:
        return [int(f) for f in fixed if f is not None]

    pins = sorted((f, i) for i, f in enumerate(fixed) if f is not None)
    if not pins:
        if anchor is not None:
            start = min(free, key=lambda i: (_dist(vecs[i], anchor), i))
        else:
            mid = unit(mat[free].mean(axis=0))
            start = free[0] if mid is None else min(free, key=lambda i: (_dist(vecs[i], mid), i))
        path = _greedy(start, [i for i in free if i != start], d)
        path = _two_opt(path, d, fixed_start=True, fixed_end=False)
        laid = _lay_out(path, d, 1, capacity, set(free), 1, capacity)
        return [laid[i] for i in range(n)]

    # Runs between consecutive pins, plus the open runs before the first and after the
    # last: (left pin index or None, its slot, right pin index or None, its slot).
    bounds: list[tuple[int | None, int]] = [(None, 0), *[(i, s) for s, i in pins]]
    bounds.append((None, capacity + 1))
    runs = [
        (bounds[j][0], bounds[j][1], bounds[j + 1][0], bounds[j + 1][1])
        for j in range(len(bounds) - 1)
    ]
    room = [rs - ls - 1 for _, ls, _, rs in runs]

    # Each free leaf to the run with the nearest pinned end, surest leaves first. Runs on
    # either side of one pin tie for every leaf; the tie goes to the run with more room
    # left, so a lone pin near one end does not crowd the whole book into the short side.
    cost = {
        (i, r): min(d[i, e] for e in (li, ri) if e is not None)
        for i in free
        for r, (li, _, ri, _) in enumerate(runs)
    }
    run_of: dict[int, int] = {}
    for i in sorted(free, key=lambda i: (min(cost[i, r] for r in range(len(runs))), i)):
        r = min(
            (r for r in range(len(runs)) if room[r] > 0),
            key=lambda r: (cost[i, r], -room[r], r),
        )
        run_of[i] = r
        room[r] -= 1

    for r, (li, ls, ri, rs) in enumerate(runs):
        members = [i for i in free if run_of.get(i) == r]
        if not members:
            continue
        if li is not None:
            path = _greedy(li, members, d)
            if ri is not None:
                path.append(ri)
            path = _two_opt(path, d, fixed_start=True, fixed_end=ri is not None)
        else:
            assert ri is not None
            # Built outward from the right pin and turned round, so it still ends there.
            path = _greedy(ri, members, d)[::-1]
            path = _two_opt(path, d, fixed_start=False, fixed_end=True)
        slot_lo = ls if li is not None else 1
        slot_hi = rs if ri is not None else capacity
        laid = _lay_out(path, d, slot_lo, slot_hi, set(members), ls + 1, rs - 1)
        for i, s in laid.items():
            out[i] = s
    assert all(s is not None for s in out)
    return [int(s) for s in out if s is not None]
