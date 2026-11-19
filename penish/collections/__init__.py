"""Collections: pictures the owner picks, filed by the machine into books and leaves.

Membership only ever comes from the owner (a checkbox, a sidecar, an admin action).
Nothing here decides that a picture belongs; it only decides where.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Classical forms only, up to the largest book or leaf count a collection may have. A
# loose parser would read `iiii` or `vv` as numbers and file a picture somewhere the owner
# did not mean; refusing them sends it to the queue page instead.
_ROMAN = re.compile(r"^(c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def roman(n: int) -> str:
    """Lowercase roman numeral, as the site prints book numbers."""
    if n <= 0:
        raise ValueError("no numeral for zero or less")
    out = []
    for value, sym in (
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def parse_number(token: str) -> int:
    """Arabic or roman, either case. ValueError for anything else."""
    t = token.strip().lower()
    if t.isascii() and t.isdigit():
        n = int(t)
    elif t and _ROMAN.fullmatch(t):
        n = 0
        prev = 0
        for ch in reversed(t):
            v = _VALUES[ch]
            n = n - v if v < prev else n + v
            prev = max(prev, v)
    else:
        raise ValueError(f"not a number: {token!r}")
    if n <= 0:
        raise ValueError(f"not a number: {token!r}")
    return n


@dataclass(frozen=True)
class Placement:
    """What the owner asked for: a collection, and optionally the book and the leaf."""

    collection: str
    book: int | None = None
    position: int | None = None


def parse_spec(text: str) -> Placement:
    """One sidecar line: `pansophicon`, `pansophicon iii`, `pansophicon iii 14`.

    Only the shape is checked here; whether the collection exists and the numbers fit
    it is the caller's to say, since that needs the database.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty")
    if len(lines) > 1:
        raise ValueError("more than one line")
    parts = lines[0].split()
    if len(parts) > 3:
        raise ValueError(f"too many words: {lines[0]!r}")
    slug = parts[0].lower()
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError(f"not a collection: {parts[0]!r}")
    book = parse_number(parts[1]) if len(parts) > 1 else None
    position = parse_number(parts[2]) if len(parts) > 2 else None
    return Placement(slug, book, position)


def format_spec(p: Placement) -> str:
    """The sidecar line for a placement; the upload form writes this."""
    out = [p.collection]
    if p.book is not None:
        out.append(roman(p.book))
        if p.position is not None:
            out.append(str(p.position))
    return " ".join(out)
