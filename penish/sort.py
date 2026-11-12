"""Grid orders other than newest, after pix.fish's sort modes.

Each order is computed over the whole live set at once and cached by the corpus
version, so later pages are slices. The corpus is small enough that this is cheaper
than doing it in SQL.

Pictures without a vector keep their base order and follow the ones that have one, so
a picture still waiting on the embedder is neither hidden nor favoured.
"""

from __future__ import annotations

import colorsys
import hashlib
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, get_args

import numpy as np
from numpy.typing import NDArray

Sort = Literal[
    "newest",
    "oldest",
    "random",
    "drifting",
    "oldest-drifting",
    "clumped",
    "anti-clumped",
    "rainbow",
    "time-of-day",
    "lonely",
    "surprising",
    "drunkards-walk",
    "tidal",
]
SORTS: tuple[str, ...] = get_args(Sort)
SEEDED = frozenset({"random", "drunkards-walk"})
# Orders that read vectors; the rest never need them loaded.
EMBEDDED = frozenset(
    {"drifting", "oldest-drifting", "clumped", "anti-clumped", "lonely", "drunkards-walk"}
)

Vecs = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Row:
    id: int
    published_at: datetime
    palette: list[str] | None
    surprise: float | None


def _rng(seed: str) -> np.random.Generator:
    # Python's hash() is salted per process; the order must survive a restart.
    return np.random.default_rng(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8]))


def _unit(v: Vecs) -> Vecs:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return np.divide(v, n, out=np.zeros_like(v), where=n > 0)


def distances(v: Vecs) -> Vecs:
    """Pairwise cosine distance, 0 to 2."""
    u = _unit(v)
    d: Vecs = 1.0 - u @ u.T
    return np.clip(d, 0.0, 2.0)


def drift(d: Vecs, alpha: float = 0.65, lookback: int = 3) -> list[int]:
    """Base order kept roughly, but each pick is pushed away from the last few picks."""
    n = len(d)
    rank = 1.0 - np.arange(n) / max(1, n - 1)
    left = np.ones(n, dtype=bool)
    out = [0]
    left[0] = False
    for _ in range(n - 1):
        near = d[out[-lookback:]].min(axis=0)
        score = alpha * rank + (1 - alpha) * near / 2
        score[~left] = -np.inf
        i = int(score.argmax())
        out.append(i)
        left[i] = False
    return out


def clump(d: Vecs) -> list[int]:
    """Chains of nearest neighbours, each started by the next picture in base order."""
    n = len(d)
    # A fixed break distance depends on the model and on centering; the corpus's own
    # median does not. Lower quantiles snapped chains inside obvious groups.
    off = d[~np.eye(n, dtype=bool)]
    brk = float(np.median(off)) if off.size else 0.0
    left = np.ones(n, dtype=bool)
    out: list[int] = []
    for start in range(n):
        if not left[start]:
            continue
        out.append(start)
        left[start] = False
        while left.any():
            row = np.where(left, d[out[-1]], np.inf)
            i = int(row.argmin())
            if row[i] > brk:
                break
            out.append(i)
            left[i] = False
    return out


def anti_clump(d: Vecs) -> list[int]:
    """Each next picture is the one furthest from everything already placed."""
    n = len(d)
    out = [0]
    near = d[0].copy()
    near[0] = -np.inf
    for _ in range(n - 1):
        i = int(near.argmax())
        out.append(i)
        near = np.minimum(near, d[i])
        near[out] = -np.inf
    return out


def drunkards_walk(d: Vecs, seed: str, jump: float = 0.1, k: int = 5) -> list[int]:
    """A walk to one of the k nearest unvisited pictures, with the odd jump anywhere."""
    n = len(d)
    rng = _rng(seed)
    left = np.ones(n, dtype=bool)
    out = [int(rng.integers(n))]
    left[out[0]] = False
    for _ in range(n - 1):
        pool = np.flatnonzero(left)
        if rng.random() < jump:
            i = int(rng.choice(pool))
        else:
            near = pool[np.argsort(d[out[-1], pool], kind="stable")[:k]]
            i = int(rng.choice(near))
        out.append(i)
        left[i] = False
    return out


