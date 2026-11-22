"""Roman numerals, and the one-line `<stem>_collection.txt` sidecar.

    pansophicon            file it
    pansophicon iii        pin to book iii, the filer picks the leaf
    pansophicon iii 14     pin to book iii, leaf 14
    pansophicon n          in no book: incertae sedis (`0` as well)

Either number may be roman or arabic, and the book may be `n`, the medieval zero. Ranges
are checked later, against the collection's row: this module knows the grammar, not the
shape of any one collection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ROMAN = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)
_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")
# The book number of the leaves that belong to none of the books: in the collection, placed
# nowhere in it. 0 rather than one past the last book, so a book found later can take that.
LOOSE = 0
# What the loose leaves are called when their row has no title to give.
LOOSE_TITLE = "incertae sedis"


def roman(n: int) -> str:
    """Lowercase, as every label on the site is."""
    if n < 1:
        raise ValueError(f"no numeral for {n}")
    out = []
    for value, glyph in _ROMAN:
        while n >= value:
            out.append(glyph)
            n -= value
    return "".join(out)


def book_name(n: int, loose_title: str | None) -> str:
    """A book as a line names it: its numeral, or for the loose leaves their title, since
    they have no numeral."""
    return (loose_title or LOOSE_TITLE) if n == LOOSE else roman(n)


def parse_number(token: str) -> int:
    """Arabic or roman, either case. Only canonical roman forms: `iiii` is not four, so a
    typo is caught rather than filed somewhere nobody meant."""
    t = token.strip().lower()
    if t.isascii() and t.isdigit():
        n = int(t)
        if n < 1:
            raise ValueError(f"{token!r} is not a book or leaf")
        return n
    if not t or any(c not in "ivxlcdm" for c in t):
        raise ValueError(f"{token!r} is not a number")
    total, i = 0, 0
    for value, glyph in _ROMAN:
        while t.startswith(glyph, i):
            total += value
            i += len(glyph)
    if i != len(t) or total < 1 or roman(total) != t:
        raise ValueError(f"{token!r} is not a roman numeral")
    return total


def parse_book(token: str) -> int:
    """A book number as parse_number reads it, or LOOSE for `0` or `n` (nulla)."""
    t = token.strip().lower()
    if t == "n" or (t.isascii() and t.isdigit() and int(t) == 0):
        return LOOSE
    return parse_number(token)


@dataclass(frozen=True)
class Request:
    collection: str
    book: int | None = None
    position: int | None = None


def parse_line(text: str) -> Request:
    """The sidecar's contents. ValueError says what is wrong, for the queue page."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty")
    if len(lines) > 1:
        raise ValueError("more than one line")
    tokens = lines[0].lower().replace(",", " ").split()
    if len(tokens) > 3:
        raise ValueError(f"too many words: {lines[0]!r}")
    slug = tokens[0]
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"{tokens[0]!r} is not a collection")
    book = parse_book(tokens[1]) if len(tokens) > 1 else None
    position = parse_number(tokens[2]) if len(tokens) > 2 else None
    return Request(slug, book, position)


def format_line(req: Request) -> str:
    parts = [req.collection]
    if req.book is not None:
        parts.append("n" if req.book == LOOSE else roman(req.book))
        if req.position is not None:
            parts.append(str(req.position))
    return " ".join(parts)
