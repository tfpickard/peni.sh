"""Measure the Pansophicon filer and write a report. Changes nothing.

Usage: uv run python scripts/ptp_filing_report.py [--dsn DSN] [--out DIR] [--anchors FILE]
       [--gold FILE] [--seed N] [--orders N] [--embed-cache FILE]

The DSN defaults to DATABASE_URL. The connection is opened with
default_transaction_read_only=on and every read runs in one transaction: nothing is written
and no row is locked. Every score comes from penish.collection.plan (choose_book,
assign_books, plan_resort) and every vector is centered by penish.collection.store._prep,
so the numbers are the filer's own; this file only arranges inputs and counts results.

"Old" anchors are what the database holds; "new" anchors are the texts and tag hints in
--anchors, embedded here through the worker's Embedder (needs `uv sync --extra worker`).
Gold labels come from --gold; the report says so loudly when they are unconfirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
from psycopg.rows import dict_row

from penish.collection.numerals import roman
from penish.collection.plan import (
    LIMITS,
    Book,
    Leaf,
    Shape,
    Tuning,
    assign_books,
    choose_book,
    plan_resort,
    tag_overlap,
)
from penish.collection.store import _prep
from penish.config import Settings, get_settings
from penish.mycelium import facets

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "reports" / "ptp-filing"
DEFAULT_ANCHORS = ROOT / "docs" / "ptp_anchors.json"
DEFAULT_GOLD = ROOT / "docs" / "ptp_gold.json"
SLUG = "pansophicon"
SEED = 20261120
ORDERS = 50
GRID_K = (3.0, 6.0, 10.0, 20.0)
GRID_ALPHA = (0.0, 0.05, 0.15)
ORDER_K = (3.0, 10.0)
# "Far more than 1/7": twice the even share.
ATTRACTOR = 2.0 / 7.0
CHECK_MIN = 0.999


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    # The stored embedding kind; None for a source embedded by this script.
    kind: str | None


SOURCES = (
    Source("a", "whole analysis", "description"),
    Source("b", "subject paragraph", facets.kind("subject", facets.SPLIT)),
    Source("c", "subject + setting", None),
    Source("d", "subject, extract recipe", facets.kind("subject", facets.EXTRACT)),
    Source("e", "style paragraph", facets.kind("style", facets.SPLIT)),
)
GRID_SOURCES = ("a", "b", "c", "d")
SOURCE = {s.key: s for s in SOURCES}

# First rule that matches the Style paragraph wins; the order settles pictures that name
# two techniques ("woodcut or risograph", "pen linework with watercolor").
MEDIUM_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mosaic", ("mosaic", "tesserae")),
    ("painted manuscript", ("illuminated", "miniature", "gouache", "tempera")),
    ("print", ("woodcut", "engraving", "letterpress", "risograph", "printed")),
    ("drawing", ("pencil", "pen linework", "pen and ink")),
)
_STYLE = re.compile(r"^\s*Style\s*:", re.M)
# The rule words above and their near relatives, looked for in Subject and Setting.
MEDIUM_WORDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [w for _, ws in MEDIUM_RULES for w in ws]
        + ["plate", "print", "manuscript", "parchment", "page", "chart", "sheet", "drawing"]
    )
)
_MEDIUM_RE = {
    w: re.compile(r"\b" + re.escape(w).replace(r"\ ", r"\s+") + r"(?:s|es)?\b", re.I)
    for w in MEDIUM_WORDS
}


# -- pure: inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class LeafRow:
    image_id: int
    slug: str
    book: int
    position: int
    book_pinned: bool
    pinned: bool
    note_source: str | None = None


@dataclass(frozen=True)
class Gold:
    confirmed: bool
    books: dict[str, frozenset[int]]
    why: dict[str, str]


def parse_book(token: str) -> int:
    for n in range(1, 8):
        if roman(n) == token.strip().lower():
            return n
    raise ValueError(f"not a book numeral: {token!r}")


def load_gold(raw: Mapping[str, Any]) -> Gold:
    leaves = raw["leaves"]
    return Gold(
        confirmed=raw.get("confirmed") is True,
        books={s: frozenset(parse_book(b) for b in v["books"]) for s, v in leaves.items()},
        why={s: str(v.get("why", "")) for s, v in leaves.items()},
    )


def load_anchors(raw: Mapping[str, Any]) -> dict[int, tuple[str, frozenset[str]]]:
    return {
        parse_book(k): (str(v["anchor"]), frozenset(str(h) for h in v["hints"]))
        for k, v in raw.items()
    }


def content_text(analysis: str | None) -> str | None:
    """Source (c): the analysis as stored, cut at the Style label, so it differs from (a)
    only by the Style and Mood paragraphs. None when the analysis does not split."""
    if facets.split(analysis) is None:
        return None
    assert analysis is not None
    m = _STYLE.search(analysis)
    assert m is not None
    return analysis[: m.start()].strip()


def medium_words(text: str | None) -> list[str]:
    """MEDIUM_WORDS found in a text as whole words (plurals too), in list order."""
    return [w for w in MEDIUM_WORDS if _MEDIUM_RE[w].search(text or "")]


def medium(style: str | None) -> tuple[str, str]:
    """(medium, the word that decided it) from a Style paragraph."""
    text = (style or "").lower()
    for name, words in MEDIUM_RULES:
        for w in words:
            if w in text:
                return name, w
    return "other", ""


# -- pure: the filer's spaces ----------------------------------------------------------


@dataclass
class Space:
    """One vector source, anchor set and centering, prepared as store.load prepares them."""

    shape: Shape
    leaves: list[Leaf]
    # Every live picture, prepared like the leaves.
    vecs: dict[int, np.ndarray | None]
    tags: dict[int, frozenset[str]]

    def tuned(self, anchor_k: float, tag_alpha: float) -> Space:
        t = replace(self.shape.tuning, anchor_k=anchor_k, tag_alpha=tag_alpha)
        return replace(self, shape=replace(self.shape, tuning=t))

    def leaf(self, image_id: int) -> Leaf:
        return next(x for x in self.leaves if x.image_id == image_id)


def make_space(
    rows: Sequence[LeafRow],
    raw: Mapping[int, np.ndarray | None],
    tags: Mapping[int, frozenset[str]],
    anchors: Mapping[int, tuple[np.ndarray | None, frozenset[str]]],
    mean: np.ndarray | None,
    cap: int,
    tuning: Tuning,
) -> Space:
    books = [Book(n, _prep(v, mean), hints) for n, (v, hints) in sorted(anchors.items())]
    vecs = {i: _prep(v, mean) for i, v in raw.items()}
    leaves = [
        Leaf(
            r.image_id,
            r.book,
            r.position,
            vecs.get(r.image_id),
            tags.get(r.image_id, frozenset()),
            r.book_pinned,
            r.pinned,
        )
        for r in sorted(rows, key=lambda r: r.image_id)
    ]
    return Space(Shape(books, cap, tuning), leaves, vecs, dict(tags))


# -- pure: scores -----------------------------------------------------------------------


@dataclass(frozen=True)
class Terms:
    cos: float
    tag: float
    fill: float
    total: float


@dataclass(frozen=True)
class Pick:
    book: int
    runner_up: int | None
    margin: float
    scores: dict[int, float]


def ranked(scores: Mapping[int, float]) -> list[int]:
    # plan._best's tie rule: the lower book wins a tie.
    return sorted(scores, key=lambda n: (-scores[n], n))


def pick(scores: Mapping[int, float]) -> Pick:
    order = ranked(scores)
    best = order[0]
    runner = order[1] if len(order) > 1 else None
    margin = scores[best] - scores[runner] if runner is not None else float("inf")
    return Pick(best, runner, margin, dict(scores))


def _scores(shape: Shape, leaves: list[Leaf], vec: np.ndarray, tags: frozenset[str]) -> Pick:
    got = choose_book(shape, leaves, vec, tags)
    if got is None:
        raise ValueError("every book is full")
    p = pick(got[1])
    assert p.book == got[0]
    return p


def breakdown(
    shape: Shape, leaves: list[Leaf], vec: np.ndarray, tags: frozenset[str]
) -> dict[int, Terms]:
    """choose_book's terms per book. plan.score returns only the sum, so each term is
    read off choose_book itself with the other weights at zero."""
    t = shape.tuning
    bare = _scores(
        replace(shape, tuning=replace(t, tag_alpha=0.0, fill_beta=0.0)), leaves, vec, tags
    )
    tagged = _scores(replace(shape, tuning=replace(t, fill_beta=0.0)), leaves, vec, tags)
    full = _scores(shape, leaves, vec, tags)
    return {
        n: Terms(
            cos=bare.scores[n],
            tag=tagged.scores[n] - bare.scores[n],
            fill=full.scores[n] - tagged.scores[n],
            total=full.scores[n],
        )
        for n in full.scores
    }


def others(leaves: Iterable[Leaf], image_id: int) -> list[Leaf]:
    return [x for x in leaves if x.image_id != image_id]


def loo(shape: Shape, leaves: list[Leaf], leaf: Leaf) -> Pick:
    """The leaf scored with itself out of its own book's centroid and fill: what the filer
    would choose for it if it were filed now against everyone else where they stand."""
    assert leaf.vec is not None
    return _scores(shape, others(leaves, leaf.image_id), leaf.vec, leaf.tags)


def with_self(shape: Shape, leaves: list[Leaf], leaf: Leaf) -> Pick:
    assert leaf.vec is not None
    return _scores(shape, leaves, leaf.vec, leaf.tags)


def score_picture(space: Space, image_id: int) -> Pick | None:
    """A leaf by leave-one-out; any other picture against every leaf as it stands."""
    vec = space.vecs.get(image_id)
    if vec is None:
        return None
    rest = others(space.leaves, image_id)
    return _scores(space.shape, rest, vec, space.tags.get(image_id, frozenset()))


# -- pure: counting ------------------------------------------------------------------


@dataclass(frozen=True)
class Tally:
    right: int
    n: int
    rights: list[str]
    wrongs: list[tuple[str, int]]

    @property
    def share(self) -> float:
        return self.right / self.n if self.n else float("nan")


def accuracy(
    choice: Mapping[str, int], gold: Mapping[str, frozenset[int]], slugs: Iterable[str]
) -> Tally:
    """A leaf is right when its book is any one of its gold books."""
    rights: list[str] = []
    wrongs: list[tuple[str, int]] = []
    for s in slugs:
        if s not in gold or s not in choice:
            continue
        if choice[s] in gold[s]:
            rights.append(s)
        else:
            wrongs.append((s, choice[s]))
    return Tally(len(rights), len(rights) + len(wrongs), rights, wrongs)


def chance(gold: Mapping[str, frozenset[int]], slugs: Iterable[str], books: int = 7) -> float:
    """Expected right count for a uniform random book per leaf."""
    return sum(len(gold[s]) / books for s in slugs if s in gold)


def random_orders(ids: Sequence[int], n: int, seed: int) -> list[list[int]]:
    rng = np.random.default_rng(seed)
    base = sorted(ids)
    return [[base[i] for i in rng.permutation(len(base))] for _ in range(n)]


def sequential(
    shape: Shape, leaves: Sequence[Leaf], order: Sequence[int], honour_pins: bool
) -> dict[int, int]:
    """File the leaves one at a time in `order`, each against those filed before it, as the
    filing job does (store._place: choose_book over the others). A book-pinned leaf keeps
    its owner's book when honour_pins, as it did when the owner placed it."""
    by_id = {x.image_id: x for x in leaves}
    placed: list[Leaf] = []
    out: dict[int, int] = {}
    for i in order:
        leaf = by_id[i]
        if (honour_pins and leaf.book_pinned) or leaf.vec is None:
            book = leaf.book
        else:
            got = choose_book(shape, placed, leaf.vec, leaf.tags)
            if got is None:
                raise ValueError("every book is full")
            book = got[0]
        out[i] = book
        placed.append(replace(leaf, book=book))
    return out


