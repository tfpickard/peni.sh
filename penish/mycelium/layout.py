"""Where each picture sits on the map: a seeded force layout over the whole layer.

Pure: numpy and the standard library. The same ids, edges and seed give the same
positions bit for bit, so every api process draws the same map and a map only moves
when the candidate graph does (a new graph_version), never from one tick to the next.

Fruchterman-Reingold in the unit square: every pair repels, a candidate edge pulls in
proportion to its affinity, and a weak pull toward the middle keeps a picture with no
edges from drifting off. The walkers' strengths are not used: they change every tick,
and a map that rearranged itself every ten minutes would be a different map each visit.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Container, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
Positions = dict[int, tuple[float, float]]

# Repulsion is all pairs; this caps pairs times iterations so a big corpus still lays
# out in seconds, with fewer iterations rather than a different algorithm.
WORK = 300_000_000
MAX_ITERATIONS = 300
MIN_ITERATIONS = 40
# Pairs held in memory at once during repulsion: rows per chunk times n.
CHUNK_PAIRS = 2_000_000
# Largest step in the first iteration, as a share of the square; cools to zero.
START_TEMPERATURE = 0.1
GRAVITY = 4.0
DECIMALS = 4


def iterations(n: int) -> int:
    if n < 2:
        return 0
    return max(MIN_ITERATIONS, min(MAX_ITERATIONS, WORK // (n * n)))


def _start(order: Sequence[int], seed: int) -> FloatArray:
    # Each picture's start depends on the seed and its own id only, so a picture keeps
    # roughly its place when others arrive. Raw .random() draws, as in the engine.
    pos = np.empty((len(order), 2), dtype=np.float64)
    for k, v in enumerate(order):
        g = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, v])))
        pos[k, 0] = g.random()
        pos[k, 1] = g.random()
    return pos - 0.5


def _repulsion(pos: FloatArray, k2: float) -> FloatArray:
    n = len(pos)
    out = np.zeros_like(pos)
    rows = max(1, CHUNK_PAIRS // n)
    for start in range(0, n, rows):
        d = pos[start : start + rows, None, :] - pos[None, :, :]
        d2 = np.einsum("ijk,ijk->ij", d, d)
        # A picture against itself has d = 0, so the floor only stops a division by zero.
        np.maximum(d2, 1e-12, out=d2)
        out[start : start + rows] = np.einsum("ijk,ij->ik", d, k2 / d2)
    return out


def undirected(
    edges: Iterable[tuple[int, int, float]], ids: Container[int]
) -> dict[tuple[int, int], float]:
    """Edges between known ids as unordered pairs, the larger weight of the two
    directions, self loops dropped."""
    out: dict[tuple[int, int], float] = {}
    for s, d, w in edges:
        if s == d or s not in ids or d not in ids:
            continue
        key = (s, d) if s < d else (d, s)
        out[key] = max(out.get(key, 0.0), float(w))
    return out


def _normalize(pos: FloatArray) -> FloatArray:
    """Into the unit square, aspect kept, the shorter side centred."""
    lo = pos.min(axis=0)
    span = pos.max(axis=0) - lo
    big = float(span.max())
    if big <= 0:
        return np.full_like(pos, 0.5)
    out = (pos - lo) / big
    out += (1.0 - span / big) / 2.0
    return out


def force_layout(
    ids: Sequence[int],
    edges: Iterable[tuple[int, int, float]],
    seed: int,
    *,
    steps: int | None = None,
) -> Positions:
    """`{id: (x, y)}` in 0..1, rounded to DECIMALS places. `edges` are `(src, dst,
    weight)` in either or both directions; ones touching an id not in `ids` are ignored."""
    order = sorted(set(ids))
    n = len(order)
    if n == 0:
        return {}
    if n == 1:
        return {order[0]: (0.5, 0.5)}
    index = {v: i for i, v in enumerate(order)}
    pairs = undirected(edges, index)
    keys = sorted(pairs)
    ei = np.array([index[a] for a, _ in keys], dtype=np.intp)
    ej = np.array([index[b] for _, b in keys], dtype=np.intp)
    ew = np.array([pairs[p] for p in keys], dtype=np.float64)

    pos = _start(order, seed)
    k = 1.0 / math.sqrt(n)
    k2 = k * k
    its = iterations(n) if steps is None else steps
    for step in range(its):
        disp = _repulsion(pos, k2)
        if len(keys):
            delta = pos[ei] - pos[ej]
            dist = np.sqrt(np.einsum("ij,ij->i", delta, delta))
            pull = delta * (ew * dist / k)[:, None]
            np.subtract.at(disp, ei, pull)
            np.add.at(disp, ej, pull)
        disp -= GRAVITY * pos
        length = np.sqrt(np.einsum("ij,ij->i", disp, disp))
        temp = START_TEMPERATURE * (1.0 - step / its)
        scale = np.minimum(length, temp) / np.maximum(length, 1e-12)
        pos = pos + disp * scale[:, None]

    out = np.round(_normalize(pos), DECIMALS)
    return {v: (float(out[i, 0]), float(out[i, 1])) for i, v in enumerate(order)}


def fingerprint(ids: Sequence[int], edges: Iterable[tuple[int, int, float]], seed: int) -> str:
    """What a layout depends on, hashed: two calls with equal fingerprints give equal
    layouts. Weights are written by float.hex so a rounding difference is a difference."""
    h = hashlib.sha256()
    order = sorted(set(ids))
    h.update(f"seed {seed}\n".encode())
    h.update((" ".join(map(str, order)) + "\n").encode())
    for (a, b), w in sorted(undirected(edges, set(order)).items()):
        h.update(f"{a} {b} {w.hex()}\n".encode())
    return h.hexdigest()