def loneliness(d: Vecs, k: int = 5) -> Vecs:
    """Mean distance to the k nearest others, as shown on the picture page."""
    n = len(d)
    if n < 2:
        return np.zeros(n)
    masked = d + np.diag(np.full(n, np.inf))
    k = min(k, n - 1)
    out: Vecs = np.sort(masked, axis=1)[:, :k].mean(axis=1)
    return out


def tidal(n: int) -> list[int]:
    """Newest, oldest, second newest, second oldest, and so on."""
    out: list[int] = []
    lo, hi = 0, n - 1
    while lo <= hi:
        out.append(lo)
        lo += 1
        if lo <= hi:
            out.append(hi)
            hi -= 1
    return out


def hue(palette: Sequence[str] | None) -> float:
    """Hue of the first palette colour, 0 to 1; greys and unknowns after every colour."""
    if not palette:
        return 4.0
    h = palette[0].lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 4.0
    hh, light, sat = colorsys.rgb_to_hls(r, g, b)
    # A hue read off a near-grey is noise; those go after the spectrum, dark to light.
    return 2.0 + light if sat < 0.08 or light < 0.04 or light > 0.96 else hh


def order(
    sort: Sort,
    rows: list[Row],
    vecs: dict[int, Vecs],
    *,
    seed: str = "",
    centered: Vecs | None = None,
) -> list[int]:
    """Image ids in grid order. `rows` arrive newest first; `centered` is the corpus mean."""
    if sort == "newest":
        return [r.id for r in rows]
    if sort == "oldest":
        return [r.id for r in reversed(rows)]
    if sort == "random":
        perm = _rng(seed).permutation(len(rows))
        return [rows[int(i)].id for i in perm]
    if sort == "tidal":
        return [rows[i].id for i in tidal(len(rows))]
    if sort == "rainbow":
        return [r.id for r in sorted(rows, key=lambda r: (hue(r.palette), r.id))]
    if sort == "time-of-day":
        return [
            r.id for r in sorted(rows, key=lambda r: (r.published_at.astimezone(UTC).time(), -r.id))
        ]
    if sort == "surprising":
        # Unscored pictures go last rather than being read as unsurprising.
        return [
            r.id for r in sorted(rows, key=lambda r: (r.surprise is None, -(r.surprise or 0.0)))
        ]

    base = list(reversed(rows)) if sort == "oldest-drifting" else rows
    have = [r.id for r in base if r.id in vecs]
    rest = [r.id for r in base if r.id not in vecs]
    if len(have) < 2:
        return have + rest
    v = np.stack([vecs[i] for i in have])

    if sort == "lonely":
        # Raw, not centered, so the order agrees with the number on each picture's page.
        lone = loneliness(distances(v))
        return [have[int(i)] for i in np.argsort(-lone, kind="stable")] + rest

    d = distances(v - centered if centered is not None else v)
    pick: Callable[[Vecs], list[int]] = {
        "drifting": drift,
        "oldest-drifting": drift,
        "clumped": clump,
        "anti-clumped": anti_clump,
        "drunkards-walk": lambda dd: drunkards_walk(dd, seed),
    }[sort]
    return [have[i] for i in pick(d)] + rest


class OrderCache:
    """The last few orders, keyed by everything that could change them."""

    def __init__(self, size: int = 256) -> None:
        self._size = size
        self._items: OrderedDict[tuple[Any, ...], list[int]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> list[int] | None:
        hit = self._items.get(key)
        if hit is not None:
            self._items.move_to_end(key)
        return hit

    def put(self, key: tuple[Any, ...], value: list[int]) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)
