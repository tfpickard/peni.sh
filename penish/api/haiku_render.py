"""Server-side port of web/src/lib/haiku/{rng,noise,ou}.ts and FungalHaiku.svelte.

The home page is served with the varied haiku already in the markup, so it varies
without JS. Keep the math and the DOM in step with the TypeScript; the vectors in
tests/fixtures/haiku_seed_vectors.json (scripts/haiku_vectors.mjs) pin them together.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Callable
from typing import Literal

Gen = Literal["noise", "ou"]
Rng = Callable[[], float]

_M = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    # Math.imul: 32-bit wrapping multiply. Kept unsigned; every later use is mod 2**32.
    return (a * b) & _M


def mulberry32(seed: int) -> Rng:
    a = seed & _M

    def rng() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & _M
        t = a
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ (t + _imul(t ^ (t >> 7), t | 61))) & _M
        return ((t ^ (t >> 14)) & _M) / 4294967296

    return rng


_OCTAVES = 3
_FREQUENCY = 0.11


def _smooth(t: float) -> float:
    return t * t * (3 - 2 * t)


def noise(n: int, rng: Rng) -> list[float]:
    lattice = [
        [rng() for _ in range(math.ceil(n * _FREQUENCY * 2**o) + 2)] for o in range(_OCTAVES)
    ]
    norm = sum(0.5**o for o in range(_OCTAVES))
    out: list[float] = []
    for i in range(n):
        total = 0.0
        for o in range(_OCTAVES):
            x = i * _FREQUENCY * 2**o
            x0 = math.floor(x)
            f = _smooth(x - x0)
            v = lattice[o][x0] * (1 - f) + lattice[o][x0 + 1] * f
            total += v * 0.5**o
        out.append(min(1.0, max(0.0, total / norm)))
    return out


_THETA = 0.12
_SIGMA = 0.22
_MU = 0.0


def _normal(rng: Rng) -> float:
    return math.sqrt(-2 * math.log(1 - rng())) * math.cos(2 * math.pi * rng())


def ou(n: int, rng: Rng) -> list[float]:
    x = _normal(rng)
    out: list[float] = []
    for _ in range(n):
        x += _THETA * (_MU - x) + _SIGMA * _normal(rng)
        out.append(1 / (1 + math.exp(-2.2 * x)))
    return out


def values_for(lines: list[str], seed: int, gen: Gen) -> list[float]:
    # JS string length counts UTF-16 units; the lattice size depends on it.
    total = sum(len(line.encode("utf-16-le")) // 2 for line in lines)
    return (ou if gen == "ou" else noise)(total, mulberry32(seed))


def axes(v: float) -> tuple[str, str]:
    # Same mapping as FungalHaiku.svelte; one decimal keeps the markup small and stable.
    return f"{60 + v * 840:.1f}", f"{450 - v * 150:.1f}"


def render_haiku_html(lines: list[str], *, seed: int, gen: Gen) -> str:
    values = iter(values_for(lines, seed, gen))
    rows: list[str] = []
    for line in lines:
        parts: list[str] = []
        # Spaces stay plain text so the browser can wrap between words.
        for tok in re.split(r"( )", line):
            if tok == " ":
                parts.append(" ")
                continue
            for ch in tok:
                grow, thck = axes(next(values))
                parts.append(
                    f'<span aria-hidden="true" style="font-variation-settings: '
                    f"'grow' {grow}, 'THCK' {thck}\">{html.escape(ch)}</span>"
                )
        rows.append(f'<span class="haiku-line">{"".join(parts)}</span>')
    label = html.escape(" / ".join(lines))
    return f'<h1 class="haiku" aria-label="{label}">{"".join(rows)}</h1>'
