"""Candidate edges per layer: who each picture may grow toward.

Pure: numpy and the standard library. The engine never looks at vectors, only at
the edges built here, so a layer can be fed any id-keyed vectors (whole-picture
embeddings, facet embeddings, or later the need layer's phrase vectors).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from penish.mycelium.tunables import Tunables

FloatArray = NDArray[np.float64]


class BonusMatrix:
    """A per-pair bonus held as a matrix over sorted ids, so a layer of a thousand
    pictures does not make a million Python calls. Callable like any other bonus."""

    __slots__ = ("ids", "values", "_index")

    def __init__(self, ids: Sequence[int], values: FloatArray) -> None:
        self.ids = np.asarray(ids, dtype=np.int64)
        if self.ids.ndim != 1 or np.any(np.diff(self.ids) <= 0):
            raise ValueError("bonus ids must be sorted and distinct")
        if values.shape != (len(self.ids), len(self.ids)):
            raise ValueError("bonus matrix does not match its ids")
        self.values = values
        self._index = {int(v): i for i, v in enumerate(self.ids)}

    def __call__(self, a: int, b: int) -> float:
        ia, ib = self._index.get(a), self._index.get(b)
        if ia is None or ib is None:
            return 0.0
        return float(self.values[ia, ib])

    def take(self, ids: Sequence[int]) -> FloatArray:
        """The submatrix for `ids` (sorted); 0 for any id the bonus never saw."""
        n = len(ids)
        out = np.zeros((n, n), dtype=np.float64)
        pos = [self._index.get(int(i)) for i in ids]
        mine = np.asarray([k for k, p in enumerate(pos) if p is not None], dtype=np.int64)
        if mine.size:
            src = np.asarray([pos[k] for k in mine], dtype=np.int64)
            out[np.ix_(mine, mine)] = self.values[np.ix_(src, src)]
        return out


# Added to a pair's score before neighbors are chosen; the facet bonus slots in
# here so the selection, the stored `raw` and the quantile all see the same number.
Bonus = Callable[[int, int], float] | BonusMatrix


@dataclass(frozen=True, slots=True)
class Edge:
    layer: str
    src: int
    dst: int
    raw: float
    affinity: float


def _unit_rows(x: FloatArray) -> tuple[FloatArray, NDArray[np.bool_]]:
    norms = np.linalg.norm(x, axis=1)
    zero = norms == 0
    safe = np.where(zero, 1.0, norms)
    return x / safe[:, None], zero


def scores(x: FloatArray, mean: FloatArray | None) -> FloatArray:
    """Pairwise centered cosine, raw cosine for any pair that cannot be centered.

    Same rule as `similarity.py`: no mean, a mean of another width, or a vector
    equal to the mean leaves nothing to center, so that pair keeps its raw score.
    A zero vector scores 0 against everything rather than NaN.
    """
    raw_unit, _ = _unit_rows(x)
    out: FloatArray = raw_unit @ raw_unit.T
    if mean is None or mean.shape != (x.shape[1],):
        return out
    cen_unit, cen_zero = _unit_rows(x - mean[None, :])
    centered: FloatArray = cen_unit @ cen_unit.T
    fallback = cen_zero[:, None] | cen_zero[None, :]
    result: FloatArray = np.where(fallback, out, centered)
    return result


def quantiles(values: FloatArray) -> FloatArray:
    """Empirical quantile of each value: rank / (n - 1), tied values share their mean rank."""
    n = values.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if n == 1:
        # One edge is both the lowest and the highest; the top keeps it strongest.
        return np.ones(1, dtype=np.float64)
    _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    start = np.cumsum(counts) - counts
    mean_rank = start + (counts - 1) / 2.0
    q: FloatArray = mean_rank[inverse].astype(np.float64) / float(n - 1)
    return q


def candidate_edges(
    layer: str,
    vectors: Mapping[int, FloatArray],
    mean: FloatArray | None,
    tunables: Tunables,
    *,
    bonus: Bonus | None = None,
) -> list[Edge]:
    """Directed edges from each picture to its `candidates` nearest others, sorted by (src, dst)."""
    ids = sorted(vectors)
    n = len(ids)
    if n < tunables.min_size or n < 2:
        return []
    x = np.stack([np.asarray(vectors[i], dtype=np.float64) for i in ids])
    m = None if mean is None else np.asarray(mean, dtype=np.float64)
    s = scores(x, m)
    if isinstance(bonus, BonusMatrix):
        s = s + bonus.take(ids)
    elif bonus is not None:
        for a in range(n):
            for b in range(n):
                if a != b:
                    s[a, b] += float(bonus(ids[a], ids[b]))

    id_arr = np.asarray(ids, dtype=np.int64)
    k = min(tunables.candidates, n - 1)
    pairs: list[tuple[int, int, float]] = []
    for a in range(n):
        # lexsort's last key is primary: highest score first, lower id on ties.
        order = np.lexsort((id_arr, -s[a]))
        picked = 0
        for b in order:
            if b == a:
                continue
            pairs.append((ids[a], ids[int(b)], float(s[a, int(b)])))
            picked += 1
            if picked == k:
                break
    pairs.sort(key=lambda p: (p[0], p[1]))

    raw = np.asarray([p[2] for p in pairs], dtype=np.float64)
    q = quantiles(raw)
    floor = float(tunables.affinity_floor)
    return [
        Edge(layer, src, dst, r, max(floor, float(qq)))
        for (src, dst, r), qq in zip(pairs, q, strict=True)
    ]


def pair_quantiles(s: FloatArray) -> FloatArray:
    """Each unordered pair's score as its quantile among every pair of the layer.

    `s` is a symmetric score matrix; the result is symmetric with NaN on the diagonal.
    Ranks are the same `quantiles` the affinity uses, ties averaged.
    """
    n = s.shape[0]
    out = np.full((n, n), np.nan, dtype=np.float64)
    if n < 2:
        return out
    iu, ju = np.triu_indices(n, k=1)
    q = quantiles(s[iu, ju])
    out[iu, ju] = q
    out[ju, iu] = q
    return out


def facet_bonuses(
    vectors: Mapping[str, Mapping[int, FloatArray]],
    means: Mapping[str, FloatArray | None],
    weight: float,
) -> dict[str, BonusMatrix]:
    """The facet bonus per layer: close in this facet and far apart in another.

    For layer f and pair (i, j): `weight * max(0, max over g != f of q_f - q_g)`, where
    q is the pair's quantile among all pairs of its own layer, so facets whose raw
    cosines sit at different heights still compare. A layer in which i or j has no
    vector does not take part for that pair; with no other layer, no bonus. The same
    mood with a different subject is exactly a large q_mood - q_subject.
    """
    names = sorted(vectors)
    if weight <= 0.0 or len(names) < 2:
        return {}
    union = sorted({i for name in names for i in vectors[name]})
    pos = {pid: k for k, pid in enumerate(union)}
    n = len(union)
    q: dict[str, FloatArray] = {}
    for name in names:
        ids = sorted(vectors[name])
        full = np.full((n, n), np.nan, dtype=np.float64)
        if len(ids) >= 2:
            x = np.stack([np.asarray(vectors[name][i], dtype=np.float64) for i in ids])
            m = means.get(name)
            local = pair_quantiles(scores(x, None if m is None else np.asarray(m, np.float64)))
            at = np.asarray([pos[i] for i in ids], dtype=np.int64)
            full[np.ix_(at, at)] = local
        q[name] = full
    out: dict[str, BonusMatrix] = {}
    for f in names:
        # max over g of (q_f - q_g) is q_f - min over g of q_g, exactly: rounded
        # subtraction is monotone in q_g. One n x n temporary per layer instead of one
        # per pair of layers. fmin ignores NaN, so a layer missing one end of a pair
        # just sits out; with every other layer missing it the gap stays NaN, then 0.
        low = np.full((n, n), np.nan, dtype=np.float64)
        for g in names:
            if g != f:
                np.fmin(low, q[g], out=low)
        gap = np.subtract(q[f], low, out=low)
        b: FloatArray = weight * np.maximum(0.0, np.nan_to_num(gap, nan=0.0))
        out[f] = BonusMatrix(union, b)
    return out


def facet_graphs(
    vectors: Mapping[str, Mapping[int, FloatArray]],
    means: Mapping[str, FloatArray | None],
    tunables: Tunables,
) -> dict[str, list[Edge]]:
    """Candidate edges for every facet layer at once, since each one's bonus reads the
    others. A picture without a vector in a layer is simply absent from it."""
    bonuses = facet_bonuses(vectors, means, float(tunables.facet_bonus))
    return {
        name: candidate_edges(
            name, vectors[name], means.get(name), tunables, bonus=bonuses.get(name)
        )
        for name in sorted(vectors)
    }