def order_counts(
    shape: Shape, leaves: Sequence[Leaf], orders: Sequence[Sequence[int]], honour_pins: bool
) -> dict[int, Counter[int]]:
    out: dict[int, Counter[int]] = {x.image_id: Counter() for x in leaves}
    for order in orders:
        for i, b in sequential(shape, leaves, order, honour_pins).items():
            out[i][b] += 1
    return out


def ranks(xs: Sequence[float]) -> np.ndarray:
    """1-based ranks, ties sharing their average rank."""
    a = np.asarray(xs, dtype=np.float64)
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=np.float64)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        r[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman's rho with average ranks for ties; nan when either side is constant."""
    if len(x) != len(y) or len(x) < 2:
        return float("nan")
    rx, ry = ranks(x), ranks(y)
    rx -= rx.mean()
    ry -= ry.mean()
    den = float(np.sqrt((rx * rx).sum() * (ry * ry).sum()))
    return float((rx * ry).sum() / den) if den > 0 else float("nan")


@dataclass(frozen=True)
class PairStat:
    n_pairs: int
    rho: float
    n_same: int
    mean_same: float
    mean_diff: float


def pair_stat(
    vecs: Sequence[np.ndarray],
    same: Sequence[Sequence[bool]],
    keep: Sequence[Sequence[bool]] | None = None,
) -> PairStat:
    """Rank correlation of pairwise cosine with a same-group indicator, over i < j, and
    only over the pairs `keep` allows when it is given."""
    sims: list[float] = []
    ind: list[float] = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if keep is not None and not keep[i][j]:
                continue
            sims.append(float(np.dot(vecs[i], vecs[j])))
            ind.append(1.0 if same[i][j] else 0.0)
    s = np.asarray(sims)
    m = np.asarray(ind) > 0
    return PairStat(
        len(sims),
        spearman(sims, ind),
        int(m.sum()),
        float(s[m].mean()) if m.any() else float("nan"),
        float(s[~m].mean()) if (~m).any() else float("nan"),
    )


def rebook(leaves: Iterable[Leaf], books: Mapping[int, int]) -> list[Leaf]:
    """The leaves with some moved to other books; flags and vectors kept."""
    return [replace(x, book=books.get(x.image_id, x.book)) for x in leaves]


def dehyphen(tags: Iterable[str]) -> frozenset[str]:
    """Tags with hyphens read as spaces, so tag_overlap's word split can reach inside
    `zodiac-man`. Simulates a filer change; plan.py does not do this."""
    return frozenset(t.replace("-", " ") for t in tags)


def gold_books(
    shape: Shape, leaves: list[Leaf], gold: Mapping[int, frozenset[int]]
) -> dict[int, int]:
    """One gold book per labelled leaf. A leaf with several takes the one it scores higher
    in by choose_book, against every single-label leaf placed in its gold book."""
    single = {i: next(iter(g)) for i, g in gold.items() if len(g) == 1}
    placed = [x for x in rebook(leaves, single) if x.image_id in single]
    out = dict(single)
    for x in leaves:
        g = gold.get(x.image_id, frozenset())
        if len(g) < 2:
            continue
        if x.vec is None:
            out[x.image_id] = min(g)
            continue
        s = _scores(shape, others(placed, x.image_id), x.vec, x.tags).scores
        out[x.image_id] = ranked({n: s[n] for n in g})[0]
    return out


def upper_bound(
    shape: Shape, leaves: list[Leaf], gold: Mapping[int, frozenset[int]]
) -> tuple[dict[int, int], dict[int, Pick]]:
    """Every leaf in its gold book, then each leaf's LOO choice against the rest: how the
    filer files a new leaf into a collection that is already right."""
    where = gold_books(shape, leaves, gold)
    state = rebook(leaves, where)
    picks = {x.image_id: loo(shape, state, x) for x in state if x.vec is not None}
    return where, picks


@dataclass(frozen=True)
class Proposal:
    image_id: int
    before: tuple[int, int]
    after: tuple[int, int]
    book_pinned: bool
    pinned: bool
    # Score at the proposed book minus the best other book, against the plan's final state.
    margin: float
    # Score at the proposed book minus the score at the current book, same state.
    gain: float


def before_after(shape: Shape, leaves: list[Leaf]) -> list[Proposal]:
    """What a full re-sort would do, through plan_resort, so pins hold as in production."""
    plan = plan_resort(shape, leaves)
    where = {x.image_id: plan[x.image_id][0] for x in leaves}
    moved = [replace(x, book=where[x.image_id]) for x in leaves]
    out: list[Proposal] = []
    for leaf in leaves:
        to = where[leaf.image_id]
        if leaf.vec is None:
            margin = gain = float("nan")
        else:
            s = _scores(shape, others(moved, leaf.image_id), leaf.vec, leaf.tags).scores
            margin = s[to] - max(v for n, v in s.items() if n != to)
            gain = s[to] - s[leaf.book]
        out.append(
            Proposal(
                leaf.image_id,
                (leaf.book, leaf.position),
                plan[leaf.image_id],
                leaf.book_pinned,
                leaf.pinned,
                margin,
                gain,
            )
        )
    return out


@dataclass
class Cell:
    acc_loo_all: float
    acc_loo_unpinned: float
    mean_margin: float
    flip_loo: float
    acc_resort_unpinned: float
    flip_resort: float
    acc_seq_unpinned: float


def evaluate(
    space: Space,
    gold: Mapping[int, frozenset[int]],
    orders: Sequence[Sequence[int]],
) -> Cell:
    """One grid cell. Accuracy counts leaves with gold only; flips compare with the stored
    book. Sequential accuracy is the mean over `orders`, pins honoured."""
    shape, leaves = space.shape, space.leaves
    scored = [x for x in leaves if x.vec is not None and x.image_id in gold]
    unpinned = [x for x in scored if not x.book_pinned]
    picks = {x.image_id: loo(shape, leaves, x) for x in scored}

    def acc(ids: Iterable[int], book: Mapping[int, int]) -> float:
        ids = list(ids)
        return sum(book[i] in gold[i] for i in ids) / len(ids) if ids else float("nan")

    loo_book = {i: p.book for i, p in picks.items()}
    where = assign_books(shape, leaves)
    seq = [
        acc((x.image_id for x in unpinned), sequential(shape, leaves, o, honour_pins=True))
        for o in orders
    ]
    return Cell(
        acc_loo_all=acc((x.image_id for x in scored), loo_book),
        acc_loo_unpinned=acc((x.image_id for x in unpinned), loo_book),
        mean_margin=float(np.mean([p.margin for p in picks.values()])) if picks else float("nan"),
        flip_loo=sum(loo_book[x.image_id] != x.book for x in scored) / len(scored)
        if scored
        else float("nan"),
        acc_resort_unpinned=acc((x.image_id for x in unpinned), where),
        flip_resort=sum(where[x.image_id] != x.book for x in unpinned) / len(unpinned)
        if unpinned
        else float("nan"),
        acc_seq_unpinned=float(np.mean(seq)) if seq else float("nan"),
    )


def gold_centroid_pick(space: Space, gold: Mapping[int, frozenset[int]], leaf: Leaf) -> int | None:
    """Nearest centroid of the other leaves' gold books, no anchors, no tags, no fill.
    Leaves with more than one gold book build no centroid. A book with no other gold leaf
    has no centroid and cannot be chosen."""
    if leaf.vec is None:
        return None
    members = [
        replace(x, book=next(iter(gold[x.image_id])))
        for x in space.leaves
        if x.image_id != leaf.image_id and len(gold.get(x.image_id, ())) == 1
    ]
    books = [Book(b.number, None, frozenset()) for b in space.shape.books]
    shape = Shape(books, space.shape.cap, Tuning(anchor_k=0.0, tag_alpha=0.0, fill_beta=0.0))
    s = _scores(shape, members, leaf.vec, frozenset()).scores
    have = {x.book for x in members if x.vec is not None}
    live = {n: v for n, v in s.items() if n in have}
    return ranked(live)[0] if live else None


def anchor_pick(space: Space, leaf: Leaf) -> int:
    """Nearest anchor by cosine alone: no leaves, no tags, no fill."""
    assert leaf.vec is not None
    shape = replace(space.shape, tuning=replace(space.shape.tuning, tag_alpha=0.0, fill_beta=0.0))
    return _scores(shape, [], leaf.vec, leaf.tags).book


# -- reading -------------------------------------------------------------------------


@dataclass
class Pic:
    image_id: int
    slug: str
    tags: frozenset[str]
    analysis: str | None
    published_at: datetime | None
    raw: dict[str, np.ndarray] = field(default_factory=dict)


@dataclass
class BookRow:
    number: int
    title: str
    anchor_text: str
    hints: frozenset[str]
    vec: np.ndarray | None


@dataclass
class Db:
    collection: dict[str, Any]
    tuning: Tuning
    cap: int
    books: list[BookRow]
    leaves: list[LeafRow]
    pics: dict[int, Pic]
    means: dict[str, np.ndarray | None]
    kind_n: dict[str, int]
    stats_mean: dict[str, np.ndarray | None]
    note_jobs: dict[str, int]
    first_filed: list[tuple[int, int, str]]
    where: str


def _conn(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    conn = psycopg.connect(dsn, options="-c default_transaction_read_only=on", row_factory=dict_row)
    conn.read_only = True
    return conn


def read_db(dsn: str, settings: Settings) -> Db:
    kinds = [s.kind for s in SOURCES if s.kind is not None]
    with _conn(dsn) as conn:
        ro = conn.execute("show transaction_read_only").fetchone()
        if ro is None or ro["transaction_read_only"] != "on":
            sys.exit("the connection is not read-only; refusing to go on")
        info = conn.info
        where = f"`{info.dbname}` at {info.host}:{info.port}"
        collection = conn.execute("select * from collections where slug = %s", (SLUG,)).fetchone()
        if collection is None:
            sys.exit(f"no collection {SLUG}")
        params = {"c": collection["id"], "m": settings.embed_model, "d": settings.embed_dim}
        # The same selects store.load makes, plus text and flags it does not need.
        books = [
            BookRow(
                r["number"],
                r["title"] or "",
                r["anchor_text"] or "",
                frozenset(r["tag_hints"] or ()),
                None if r["anchor"] is None else np.asarray(r["anchor"], dtype=np.float64),
            )
            for r in conn.execute(
                """
                select number, title, anchor_text, tag_hints,
                       case when anchor_model = %(m)s and anchor_dim = %(d)s
                            then anchor_embedding::real[] end as anchor
                  from collection_books where collection_id = %(c)s order by number
                """,
                params,
            ).fetchall()
        ]
        leaves = [
            LeafRow(
                r["image_id"],
                r["slug"],
                r["book"],
                r["position"],
                r["book_pinned"],
                r["pinned"],
                r["note_source"] if r["has_note"] else None,
            )
            for r in conn.execute(
                """
                select l.image_id, i.slug, l.book, l.position, l.book_pinned, l.pinned,
                       l.note_source, l.note is not null as has_note
                  from collection_leaves l join images i on i.id = l.image_id
                 where l.collection_id = %(c)s order by l.image_id
                """,
                params,
            ).fetchall()
        ]
        pics = {
            r["id"]: Pic(
                r["id"],
                r["slug"],
                frozenset(r["tags"]),
                r["analysis"],
                r["published_at"],
            )
            for r in conn.execute(
                """
                select i.id, i.slug, i.published_at,
                       (select body from image_texts t
                         where t.image_id = i.id and t.field = 'analysis') as analysis,
                       coalesce((select array_agg(t.tag) from tags t where t.image_id = i.id),
                                '{}') as tags
                  from images i where i.status = 'live' order by i.id
                """
            ).fetchall()
        }
        for r in conn.execute(
            """
            select e.image_id, e.kind, e.vec::real[] as vec
              from embeddings e join images i on i.id = e.image_id
             where i.status = 'live' and e.model = %(m)s and e.dim = %(d)s
               and e.kind = any(%(k)s)
            """,
            {**params, "k": kinds},
        ):
            src = next(s.key for s in SOURCES if s.kind == r["kind"])
            pics[r["image_id"]].raw[src] = np.asarray(r["vec"], dtype=np.float64)

        # store._mean's rule, for each stored kind: the corpus mean once enough live
        # pictures have a vector of that kind, else none.
        means: dict[str, np.ndarray | None] = {}
        kind_n: dict[str, int] = {}
        stats_mean: dict[str, np.ndarray | None] = {}
        for s in SOURCES:
            if s.kind is None:
                continue
            row = conn.execute(
                """
                select (select count(*) from embeddings e join images i on i.id = e.image_id
                         where e.kind = %(k)s and e.model = %(m)s and e.dim = %(d)s
                           and i.status = 'live') as n,
                       (select mean::real[] from corpus_stats
                         where kind = %(k)s and model = %(m)s and vector_dims(mean) = %(d)s)
                         as mean
                """,
                {"k": s.kind, "m": settings.embed_model, "d": settings.embed_dim},
            ).fetchone()
            assert row is not None
            n = int(row["n"])
            mean = None if row["mean"] is None else np.asarray(row["mean"], dtype=np.float64)
            kind_n[s.key] = n
            stats_mean[s.key] = mean
            means[s.key] = mean if n >= settings.centered_min_images else None

        note_jobs = {
            r["status"]: int(r["n"])
            for r in conn.execute(
                "select status::text as status, count(*) as n from jobs"
                " where type = 'collection_note' group by 1"
            ).fetchall()
        }
        first_filed = [
            (int(r["image_id"]), int(r["to_book"]), str(r["reason"]))
            for r in conn.execute(
                """
                select image_id, to_book, reason from (
                  select distinct on (image_id) id, image_id, to_book, reason
                    from collection_leaf_history
                   where collection_id = %(c)s and from_book is null
                   order by image_id, id) f
                 order by id
                """,
                params,
            ).fetchall()
        ]
        conn.rollback()
    return Db(
        collection=dict(collection),
        tuning=Tuning.from_json(collection["tuning"]),
        cap=int(collection["leaves_per_book"]),
        books=books,
        leaves=leaves,
        pics=pics,
        means=means,
        kind_n=kind_n,
        stats_mean=stats_mean,
        note_jobs=note_jobs,
        first_filed=first_filed,
        where=where,
    )


async def _store_load(dsn: str, settings: Settings) -> Any:
    """store.load itself, for a cross-check of the reads above. It selects only; the
    advisory lock is a separate call that this never makes."""
    from penish.collection import store

    conn = await psycopg.AsyncConnection.connect(
        dsn, options="-c default_transaction_read_only=on", row_factory=dict_row
    )
    try:
        collection = await store.collection_by_slug(conn, SLUG)
        assert collection is not None
        state = await store.load(conn, collection, settings)
        await conn.rollback()
        return state
    finally:
        await conn.close()


# -- embedding ---------------------------------------------------------------------


class Embeds:
    """The worker's Embedder, with an optional on-disk cache keyed by model and text."""

    def __init__(self, settings: Settings, cache: Path | None) -> None:
        self.settings = settings
        self.cache = cache
        self.memo: dict[str, np.ndarray] = {}
        if cache is not None and cache.exists():
            with np.load(cache) as z:
                self.memo = {k: z[k] for k in z.files}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.settings.embed_model}\0{text}".encode()).hexdigest()

    def documents(self, texts: list[str], *, fresh: bool = False) -> np.ndarray:
        """One embed_documents call for every text not cached (all of them when fresh), so
        a batch is a batch as the worker would send it."""
        from penish.worker.embed import Embedder

        keys = [self._key(t) for t in texts]
        todo = [i for i, k in enumerate(keys) if fresh or k not in self.memo]
        if todo:
            out = asyncio.run(Embedder(self.settings).embed_documents([texts[i] for i in todo]))
            vecs = np.asarray(out, dtype=np.float32)
            for i, v in zip(todo, vecs, strict=True):
                self.memo[keys[i]] = v
            if self.cache is not None and not fresh:
                np.savez(self.cache, **self.memo)  # type: ignore[arg-type]
        return np.stack([self.memo[k] for k in keys]).astype(np.float64)


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


# -- report helpers ------------------------------------------------------------------


def f4(x: float) -> str:
    return "n/a" if x != x else f"{x:.4f}"


def pct(x: float) -> str:
    return "n/a" if x != x else f"{100 * x:.0f}%"


def bk(n: int | None) -> str:
    return "-" if n is None else roman(n)


def books_str(ns: Iterable[int]) -> str:
    return " or ".join(roman(n) for n in sorted(ns))


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            w.writerow([f"{c:.8g}" if isinstance(c, float) else c for c in r])


# -- the run ---------------------------------------------------------------------------


@dataclass
class Run:
    db: Db
    settings: Settings
    gold_file: Gold
    new_anchors: dict[int, tuple[str, frozenset[str]]]
    seed: int
    n_orders: int
    out: Path
    md: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    new_vecs: dict[int, np.ndarray] = field(default_factory=dict)
    # Means for every source, (c) included, by the filer's rule.
    means: dict[str, np.ndarray | None] = field(default_factory=dict)

    # -- spaces

    def raw(self, source: str) -> dict[int, np.ndarray | None]:
        return {i: p.raw.get(source) for i, p in self.db.pics.items()}

    def anchors(self, which: str) -> dict[int, tuple[np.ndarray | None, frozenset[str]]]:
        if which == "old":
            return {b.number: (b.vec, b.hints) for b in self.db.books}
        return {n: (self.new_vecs[n], hints) for n, (_, hints) in self.new_anchors.items()}

    def space(
        self, source: str, which: str, centered: bool = True, tuning: Tuning | None = None
    ) -> Space:
        return make_space(
            self.db.leaves,
            self.raw(source),
            {i: p.tags for i, p in self.db.pics.items()},
            self.anchors(which),
            self.means[source] if centered else None,
            self.db.cap,
            tuning or self.db.tuning,
        )

    @property
    def gold(self) -> dict[int, frozenset[int]]:
        by_slug = {r.slug: r.image_id for r in self.db.leaves}
        return {by_slug[s]: b for s, b in self.gold_file.books.items() if s in by_slug}

    def slug(self, image_id: int) -> str:
        return self.db.pics[image_id].slug if image_id in self.db.pics else str(image_id)

    def unpinned(self) -> list[LeafRow]:
        return [r for r in self.db.leaves if not r.book_pinned]

    def h(self, text: str) -> None:
        self.md.append(text)


def embed_all(run: Run, embeds: Embeds) -> None:
    db = run.db
    t0 = time.perf_counter()
    # The check: one stored anchor, one stored analysis and one stored subject paragraph,
    # each embedded fresh, as the worker embeds them.
    check_book = db.books[0]
    leaf_id = db.leaves[0].image_id
    pic = db.pics[leaf_id]
    split = facets.split(pic.analysis)
    assert split is not None and check_book.vec is not None
    old_texts = [b.anchor_text for b in db.books]
    fresh_anchors = embeds.documents(old_texts, fresh=True)
    fresh_analysis = embeds.documents([pic.analysis or ""], fresh=True)[0]
    fresh_subject = embeds.documents([split["subject"]], fresh=True)[0]
    results = [
        (f"book {roman(b.number)} anchor (batch of 7, as collection_anchor)", cos(v, b.vec))
        for b, v in zip(db.books, fresh_anchors, strict=True)
        if b.vec is not None
    ]
    results.append(
        (f"analysis of {pic.slug} vs stored description", cos(fresh_analysis, pic.raw["a"]))
    )
    results.append(
        (
            f"subject paragraph of {pic.slug} vs stored facet:subject/split",
            cos(fresh_subject, pic.raw["b"]),
        )
    )
    run.checks = [f"{name}: cosine {c:.6f}" for name, c in results]
    worst = min(c for _, c in results)
    if worst < CHECK_MIN:
        sys.exit(
            f"embedder check failed: worst cosine {worst:.6f} < {CHECK_MIN}; "
            "locally embedded vectors would not match the stored ones"
        )

    nums = sorted(run.new_anchors)
    vecs = embeds.documents([run.new_anchors[n][0] for n in nums])
    run.new_vecs = {n: v for n, v in zip(nums, vecs, strict=True)}

    ids = sorted(db.pics)
    texts = {i: content_text(db.pics[i].analysis) for i in ids}
    have = [i for i in ids if texts[i] is not None]
    got = embeds.documents([texts[i] or "" for i in have])
    for i, v in zip(have, got, strict=True):
        db.pics[i].raw["c"] = v
    run.timings["embedding"] = time.perf_counter() - t0


def set_means(run: Run) -> None:
    db = run.db
    run.means = dict(db.means)
    c = [p.raw["c"] for p in db.pics.values() if "c" in p.raw]
    # corpus_stats' mean is avg(vec) over live pictures; (c) has no row, so it is taken
    # the same way here, under the same threshold.
    run.means["c"] = (
        np.mean(np.stack(c), axis=0) if len(c) >= run.settings.centered_min_images else None
    )


def cross_check(run: Run, dsn: str) -> None:
    db = run.db
    a = [p.raw["a"] for p in db.pics.values() if "a" in p.raw]
    ours = np.mean(np.stack(a), axis=0)
    stored = db.stats_mean["a"]
    if stored is not None:
        run.checks.append(
            "mean of the live description vectors vs corpus_stats description mean:"
            f" max abs difference {float(np.max(np.abs(ours - stored))):.2e}"
        )
    state = asyncio.run(_store_load(dsn, run.settings))
    sp = run.space("a", "old")
    diffs = [0.0]
    same = len(state.leaves) == len(sp.leaves)
    for x, y in zip(state.leaves, sp.leaves, strict=False):
        same &= (x.image_id, x.book, x.book_pinned, x.pinned, x.tags) == (
            y.image_id,
            y.book,
            y.book_pinned,
            y.pinned,
            y.tags,
        )
        if x.vec is not None and y.vec is not None:
            diffs.append(float(np.max(np.abs(x.vec - y.vec))))
        else:
            same &= x.vec is None and y.vec is None
    for bx, by in zip(state.shape.books, sp.shape.books, strict=True):
        same &= bx.hints == by.hints
        if bx.anchor is not None and by.anchor is not None:
            diffs.append(float(np.max(np.abs(bx.anchor - by.anchor))))
    same &= state.shape.tuning == sp.shape.tuning and state.shape.cap == sp.shape.cap
    run.checks.append(
        "store.load vs this script's reads (source a, old anchors, centered): "
        + ("identical" if same else "DIFFERENT")
        + f", max abs vector difference {max(diffs):.2e}"
    )
    if not same:
        sys.exit("this script's reads do not reproduce store.load")


# -- sections ---------------------------------------------------------------------------


def section_method(run: Run) -> None:
    db = run.db
    newest = max((p.published_at for p in db.pics.values() if p.published_at), default=None)
    t = db.tuning
    h = run.h
    if not run.gold_file.confirmed:
        h("> **UNCONFIRMED GOLD LABELS.** `docs/ptp_gold.json` says `confirmed: false`. Every")
        h("> accuracy figure below counts against labels the owner has not confirmed.")
        h("")
    h("## Method")
    h("")
    h(f"- Database: {db.where}, read in one read-only transaction.")
    h(
        f"- Holdings: {len(db.pics)} live pictures, newest published"
        f" {newest.isoformat(timespec='seconds') if newest else 'n/a'};"
        f" {len(db.leaves)} leaves in `{SLUG}`, {len(run.unpinned())} of them not book-pinned."
    )
    h(
        f"- Tuning from the database: anchor_k {t.anchor_k:g}, tag_alpha {t.tag_alpha:g},"
        f" fill_beta {t.fill_beta:g}, book_margin {t.book_margin:g}; {db.cap} leaves per book."
        " Values not varied in a section are these."
    )
    h(
        f"- Embedding model {run.settings.embed_model} ({run.settings.embed_dim} dimensions)."
        f" Centering follows `store._mean`: a kind's corpus mean is subtracted when at least"
        f" {run.settings.centered_min_images} live pictures have a vector of that kind."
    )
    h("- Vector sources:")
    for s in SOURCES:
        n = sum(1 for p in db.pics.values() if s.key in p.raw)
        centered = (
            "centered on its own corpus mean" if run.means[s.key] is not None else "not centered"
        )
        if s.kind is None:
            what = (
                "embedded here: the stored analysis cut at its `Style:` label (Subject and"
                " Setting paragraphs, labels kept), one embed_documents batch"
            )
        elif s.key == "a":
            what = (
                "stored `description` kind: the whole four-paragraph analysis (what the filer uses)"
            )
        elif s.key == "b":
            what = (
                "stored `facet:subject/split`: the Subject paragraph body cut from the analysis"
                " by `facets.split` (label removed)"
            )
        elif s.key == "d":
            what = (
                "stored `facet:subject/extract`: the subject written by a separate vision call"
                " (the `extract` recipe); (b) already stands for the split recipe"
            )
        else:
            what = "stored `facet:style/split`: the Style paragraph body cut from the analysis"
        h(f"  - ({s.key}) {s.label}: {what}; {n} pictures; {centered}.")
    h(
        "- Anchors: old = the database's anchor texts, tag hints and stored anchor vectors;"
        " new = the texts and tag hints in `docs/ptp_anchors.json` (section B of the brief),"
        " embedded here. Under every source the same anchor vector is used, centered with that"
        " source's mean, as `store.load` centers anchors with the picture mean."
    )
    h(
        "- Scores: `choose_book`, `assign_books` and `plan_resort` from"
        " `penish/collection/plan.py`. `plan.score` returns only the total, so the cos, tag"
        " and fill terms are read off `choose_book` run with the other weights at zero."
    )
    h(
        "- Leave-one-out (LOO): the leaf scored with every other leaf where it stands and"
        " itself removed from its book's centroid and fill; this is `choose_book` over the"
        " others, which is how a filing or a re-sort sees it."
    )
    same = [
        roman(b.number)
        for b in db.books
        if b.number in run.new_anchors
        and run.new_anchors[b.number][0] == b.anchor_text
        and run.new_anchors[b.number][1] == b.hints
    ]
    h(
        "- Books whose anchor text and tag hints in the file equal the database's:"
        f" {', '.join(same) or 'none'}."
    )
    h(
        "- Pins at gold (C5b): a counterfactual state with the book-pinned leaves moved to"
        " their gold books and still book-pinned there. Upper bound (C5b): every leaf in its"
        " gold book, the two-book leaf in the one of its two it scores higher in."
    )
    h(
        "- Tags with hyphens as spaces (C5c): simulates a change to the filer that does not"
        " exist yet. `plan.tag_overlap` splits tags on whitespace and the live tags are"
        " hyphenated; this variant hands the plan functions the tags with hyphens turned into"
        " spaces. Every other section uses the tags as stored."
    )
    h(
        "- Medium words (C3b): the medium rule words plus near relatives, counted as whole words"
        " in the Subject and Setting paragraphs."
    )
    h(f"- Seed {run.seed}; {run.n_orders} random orders, the same orders in every section.")
    h("- Checks:")
    for c in run.checks:
        h(f"  - {c}")
    h("")


def section_c1(run: Run) -> None:
    db = run.db
    sp = run.space("a", "old")
    shape, leaves = sp.shape, sp.leaves
    where = assign_books(shape, leaves)
    rows: list[list[Any]] = []
    summary: list[list[Any]] = []
    detail: list[str] = []
    d = detail.append
    for r in db.leaves:
        leaf = sp.leaf(r.image_id)
        if leaf.vec is None:
            d(f"### {r.slug}: no vector")
            d("")
            continue
        own = breakdown(shape, leaves, leaf.vec, leaf.tags)
        alone = breakdown(shape, others(leaves, leaf.image_id), leaf.vec, leaf.tags)
        p_self = pick({n: t.total for n, t in own.items()})
        p_loo = pick({n: t.total for n, t in alone.items()})
        flags = f"book_pinned {str(r.book_pinned).lower()}, pinned {str(r.pinned).lower()}"
        d(f"### {r.slug}")
        d("")
        d(f"Stored in {roman(r.book)}, leaf {r.position}; {flags}.")
        d("")
        d(
            table(
                ["book", "cos", "tag", "fill", "total", "LOO cos", "LOO fill", "LOO total"],
                [
                    [
                        roman(n) + (" (stored)" if n == r.book else ""),
                        f4(own[n].cos),
                        f4(own[n].tag),
                        f4(own[n].fill),
                        f4(own[n].total),
                        f4(alone[n].cos),
                        f4(alone[n].fill),
                        f4(alone[n].total),
                    ]
                    for n in sorted(own)
                ],
            )
        )
        d("")
        d(
            f"With self: chooses {roman(p_self.book)}, runner-up {bk(p_self.runner_up)},"
            f" margin {f4(p_self.margin)}. LOO: chooses {roman(p_loo.book)}, runner-up"
            f" {bk(p_loo.runner_up)}, margin {f4(p_loo.margin)}."
        )
        if r.book_pinned:
            d("Book-pinned by the owner; the filer does not choose its book.")
        else:
            same = "equals" if p_loo.book == r.book else "differs from"
            stay = where[r.image_id] == r.book
            d(
                f"The LOO choice {same} the stored book. A re-sort would"
                f" {'keep it in' if stay else 'move it to'} {roman(where[r.image_id])}."
            )
        d("")
        for mode, terms, p in (("with_self", own, p_self), ("loo", alone, p_loo)):
            for n in sorted(terms):
                t = terms[n]
                rows.append(
                    [
                        r.image_id,
                        r.slug,
                        mode,
                        roman(n),
                        t.cos,
                        t.tag,
                        t.fill,
                        t.total,
                        roman(p.book),
                        bk(p.runner_up),
                        p.margin,
                        roman(r.book),
                        r.book_pinned,
                        r.pinned,
                    ]
                )
        summary.append(
            [
                r.slug,
                roman(r.book),
                "yes" if r.book_pinned else "",
                f"{roman(p_self.book)} / {f4(p_self.margin)}",
                f"{roman(p_loo.book)} / {f4(p_loo.margin)}",
                "" if r.book_pinned else ("yes" if p_loo.book == r.book else "no"),
                roman(where[r.image_id]),
            ]
        )
    write_csv(
        run.out / "c1_placements.csv",
        [
            "image_id",
            "slug",
            "mode",
            "book",
            "cos",
            "tag",
            "fill",
            "total",
            "chosen",
            "runner_up",
            "margin",
            "stored_book",
            "book_pinned",
            "pinned",
        ],
        rows,
    )
    h = run.h
    h("## C1. Placements")
    h("")
    h(
        "Source (a), old anchors, centered, database tuning. `with self`: centroids and fill"
        " as they stand, the leaf included in its own book. `LOO`: the leaf left out."
        " A re-sort (`assign_books`) moves an unpinned leaf only when its LOO best beats its"
        f" current book by book_margin {shape.tuning.book_margin:g}. The tag term is the same"
        " with and without the leaf, so it is shown once."
    )
    h("")
    moves = sum(1 for x in leaves if where[x.image_id] != x.book)
    h(f"A full re-sort today (`assign_books`, old anchors) moves {moves} leaves between books.")
    h("")
    for which in ("old", "new"):
        hints = run.anchors(which)
        hit = {
            r.slug: [
                roman(n)
                for n, (_, hs) in sorted(hints.items())
                if tag_overlap(db.pics[r.image_id].tags, hs) > 0
            ]
            for r in db.leaves
        }
        with_hit = {s_: b for s_, b in hit.items() if b}
        h(
            f"Tag hints ({which}): {len(with_hit)} of {len(db.leaves)} leaves match a hint of any"
            " book"
            + (
                ": " + "; ".join(f"{s_} ({', '.join(b)})" for s_, b in with_hit.items())
                if with_hit
                else ""
            )
            + "."
        )
    all_tags = [t for p in db.pics.values() for t in p.tags]
    h(
        f"Of the {len(all_tags)} tags on live pictures, {sum('-' in t for t in all_tags)} contain a"
        f" hyphen and {sum(' ' in t for t in all_tags)} a space."
    )
    h("")
    h("Summary (book / margin, margin being chosen minus runner-up):")
    h("")
    h(
        table(
            ["leaf", "stored", "book_pinned", "with self", "LOO", "LOO = stored", "re-sort"],
            summary,
        )
    )
    h("")
    run.md.extend(detail)


def _choices(sp: Space, slugs: Mapping[int, str]) -> tuple[dict[str, int], dict[str, int]]:
    loo_c = {
        slugs[x.image_id]: loo(sp.shape, sp.leaves, x).book for x in sp.leaves if x.vec is not None
    }
    where = assign_books(sp.shape, sp.leaves)
    return loo_c, {slugs[i]: b for i, b in where.items()}


def section_c2(run: Run) -> None:
    db = run.db
    h = run.h
    gold = run.gold_file.books
    slugs = {r.image_id: r.slug for r in db.leaves}
    all_s = [r.slug for r in db.leaves]
    unp = [r.slug for r in run.unpinned()]
    missing = [s for s in all_s if s not in gold]
    extra = [s for s in gold if s not in all_s]
    stored = {r.slug: r.book for r in db.leaves}
    rows: dict[str, dict[str, int]] = {"stored": stored}
    for which in ("old", "new"):
        for k in (run.db.tuning.anchor_k, 10.0):
            sp = run.space("a", which, tuning=replace(run.db.tuning, anchor_k=k))
            lc, rc = _choices(sp, slugs)
            rows[f"LOO, {which} anchors, k {k:g}"] = lc
            rows[f"re-sort, {which} anchors, k {k:g}"] = rc
    h("## C2. Gold accuracy")
    h("")
    if not run.gold_file.confirmed:
        h("Gold labels are **unconfirmed**.")
        h("")
    if missing or extra:
        h(f"Leaves with no gold label: {', '.join(missing) or 'none'}.")
        h(f"Gold labels for pictures that are not leaves: {', '.join(extra) or 'none'}.")
        h("")
    h(
        "Source (a), centered. `stored` is the book in the database. `LOO` is what the filer"
        " chooses for each leaf against everyone else where they stand. `re-sort` is"
        " `assign_books` (book-pinned leaves stay). A leaf with two gold books is right in"
        " either."
    )
    h("")
    h(
        f"Chance (one uniform random book per leaf): {chance(gold, unp):.2f} of {len(unp)}"
        f" unpinned, {chance(gold, all_s):.2f} of {len(all_s)} in all"
        " (1/7 per leaf, 2/7 for the leaf with two gold books)."
    )
    h("")
    acc_rows = []
    for name, ch in rows.items():
        a_u = accuracy(ch, gold, unp)
        a_a = accuracy(ch, gold, all_s)
        acc_rows.append([name, f"{a_u.right} of {a_u.n}", f"{a_a.right} of {a_a.n}"])
    h(table(["choice", "unpinned right", "all right"], acc_rows))
    h("")
    st = accuracy(stored, gold, unp)
    h(f"Stored books, unpinned leaves: {st.right} of {st.n} right.")
    h("")
    h(
        table(
            ["leaf", "gold", "stored", "result"],
            [
                [s, books_str(gold[s]), roman(stored[s]), "right" if s in st.rights else "wrong"]
                for s in unp
                if s in gold
            ],
        )
    )
    h("")
    st_all = accuracy(stored, gold, all_s)
    pinned_wrong = [s for s, _ in st_all.wrongs if s not in unp]
    h(
        f"Stored books, all leaves: {st_all.right} of {st_all.n} right; the book-pinned"
        f" leaves that are wrong: {', '.join(pinned_wrong) or 'none'}."
    )
    h("")
    h("Per leaf, every choice:")
    h("")
    names = list(rows)
    h(
        table(
            ["leaf", "gold", *names],
            [
                [s, books_str(gold.get(s, frozenset()))]
                + [
                    roman(rows[n][s]) + ("" if rows[n][s] in gold.get(s, frozenset()) else " x")
                    for n in names
                ]
                for s in all_s
            ],
        )
    )
    h("")
    h("`x` marks a book outside the leaf's gold.")
    h("")
    write_csv(
        run.out / "c2_gold.csv",
        ["slug", "book_pinned", "gold", *names],
        [
            [s, s not in unp, books_str(gold.get(s, frozenset()))]
            + [roman(rows[n][s]) for n in names]
            for s in all_s
        ],
    )

    # The brief's claims about where the old anchors put four leaves.
    old = {b.number: b.anchor_text for b in db.books}
    claims = [
        ("listening-station-with-recording-attendants", 4, "the listening chair is in iv"),
        ("towel-and-slipper-racks-in-a-bathhouse-changing-room", 5, "the bathhouse is in v"),
        ("monks-harvesting-the-shell-tree-by-the-shore", 6, "the barnacle tree is in vi"),
        ("bread-being-baked-over-an-open-fire", 4, "the bonnacon is in iv"),
    ]
    h("The brief's claims:")
    h("")
    crow = []
    for s, b, text in claims:
        got = stored.get(s)
        crow.append([text, s, bk(got), "confirmed" if got == b else "refuted"])
    says = "bathhouses" in old.get(3, "")
    crow.append(
        [
            "book iii's old anchor says bathhouses",
            "-",
            "-",
            "confirmed" if says else "refuted",
        ]
    )
    right_map = [s for s in st.rights]
    crow.append(
        [
            "about 1 of the 10 unpinned leaves is right (the map)",
            ", ".join(right_map) or "none",
            f"{st.right} of {st.n}",
            "confirmed"
            if st.right == 1 and right_map == ["legibility-of-label-banners-on-a-framed-map"]
            else "refuted",
        ]
    )
    h(table(["claim", "leaf", "stored", "result"], crow))
    h("")


def section_c3(run: Run) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    unp = {r.image_id for r in run.unpinned()}
    h("## C3. The medium hypothesis")
    h("")
    h(
        "Gold accuracy of each vector source on the leaves, four ways: the filer's LOO"
        " choice (database tuning) under old and new anchors; the nearest anchor by cosine"
        " alone (no leaves, tags or fill); and the nearest gold centroid built from the other"
        " leaves' gold books (no anchors; the two-book leaf builds no centroid; a book with no"
        " other gold leaf cannot be chosen, which rules out the leaves whose gold book has no"
        f" other member). Each cell: right of {len(gold)} / right of the {len(unp)} unpinned."
    )
    h("")
    acc_rows = []
    csv_rows = []
    for s in SOURCES:
        cells: list[str] = []
        for which in ("old", "new"):
            sp = run.space(s.key, which)
            ch = {
                x.image_id: loo(sp.shape, sp.leaves, x).book for x in sp.leaves if x.vec is not None
            }
            an = {x.image_id: anchor_pick(sp, x) for x in sp.leaves if x.vec is not None}
            for name, got in ((f"LOO {which}", ch), (f"anchor only {which}", an)):
                ra = sum(got[i] in gold[i] for i in got if i in gold)
                ru = sum(got[i] in gold[i] for i in got if i in gold and i in unp)
                cells.append(f"{ra} / {ru}")
                csv_rows.append([s.key, s.label, name, ra, len(gold), ru, len(unp)])
        sp = run.space(s.key, "old")
        gc = {x.image_id: gold_centroid_pick(sp, gold, x) for x in sp.leaves}
        ra = sum(1 for i, b in gc.items() if b is not None and i in gold and b in gold[i])
        ru = sum(
            1 for i, b in gc.items() if b is not None and i in gold and i in unp and b in gold[i]
        )
        cells.append(f"{ra} / {ru}")
        csv_rows.append([s.key, s.label, "gold centroid", ra, len(gold), ru, len(unp)])
        acc_rows.append([f"({s.key}) {s.label}", *cells])
    h(
        table(
            [
                "source",
                "LOO old",
                "anchor only old",
                "LOO new",
                "anchor only new",
                "gold centroid",
            ],
            acc_rows,
        )
    )
    h("")
    write_csv(
        run.out / "c3_sources.csv",
        ["source", "label", "method", "right_all", "n_all", "right_unpinned", "n_unpinned"],
        csv_rows,
    )

    h(
        f"Share of all {len(db.pics)} live pictures each book takes (leaves by LOO, every other"
        " picture against the leaves where they stand), database tuning:"
    )
    h("")
    share_rows = []
    csv_rows = []
    for s in SOURCES:
        for which in ("old", "new"):
            sp = run.space(s.key, which)
            c: Counter[int] = Counter()
            for i in db.pics:
                p = score_picture(sp, i)
                if p is not None:
                    c[p.book] += 1
            n = sum(c.values())
            share_rows.append(
                [f"({s.key}) {s.label}", which, n]
                + [f"{c[b]} ({pct(c[b] / n)})" for b in range(1, 8)]
            )
            csv_rows += [[s.key, which, roman(b), c[b], c[b] / n] for b in range(1, 8)]
    h(table(["source", "anchors", "n", *[roman(b) for b in range(1, 8)]], share_rows))
    h("")
    write_csv(run.out / "c3_shares.csv", ["source", "anchors", "book", "count", "share"], csv_rows)

    # Medium of each leaf from its Style paragraph.
    med: dict[int, str] = {}
    mrows = []
    for r in db.leaves:
        sp_ = facets.split(db.pics[r.image_id].analysis)
        style = sp_["style"] if sp_ else ""
        m, word = medium(style)
        med[r.image_id] = m
        first = style.split(". ")[0].strip().rstrip(".")
        mrows.append([r.slug, m, word, first])
    h("Medium of each leaf, from its Style paragraph: the first rule that matches wins.")
    h(
        "Rules in order: "
        + "; ".join(f"{name}: {', '.join(words)}" for name, words in MEDIUM_RULES)
        + ". Check these by eye."
    )
    h("")
    h(table(["leaf", "medium", "matched", "Style paragraph, first sentence"], mrows))
    h("")
    write_csv(
        run.out / "c3_medium.csv", ["slug", "medium", "matched", "style_first_sentence"], mrows
    )

    ids = [r.image_id for r in db.leaves]
    stored = {r.image_id: r.book for r in db.leaves}
    indicators = {
        "same gold book": [
            [bool(gold.get(a, frozenset()) & gold.get(b, frozenset())) for b in ids] for a in ids
        ],
        "same medium": [[med[a] == med[b] for b in ids] for a in ids],
        "same stored book": [[stored[a] == stored[b] for b in ids] for a in ids],
    }
    h(
        "Spearman rank correlation between the pairwise cosine of two leaves (each source"
        " centered on its own mean) and an indicator that the pair shares a gold book, a"
        " medium, or a stored book. Pairs share leaves, so they are not independent;"
        " no p-values are given. `same` and `diff` are the mean cosine of pairs with and"
        " without the shared property."
    )
    h("")
    crow = []
    csv_rows = []
    for s in SOURCES:
        sp = run.space(s.key, "old")
        vecs = [sp.vecs[i] for i in ids]
        if any(v is None for v in vecs):
            continue
        for name, same in indicators.items():
            ps = pair_stat([v for v in vecs if v is not None], same)
            crow.append(
                [
                    f"({s.key}) {s.label}",
                    name,
                    ps.n_pairs,
                    ps.n_same,
                    f"{ps.rho:.3f}",
                    f4(ps.mean_same),
                    f4(ps.mean_diff),
                ]
            )
            csv_rows.append(
                [s.key, name, ps.n_pairs, ps.n_same, ps.rho, ps.mean_same, ps.mean_diff]
            )
    h(table(["source", "indicator", "pairs", "pairs sharing", "rho", "same", "diff"], crow))
    h("")
    write_csv(
        run.out / "c3_correlation.csv",
        ["source", "indicator", "n_pairs", "n_same", "spearman", "mean_cos_same", "mean_cos_diff"],
        csv_rows,
    )


def section_c4(run: Run, orders: Sequence[Sequence[int]]) -> None:
    h = run.h
    gold = run.gold
    t0 = time.perf_counter()
    cells: list[tuple[dict[str, Any], Cell]] = []
    for src in GRID_SOURCES:
        for which in ("old", "new"):
            for centered in (True, False):
                base = run.space(src, which, centered)
                for k in GRID_K:
                    for alpha in GRID_ALPHA:
                        sp = base.tuned(anchor_k=k, tag_alpha=alpha)
                        key = {
                            "source": src,
                            "anchors": which,
                            "centering": "on" if centered else "off",
                            "anchor_k": k,
                            "tag_alpha": alpha,
                        }
                        cells.append((key, evaluate(sp, gold, orders)))
    run.timings["grid"] = time.perf_counter() - t0
    metrics = list(Cell.__dataclass_fields__)
    write_csv(
        run.out / "c4_grid.csv",
        ["source", "anchors", "centering", "anchor_k", "tag_alpha", *metrics],
        [[*k.values(), *[getattr(c, m) for m in metrics]] for k, c in cells],
    )
    n_unp = len([r for r in run.unpinned() if r.image_id in gold])
    h("## C4. Ablation grid")
    h("")
    h(
        f"{len(cells)} cells: source (a to d) x anchor_k {', '.join(f'{k:g}' for k in GRID_K)}"
        f" x tag_alpha {', '.join(f'{a:g}' for a in GRID_ALPHA)} x centering on/off x old/new"
        f" anchors; fill_beta {run.db.tuning.fill_beta:g} and book_margin"
        f" {run.db.tuning.book_margin:g} from the database. Every cell in `c4_grid.csv`."
    )
    h("")
    h(
        f"- `loo all`: LOO accuracy on the {len(gold)} gold leaves. `loo unp`: the same on the"
        f" {n_unp} unpinned. `margin`: mean LOO margin, chosen minus runner-up, over the"
        f" {len(gold)}. `flip`: share of the {len(gold)} whose LOO book differs from the stored"
        f" book. `resort unp`: accuracy of `assign_books` on the unpinned (pins stay)."
        f" `resort flip`: share of the unpinned it moves. `seq unp`: mean accuracy on the"
        f" unpinned over the {len(orders)} sequential refiling orders, pins honoured."
    )
    h("")
    cols = ["loo all", "loo unp", "margin", "flip", "resort unp", "resort flip", "seq unp"]

    def fmt(c: Cell) -> list[str]:
        return [
            pct(c.acc_loo_all),
            pct(c.acc_loo_unpinned),
            f4(c.mean_margin),
            pct(c.flip_loo),
            pct(c.acc_resort_unpinned),
            pct(c.flip_resort),
            pct(c.acc_seq_unpinned),
        ]

    top = sorted(
        cells, key=lambda kc: (-kc[1].acc_loo_all, -kc[1].acc_seq_unpinned, -kc[1].mean_margin)
    )[:15]
    h("Top 15 cells by `loo all`, then `seq unp`, then `margin`:")
    h("")
    h(
        table(
            ["source", "anchors", "centering", "k", "alpha", *cols],
            [
                [
                    k["source"],
                    k["anchors"],
                    k["centering"],
                    f"{k['anchor_k']:g}",
                    f"{k['tag_alpha']:g}",
                    *fmt(c),
                ]
                for k, c in top
            ],
        )
    )
    h("")
    cur = [
        (k, c)
        for k, c in cells
        if k["source"] == "a"
        and k["centering"] == "on"
        and k["anchor_k"] == run.db.tuning.anchor_k
        and k["tag_alpha"] == run.db.tuning.tag_alpha
    ]
    h("The cells at today's settings (source a, centered, database k and alpha):")
    h("")
    h(
        table(
            ["anchors", *cols],
            [[k["anchors"], *fmt(c)] for k, c in cur],
        )
    )
    h("")
    h(
        "Marginal effect of each factor: the mean of each metric over every cell at that"
        " level, so every combination of the other factors is averaged in."
    )
    h("")
    mrows = []
    for factor in ("source", "anchors", "centering", "anchor_k", "tag_alpha"):
        levels = sorted({k[factor] for k, _ in cells}, key=lambda v: (isinstance(v, str), v))
        for lv in levels:
            sel = [c for k, c in cells if k[factor] == lv]
            mean = {m: float(np.mean([getattr(c, m) for c in sel])) for m in metrics}
            mrows.append(
                [
                    factor,
                    f"{lv:g}" if isinstance(lv, float) else lv,
                    len(sel),
                    pct(mean["acc_loo_all"]),
                    pct(mean["acc_loo_unpinned"]),
                    f4(mean["mean_margin"]),
                    pct(mean["flip_loo"]),
                    pct(mean["acc_resort_unpinned"]),
                    pct(mean["flip_resort"]),
                    pct(mean["acc_seq_unpinned"]),
                ]
            )
    h(table(["factor", "level", "cells", *cols], mrows))
    h("")


def section_c5(run: Run, orders: Sequence[Sequence[int]]) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    h("## C5. Order sensitivity and attractors")
    h("")
    h(
        f"Each of the {len(orders)} orders refiles the {len(db.leaves)} leaves from empty books,"
        " one at a time, each through `choose_book` against those filed before it (the filing"
        " job's path). Source (a), centered, database tuning except anchor_k. With pins"
        " honoured a book-pinned leaf takes its owner's book when its turn comes, as when the"
        " owner placed it; with pins ignored the filer files it too."
    )
    h("")
    csv_rows = []
    summary = []
    per_leaf: dict[str, dict[int, Counter[int]]] = {}
    for which in ("old", "new"):
        for k in ORDER_K:
            sp = run.space("a", which, tuning=replace(db.tuning, anchor_k=k))
            for honour in (True, False):
                counts = order_counts(sp.shape, sp.leaves, orders, honour)
                label = f"{which} anchors, k {k:g}, pins {'honoured' if honour else 'ignored'}"
                if honour:
                    per_leaf[f"{which}, k {k:g}"] = counts
                movers = [i for i, c in counts.items() if len(c) > 1]
                filed = [
                    r.image_id
                    for r in db.leaves
                    if r.image_id in gold and not (honour and r.book_pinned)
                ]
                right = [
                    sum(n for b, n in counts[i].items() if b in gold[i]) / len(orders)
                    for i in filed
                ]
                summary.append(
                    [
                        label,
                        len(movers),
                        max((len(c) for c in counts.values()), default=0),
                        f"{sum(right):.2f} of {len(right)}",
                    ]
                )
                for i, per in counts.items():
                    for b, n in sorted(per.items()):
                        csv_rows.append([which, k, honour, i, run.slug(i), roman(b), n])
    write_csv(
        run.out / "c5_orders.csv",
        ["anchors", "anchor_k", "pins_honoured", "image_id", "slug", "book", "count"],
        csv_rows,
    )
    h(
        table(
            [
                "setting",
                "leaves landing in more than one book",
                "most books for one leaf",
                "mean right per order (leaves the filer files)",
            ],
            summary,
        )
    )
    h("")
    h("Per leaf, pins honoured: how often it lands in each book over the orders.")
    h("")
    names = list(per_leaf)
    h(
        table(
            ["leaf", "gold", "stored", *names],
            [
                [
                    r.slug + (" (pinned)" if r.book_pinned else ""),
                    books_str(gold.get(r.image_id, frozenset())),
                    roman(r.book),
                ]
                + [
                    ", ".join(
                        f"{roman(b)} {n}"
                        for b, n in sorted(
                            per_leaf[nm][r.image_id].items(), key=lambda bn: (-bn[1], bn[0])
                        )
                    )
                    for nm in names
                ]
                for r in db.leaves
            ],
        )
    )
    h("")

    # The order the leaves actually arrived in, replayed today. The corpus mean then was
    # taken over fewer pictures, so a mismatch here is not by itself a fault.
    hist = [i for i, _, _ in db.first_filed if i in {r.image_id for r in db.leaves}]
    if len(hist) == len(db.leaves):
        sp = run.space("a", "old")
        owner = {i for i, _, reason in db.first_filed if reason == "owner"}
        by_id = {x.image_id: x for x in sp.leaves}
        shaped = [replace(by_id[i], book_pinned=i in owner) for i in hist]
        replay = sequential(sp.shape, shaped, hist, honour_pins=True)
        first = {i: b for i, b, _ in db.first_filed}
        agree = sum(1 for i in hist if replay[i] == first[i])
        h(
            f"Historical order replayed (old anchors, database tuning, today's corpus mean;"
            f" owner placements kept): {agree} of {len(hist)} leaves land where they were first"
            " filed."
        )
        h("")
        h(
            table(
                ["#", "leaf", "first filed", "by", "replay"],
                [
                    [n + 1, run.slug(i), roman(first[i]), reason, roman(replay[i])]
                    for n, (i, _, reason) in enumerate(db.first_filed)
                    if i in replay
                ],
            )
        )
        h("")

    leaf_ids = {r.image_id for r in db.leaves}
    rest = [i for i in db.pics if i not in leaf_ids]
    h(
        f"Attractors: each of the {len(rest)} live pictures that is not a leaf scored by"
        " `choose_book` against the leaves where they stand (nothing filed). A book is flagged"
        f" when its share is at least {ATTRACTOR:.3f} (twice 1/7)."
    )
    h("")
    arows = []
    csv_rows = []
    for which in ("old", "new"):
        for k in ORDER_K:
            sp = run.space("a", which, tuning=replace(db.tuning, anchor_k=k))
            c: Counter[int] = Counter()
            for i in rest:
                p = score_picture(sp, i)
                if p is not None:
                    c[p.book] += 1
            n = sum(c.values())
            flagged = [roman(b) for b in range(1, 8) if n and c[b] / n >= ATTRACTOR]
            arows.append(
                [f"{which}, k {k:g}", n]
                + [f"{c[b]} ({pct(c[b] / n)})" for b in range(1, 8)]
                + [", ".join(flagged) or "none"]
            )
            csv_rows += [
                [which, k, roman(b), c[b], c[b] / n if n else float("nan")] for b in range(1, 8)
            ]
    h(table(["setting", "n", *[roman(b) for b in range(1, 8)], "flagged"], arows))
    h("")
    write_csv(
        run.out / "c5_attractors.csv", ["anchors", "anchor_k", "book", "count", "share"], csv_rows
    )


def section_c6(run: Run) -> None:
    db = run.db
    h = run.h
    src = Counter(r.note_source or "none" for r in db.leaves)
    h("## C6. Conservator notes")
    h("")
    h(
        f"Leaves with a note: {sum(v for k, v in src.items() if k != 'none')} of {len(db.leaves)}"
        f" ({', '.join(f'{k} {v}' for k, v in sorted(src.items()))})."
    )
    jobs = ", ".join(f"{k} {v}" for k, v in sorted(db.note_jobs.items())) or "none"
    h(f"`collection_note` rows in the jobs table, by status: {jobs}.")
    h("")


def section_before_after(run: Run) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    h("## Before and after: a full re-sort under the new anchors (never applied)")
    h("")
    h(
        "`plan_resort` on source (a), centered, new anchors and tag hints, database tuning,"
        " then the same with anchor_k 10. Book-pinned leaves keep their book and pinned leaves"
        " their slot, as in production. `margin`: the proposed book's score minus the best"
        " other book, against the plan's final state with the leaf left out. `gain`: the"
        " proposed book's score minus the current book's, same state."
    )
    h("")
    csv_rows = []
    for k in (db.tuning.anchor_k, 10.0):
        sp = run.space("a", "new", tuning=replace(db.tuning, anchor_k=k))
        props = before_after(sp.shape, sp.leaves)
        moved = sum(1 for p in props if p.before[0] != p.after[0])
        g = [p for p in props if p.image_id in gold]
        right_before = sum(p.before[0] in gold[p.image_id] for p in g)
        right_after = sum(p.after[0] in gold[p.image_id] for p in g)
        h(f"### anchor_k {k:g}")
        h("")
        h(
            f"{moved} of {len(props)} leaves change book. Gold right: {right_before} of {len(g)}"
            f" now, {right_after} of {len(g)} after."
        )
        h("")
        rows = []
        for p in props:
            gb = gold.get(p.image_id, frozenset())
            rows.append(
                [
                    run.slug(p.image_id),
                    f"{roman(p.before[0])} {p.before[1]}",
                    f"{roman(p.after[0])} {p.after[1]}"
                    + (" *" if p.before[0] != p.after[0] else ""),
                    f4(p.margin),
                    f4(p.gain),
                    "yes" if p.book_pinned else "",
                    "yes" if p.pinned else "",
                    books_str(gb),
                    "right" if p.after[0] in gb else "wrong",
                ]
            )
            csv_rows.append(
                [
                    k,
                    p.image_id,
                    run.slug(p.image_id),
                    roman(p.before[0]),
                    p.before[1],
                    roman(p.after[0]),
                    p.after[1],
                    p.margin,
                    p.gain,
                    p.book_pinned,
                    p.pinned,
                    books_str(gb),
                ]
            )
        h(
            table(
                [
                    "leaf",
                    "now",
                    "proposed",
                    "margin",
                    "gain",
                    "book_pinned",
                    "pinned",
                    "gold",
                    "proposed vs gold",
                ],
                rows,
            )
        )
        h("")
        h("`*` marks a change of book.")
        h("")
    write_csv(
        run.out / "before_after.csv",
        [
            "anchor_k",
            "image_id",
            "slug",
            "book_now",
            "leaf_now",
            "book_proposed",
            "leaf_proposed",
            "margin",
            "gain",
            "book_pinned",
            "pinned",
            "gold",
        ],
        csv_rows,
    )


PIN_K = (3.0, 6.0, 10.0, 20.0, 40.0)
TAG_K = (3.0, 10.0)
TAG_ALPHA = (0.0, 0.05, 0.15, LIMITS["tag_alpha"][1])


def pins_at_gold(run: Run, sp: Space) -> Space:
    """The book-pinned leaves moved to their gold books, still book-pinned there."""
    pinned = {r.image_id for r in run.db.leaves if r.book_pinned}
    where = gold_books(sp.shape, sp.leaves, run.gold)
    return replace(sp, leaves=rebook(sp.leaves, {i: where[i] for i in pinned if i in where}))


def retag(sp: Space) -> Space:
    return replace(
        sp,
        leaves=[replace(x, tags=dehyphen(x.tags)) for x in sp.leaves],
        tags={i: dehyphen(t) for i, t in sp.tags.items()},
    )


def section_c3b(run: Run) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    h("## C3b. Medium words in Subject and Setting")
    h("")
    h(
        "Words looked for, as whole words with plurals, case ignored: "
        + ", ".join(MEDIUM_WORDS)
        + ". Some of them also name things a picture can show (a dinner plate, a sheet of"
        " cloth, a chart on a wall); the list counts the word, not the sense."
    )
    h("")
    rows = []
    med: dict[int, str] = {}
    for r in db.leaves:
        parts: dict[str, str] = facets.split(db.pics[r.image_id].analysis) or {}
        med[r.image_id] = medium(parts.get("style"))[0]
        rows.append(
            [
                r.slug,
                med[r.image_id],
                ", ".join(medium_words(parts.get("subject"))) or "-",
                ", ".join(medium_words(parts.get("setting"))) or "-",
            ]
        )
    h(table(["leaf", "medium", "in Subject", "in Setting"], rows))
    h("")
    write_csv(run.out / "c3b_medium_words.csv", ["slug", "medium", "subject", "setting"], rows)
    counts: dict[str, Counter[str]] = {}
    totals: dict[str, int] = {}
    n_split = 0
    for pic in db.pics.values():
        split = facets.split(pic.analysis)
        if split is None:
            continue
        n_split += 1
        for f in ("subject", "setting", "style"):
            found = medium_words(split[f])
            counts.setdefault(f, Counter()).update(found)
            totals[f] = totals.get(f, 0) + (1 if found else 0)
    h(
        f"Across the {n_split} live pictures whose analysis splits: Subject paragraphs with at"
        f" least one of the words {totals.get('subject', 0)}; Setting {totals.get('setting', 0)};"
        f" Style {totals.get('style', 0)}."
    )
    h("")
    h(
        table(
            ["word", "Subject", "Setting", "Style"],
            [
                [w, counts["subject"][w], counts["setting"][w], counts["style"][w]]
                for w in MEDIUM_WORDS
                if any(counts[f][w] for f in ("subject", "setting", "style"))
            ],
        )
    )
    h("")
    ids = [r.image_id for r in db.leaves]
    same_gold = [
        [bool(gold.get(a, frozenset()) & gold.get(b, frozenset())) for b in ids] for a in ids
    ]
    same_med = [[med[a] == med[b] for b in ids] for a in ids]
    diff_med = [[not x for x in row] for row in same_med]
    h(
        "Spearman of pairwise cosine against `same gold book`, within pairs that share a"
        " medium and within pairs that do not. Very small samples; no p-values."
    )
    h("")
    crow = []
    for key in ("a", "b"):
        sp = run.space(key, "old")
        vecs = [sp.vecs[i] for i in ids]
        if any(v is None for v in vecs):
            continue
        vv = [v for v in vecs if v is not None]
        for name, keep in (("pairs sharing a medium", same_med), ("pairs not sharing", diff_med)):
            ps = pair_stat(vv, same_gold, keep)
            crow.append(
                [
                    f"({key}) {SOURCE[key].label}",
                    name,
                    ps.n_pairs,
                    ps.n_same,
                    f"{ps.rho:.3f}",
                    f4(ps.mean_same),
                    f4(ps.mean_diff),
                ]
            )
    h(table(["source", "pairs", "n", "same gold", "rho", "same", "diff"], crow))
    h("")


def section_pins(run: Run, orders: Sequence[Sequence[int]]) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    t0 = time.perf_counter()
    hi = LIMITS["anchor_k"][1]
    ks = [k for k in PIN_K if k <= hi]
    sp0 = run.space("a", "old")
    gb = gold_books(sp0.shape, sp0.leaves, gold)
    moved = {r.slug: roman(gb[r.image_id]) for r in db.leaves if r.book_pinned and r.image_id in gb}
    h("## C5b. Pins at gold (counterfactual)")
    h("")
    h(
        "The book-pinned leaves moved to their gold books and still book-pinned there ("
        + ", ".join(f"{s_} to {b}" for s_, b in moved.items())
        + "); every other leaf as stored to start with. Centering on, database tag_alpha,"
        f" fill_beta and book_margin. anchor_k {', '.join(f'{k:g}' for k in ks)}"
        f" (LIMITS allows up to {hi:g}). Accuracy is on the {len(run.unpinned())} unpinned"
        f" leaves: `re-sort` by `assign_books`, `LOO` against the counterfactual state, `seq`"
        f" the mean over the same {len(orders)} orders, pinned leaves taking their gold book."
    )
    h("")
    cells: list[tuple[str, str, float, Cell]] = []
    for key in GRID_SOURCES:
        for which in ("old", "new"):
            base = pins_at_gold(run, run.space(key, which))
            for k in ks:
                sp = base.tuned(anchor_k=k, tag_alpha=db.tuning.tag_alpha)
                cells.append((key, which, k, evaluate(sp, gold, orders)))
    rows = [
        [
            f"({key}) {SOURCE[key].label}",
            which,
            f"{k:g}",
            pct(c.acc_resort_unpinned),
            pct(c.acc_loo_unpinned),
            pct(c.acc_seq_unpinned),
            f4(c.mean_margin),
        ]
        for key, which, k, c in cells
    ]
    h(table(["source", "anchors", "k", "re-sort", "LOO", "seq", "margin"], rows))
    h("")
    write_csv(
        run.out / "c5b_pins_at_gold.csv",
        [
            "source",
            "anchors",
            "anchor_k",
            "acc_resort_unpinned",
            "acc_loo_unpinned",
            "acc_seq_unpinned",
            "mean_margin",
        ],
        [
            [
                key,
                which,
                k,
                c.acc_resort_unpinned,
                c.acc_loo_unpinned,
                c.acc_seq_unpinned,
                c.mean_margin,
            ]
            for key, which, k, c in cells
        ],
    )

    def mean_new(key: str) -> float:
        sel = [c for k_, w, _, c in cells if k_ == key and w == "new"]
        return float(
            np.mean(
                [(c.acc_resort_unpinned + c.acc_loo_unpinned + c.acc_seq_unpinned) / 3 for c in sel]
            )
        )

    best = max(("b", "c"), key=lambda k_: (mean_new(k_), k_ == "b"))
    h(
        f"Per leaf, new anchors, sources (a) and ({best}); ({best}) is the one of (b) and (c)"
        " with the higher mean of the three accuracies over its new-anchor cells"
        f" (b {pct(mean_new('b'))}, c {pct(mean_new('c'))}). Each cell: re-sort book /"
        " LOO book / the book it lands in most often over the orders (how often)."
    )
    h("")
    unp = run.unpinned()
    cols = []
    per: dict[str, dict[int, str]] = {}
    for key in ("a", best):
        base = pins_at_gold(run, run.space(key, "new"))
        for k in (3.0, 10.0, 20.0):
            sp = base.tuned(anchor_k=k, tag_alpha=db.tuning.tag_alpha)
            where = assign_books(sp.shape, sp.leaves)
            counts = order_counts(sp.shape, sp.leaves, orders, honour_pins=True)
            name = f"({key}) k {k:g}"
            cols.append(name)
            per[name] = {}
            for r in unp:
                x = sp.leaf(r.image_id)
                lb = loo(sp.shape, sp.leaves, x).book if x.vec is not None else r.book
                top_b, top_n = sorted(counts[r.image_id].items(), key=lambda bn: (-bn[1], bn[0]))[0]
                g = gold.get(r.image_id, frozenset())

                def mark(b: int, g: frozenset[int] = g) -> str:
                    return roman(b) + ("" if b in g else " x")

                per[name][r.image_id] = (
                    f"{mark(where[r.image_id])} / {mark(lb)} / {mark(top_b)} ({top_n})"
                )
    h(
        table(
            ["leaf", "gold", "stored", *cols],
            [
                [r.slug, books_str(gold.get(r.image_id, frozenset())), roman(r.book)]
                + [per[c][r.image_id] for c in cols]
                for r in unp
            ],
        )
    )
    h("")
    h("`x` marks a book outside the leaf's gold.")
    h("")

    h(
        "Upper bound: every leaf placed in its gold book, then each leaf's LOO choice against"
        " the rest (pinned leaves included in the count). The two-book leaf is placed in the"
        " book of its two it scores higher in, against the single-label leaves in their gold"
        " books; that book is named per row."
    )
    h("")
    meal = [r.image_id for r in db.leaves if len(gold.get(r.image_id, ())) > 1]
    urows = []
    ucsv = []
    for key in GRID_SOURCES:
        for which in ("old", "new"):
            cells_ = []
            for k in (db.tuning.anchor_k, 10.0):
                sp = run.space(key, which, tuning=replace(db.tuning, anchor_k=k))
                where, picks = upper_bound(sp.shape, sp.leaves, gold)
                ra = sum(1 for i, p_ in picks.items() if i in gold and p_.book in gold[i])
                ru = sum(
                    1
                    for r in unp
                    if r.image_id in picks and picks[r.image_id].book in gold[r.image_id]
                )
                two = ", ".join(f"{run.slug(i)} in {roman(where[i])}" for i in meal if i in where)
                cells_ += [f"{ra} of {len(picks)}", f"{ru} of {len(unp)}", two]
                ucsv.append([key, which, k, ra, len(picks), ru, len(unp), two])
            urows.append([f"({key}) {SOURCE[key].label}", which, *cells_])
    kd = f"{db.tuning.anchor_k:g}"
    h(
        table(
            [
                "source",
                "anchors",
                f"all, k {kd}",
                f"unpinned, k {kd}",
                f"two-book leaf, k {kd}",
                "all, k 10",
                "unpinned, k 10",
                "two-book leaf, k 10",
            ],
            urows,
        )
    )
    h("")
    write_csv(
        run.out / "c5b_upper_bound.csv",
        [
            "source",
            "anchors",
            "anchor_k",
            "right_all",
            "n_all",
            "right_unpinned",
            "n_unpinned",
            "two_book_leaf",
        ],
        ucsv,
    )
    run.timings["pins grid"] = time.perf_counter() - t0


def section_tags(run: Run) -> None:
    db = run.db
    h = run.h
    gold = run.gold
    unp = {r.image_id for r in run.unpinned()}
    h("## C5c. Tag hints with hyphens read as spaces (simulated)")
    h("")
    h(
        "A simulated filer change: the plan functions are handed each picture's tags with"
        " hyphens turned into spaces, so `tag_overlap` can match `zodiac` inside `zodiac-man`."
        " `plan.py` does not do this today."
    )
    h("")
    mrows = []
    for which in ("old", "new"):
        hints = run.anchors(which)
        for name, fn in (("as stored", frozenset), ("hyphens as spaces", dehyphen)):
            anyb = goldb = 0
            for r in db.leaves:
                tags = fn(db.pics[r.image_id].tags)
                hit = {n for n, (_, hs) in hints.items() if tag_overlap(tags, hs) > 0}
                anyb += bool(hit)
                goldb += bool(hit & gold.get(r.image_id, frozenset()))
            mrows.append(
                [which, name, f"{anyb} of {len(db.leaves)}", f"{goldb} of {len(db.leaves)}"]
            )
    h(table(["hints", "tags", "leaves matching any book", "leaves matching a gold book"], mrows))
    h("")
    h(
        "Gold accuracy, new anchors, centered. `LOO`: of the 15 and of the unpinned."
        " `re-sort`: unpinned, pins as stored. `pins at gold`: unpinned, re-sort with the"
        f" book-pinned leaves in their gold books (C5b). tag_alpha up to {TAG_ALPHA[-1]:g}, the"
        " LIMITS maximum."
    )
    h("")
    rows = []
    csv_rows = []
    for key in ("a", "b"):
        for k in TAG_K:
            for alpha in TAG_ALPHA:
                for name, tagged in (("as stored", False), ("hyphens as spaces", True)):
                    sp = run.space(key, "new").tuned(anchor_k=k, tag_alpha=alpha)
                    if tagged:
                        sp = retag(sp)
                    lb = {
                        x.image_id: loo(sp.shape, sp.leaves, x).book
                        for x in sp.leaves
                        if x.vec is not None
                    }
                    la = sum(1 for i, b in lb.items() if i in gold and b in gold[i])
                    lu = sum(1 for i, b in lb.items() if i in gold and i in unp and b in gold[i])
                    where = assign_books(sp.shape, sp.leaves)
                    ru = sum(1 for i in unp if where[i] in gold.get(i, frozenset()))
                    pg = pins_at_gold(run, sp)
                    where2 = assign_books(pg.shape, pg.leaves)
                    pu = sum(1 for i in unp if where2[i] in gold.get(i, frozenset()))
                    rows.append(
                        [
                            f"({key})",
                            f"{k:g}",
                            f"{alpha:g}",
                            name,
                            f"{la} / {lu}",
                            f"{ru} of {len(unp)}",
                            f"{pu} of {len(unp)}",
                        ]
                    )
                    csv_rows.append([key, k, alpha, name, la, lu, ru, pu])
    h(table(["source", "k", "alpha", "tags", "LOO all / unp", "re-sort", "pins at gold"], rows))
    h("")
    write_csv(
        run.out / "c5c_tags.csv",
        [
            "source",
            "anchor_k",
            "tag_alpha",
            "tags",
            "loo_right_all",
            "loo_right_unpinned",
            "resort_right_unpinned",
            "pins_at_gold_resort_right_unpinned",
        ],
        csv_rows,
    )


# -- main --------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--anchors", type=Path, default=DEFAULT_ANCHORS)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--orders", type=int, default=ORDERS)
    ap.add_argument("--embed-cache", type=Path, default=None)
    args = ap.parse_args(argv)
    if not args.dsn:
        sys.exit("no database: pass --dsn or set DATABASE_URL")

    started = time.perf_counter()
    settings = get_settings()
    gold = load_gold(json.loads(args.gold.read_text()))
    if not gold.confirmed:
        print("UNCONFIRMED GOLD LABELS: every accuracy counts against unconfirmed labels.")
    new_anchors = load_anchors(json.loads(args.anchors.read_text()))
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    db = read_db(args.dsn, settings)
    run = Run(db, settings, gold, new_anchors, args.seed, args.orders, args.out)
    run.timings["read"] = time.perf_counter() - t0
    print(f"read {len(db.pics)} live pictures and {len(db.leaves)} leaves from {db.where}")

    embed_all(run, Embeds(settings, args.embed_cache))
    set_means(run)
    cross_check(run, args.dsn)
    print("embedder and read checks passed")

    orders = random_orders([r.image_id for r in db.leaves], args.orders, args.seed)
    sections: list[tuple[str, Any]] = [
        ("c1", lambda: section_c1(run)),
        ("c2", lambda: section_c2(run)),
        ("c3", lambda: section_c3(run)),
        ("c3b", lambda: section_c3b(run)),
        ("c4", lambda: section_c4(run, orders)),
        ("c5", lambda: section_c5(run, orders)),
        ("c5b", lambda: section_pins(run, orders)),
        ("c5c", lambda: section_tags(run)),
        ("c6", lambda: section_c6(run)),
        ("before_after", lambda: section_before_after(run)),
    ]
    body: dict[str, list[str]] = {}
    for name, fn in sections:
        t1 = time.perf_counter()
        run.md = []
        fn()
        body[name] = run.md
        run.timings[name] = time.perf_counter() - t1
        print(f"{name}: {run.timings[name]:.1f}s")

    run.md = []
    run.h("# How well does the filer do?")
    run.h("")
    section_method(run)
    total = time.perf_counter() - started
    run.h(
        "Run time: "
        + ", ".join(f"{k} {v:.1f}s" for k, v in run.timings.items())
        + f"; total {total:.1f}s."
    )
    run.h("")
    order = ["c1", "c2", "c3", "c3b", "c4", "c5", "c5b", "c5c", "c6", "before_after"]
    head = run.md
    text = "\n".join(head + [line for name in order for line in body[name]]).rstrip() + "\n"
    (args.out / "report.md").write_text(text)
    print(f"wrote {args.out / 'report.md'} in {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
