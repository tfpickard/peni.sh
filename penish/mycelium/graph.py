"""Candidate edges per layer: who each picture may grow toward.

Pure: numpy and the standard library. The engine never looks at vectors, only at
the edges built here, so a layer can be fed any id-keyed vectors (whole-picture
embeddings, facet embeddings, or later the need layer's phrase vectors).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from penish.mycelium.tunables import Tunables

FloatArray = NDArray[np.float64]

# Added to a pair's score before neighbors are chosen; the facet bonus slots in
# here so the selection, the stored `raw` and the quantile all see the same number.
Bonus = Callable[[int, int], float]


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
    if bonus is not None:
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
