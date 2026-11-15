"""Offline harness: run the organism over a corpus and print what it grew.

    python -m penish.mycelium.sim --synthetic 168 --ticks 1000 --seed 1
    python -m penish.mycelium.sim --dsn postgresql://... --from-publish-dates

Writes nothing. `--dsn` opens one read-only connection and reads live pictures,
their description vectors and the corpus mean; psycopg is imported only there,
so a synthetic run needs no database. Everything after loading is the same pure
code the live tick runs: graph, then engine, then colonies, once per tick.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from penish.mycelium import colonies, engine
from penish.mycelium.graph import Edge, candidate_edges
from penish.mycelium.tunables import (
    DEFAULTS,
    LAYERS,
    TICK_SECONDS,
    TICKS_PER_DAY,
    TunableError,
    Tunables,
    with_overrides,
)

FloatArray = NDArray[np.float64]

KIND = "description"
SYNTHETIC_DIM = 32


@dataclass(frozen=True, slots=True)
class Corpus:
    label: str
    ids: tuple[int, ...]
    vectors: Mapping[str, Mapping[int, FloatArray]]
    # None means the mean is recomputed from whoever is live, as corpus_stats would be.
    means: Mapping[str, FloatArray | None]
    published: Mapping[int, datetime] = field(default_factory=dict)


# -- synthetic corpus --------------------------------------------------------


def _normals(rng: np.random.Generator, shape: tuple[int, ...]) -> FloatArray:
    # Box-Muller from .random() alone, so a numpy upgrade cannot move the corpus.
    size = int(np.prod(shape))
    u1 = 1.0 - rng.random(size)
    u2 = rng.random(size)
    z: FloatArray = np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * math.pi * u2)
    return z.reshape(shape)


def synthetic_corpus(n: int, seed: int, layers: Sequence[str]) -> Corpus:
    """Uneven clusters plus some loners, on top of a shared offset.

    The shared offset is what every real embedding has in common; it makes raw
    cosine uniformly high, so centering has the same work to do as in production.
    Each layer gets its own stream keyed by name, like the engine.
    """
    ids = tuple(range(1, n + 1))
    vectors: dict[str, dict[int, FloatArray]] = {}
    for layer in layers:
        rng = np.random.Generator(
            np.random.PCG64(np.random.SeedSequence([seed, engine.layer_key("synthetic:" + layer)]))
        )
        k = max(1, round(n / 10))
        centers = _normals(rng, (k, SYNTHETIC_DIM))
        offset = 3.0 * _normals(rng, (SYNTHETIC_DIM,))
        weights = rng.random(k) + 0.25
        cum = np.cumsum(weights)
        out: dict[int, FloatArray] = {}
        for pid in ids:
            noise = _normals(rng, (SYNTHETIC_DIM,))
            if rng.random() < 0.15:
                out[pid] = offset + 1.2 * noise
                continue
            c = int(np.searchsorted(cum, rng.random() * cum[-1], side="right"))
            c = min(c, k - 1)
            out[pid] = offset + centers[c] + 0.45 * noise
        vectors[layer] = out
    return Corpus(f"synthetic {n} pictures", ids, vectors, dict.fromkeys(layers))


# -- database corpus ---------------------------------------------------------


def load_corpus(dsn: str, model: str | None) -> Corpus:
    """Live pictures with a description vector in one (kind, model) space. Read-only."""
    import psycopg

    if model is None:
        from penish.config import get_settings

        model = get_settings().embed_model

    with psycopg.connect(dsn) as conn:
        # Set before the first statement, so the whole session refuses writes.
        conn.read_only = True
        rows = conn.execute(
            """
            select i.id, i.published_at, e.dim, e.vec::real[]
            from images i
            join embeddings e on e.image_id = i.id
            where i.status = 'live' and e.kind = %(kind)s and e.model = %(model)s
            order by i.id
            """,
            {"kind": KIND, "model": model},
        ).fetchall()
        mean_row = conn.execute(
            "select mean::real[] from corpus_stats where kind = %(kind)s and model = %(model)s",
            {"kind": KIND, "model": model},
        ).fetchone()
        conn.rollback()

    mean = None
    if mean_row is not None and mean_row[0] is not None:
        mean = np.asarray(mean_row[0], dtype=np.float64)
    # One width only, as similarity.py filters by dim: the mean's if it has one.
    dims = [int(r[2]) for r in rows]
    width = mean.shape[0] if mean is not None else (statistics.mode(dims) if dims else 0)
    vecs: dict[int, FloatArray] = {}
    published: dict[int, datetime] = {}
    for pid, pub, dim, vec in rows:
        if int(dim) != width:
            continue
        vecs[int(pid)] = np.asarray(vec, dtype=np.float64)
        if pub is not None:
            published[int(pid)] = pub
    return Corpus(
        f"{len(vecs)} pictures, {model}",
        tuple(sorted(vecs)),
        {"whole": vecs},
        {"whole": mean},
        published,
    )


def entry_ticks(
    published: Mapping[int, datetime], min_size: int
) -> tuple[datetime | None, dict[int, int]]:
    """Tick 0 is the publish time of the picture that brings the corpus to `min_size`.

    Below that a layer has no edges, so earlier ticks would be empty. Everyone
    published before it enters at tick 0.
    """
    order = sorted(published, key=lambda pid: (published[pid], pid))
    if not order:
        return None, {}
    t0 = published[order[min(min_size, len(order)) - 1]]
    out: dict[int, int] = {}
    for pid in order:
        secs = (published[pid] - t0).total_seconds()
        out[pid] = max(0, math.floor(secs / TICK_SECONDS))
    return t0, out


# -- the run -----------------------------------------------------------------


@dataclass(slots=True)
class Summary:
    ticks: int
    layers: tuple[str, ...]
    pictures: int
    entered: int
    share_end: float
    share_mean: float
    share_by_day: list[float]
    died: int
    alive: int
    dead_lifespans: list[int]
    oldest_alive: int | None
    largest: int
    deaths: dict[str, int]
    membership_changes: int
    edge_counts: dict[str, dict[str, int]]
    mean_strength: dict[str, float]
    engine_hash: str
    colonies_hash: str


def _mean_of(vectors: Mapping[int, FloatArray], live: Sequence[int]) -> FloatArray | None:
    if not live:
        return None
    m: FloatArray = np.mean(np.stack([vectors[i] for i in live]), axis=0)
    return m


def run(
    corpus: Corpus,
    ticks: int,
    seed: int,
    tunables: Tunables = DEFAULTS,
    layers: Sequence[str] = ("whole",),
    entry: Mapping[int, int] | None = None,
) -> Summary:
    """Pure. `entry` maps a picture to the tick it goes live; None means all at tick 0."""
    t = tunables
    names = tuple(engine.layer_order(layers))
    enter = {pid: 0 for pid in corpus.ids} if entry is None else dict(entry)

    states: dict[str, engine.LayerState] = {n: engine.LayerState.empty(n) for n in names}
    col = colonies.ColoniesState()
    graphs: dict[str, list[Edge]] = {}
    graph_for: tuple[int, ...] = ()

    shares: list[float] = []
    by_day: list[float] = []
    deaths = dict.fromkeys(colonies.CAUSES, 0)
    changes = 0
    live: list[int] = []
    tick = -1

    for tick in range(ticks):
        live = sorted(pid for pid, at in enter.items() if at <= tick)
        key = tuple(live)
        if key != graph_for:
            # The graph only changes when someone enters, so it is rebuilt then.
            for n in names:
                vecs = {pid: corpus.vectors[n][pid] for pid in live}
                mean = corpus.means.get(n)
                if mean is None:
                    mean = _mean_of(corpus.vectors[n], live)
                graphs[n] = candidate_edges(n, vecs, mean, t)
            graph_for = key

        result = engine.run_tick(states, graphs, {}, live, {}, t, seed, tick)
        states = dict(result.states)
        latched = {n: engine.latched_edges(states[n]) for n in names}
        col, events = colonies.step(col, tick, latched, set(live), t)

        for ev in events:
            if ev.kind in deaths:
                deaths[ev.kind] += 1
            elif ev.kind in ("joined", "left"):
                changes += 1

        share = _share(col, live)
        shares.append(share)
        if (tick + 1) % TICKS_PER_DAY == 0:
            by_day.append(share)

    end = tick
    galleries = [c for c in col.colonies if c.fruited is not None]
    dead = [c for c in galleries if c.state == colonies.DEAD]
    living = [c for c in galleries if c.state in colonies.LIVING]
    lifespans = sorted(
        c.died - c.fruited for c in dead if c.died is not None and c.fruited is not None
    )
    ages = [end - c.fruited for c in living if c.fruited is not None]

    thresholds = {
        "wither_min": t.wither_min,
        "hypha_visible": t.hypha_visible,
        "colony_leave": t.colony_leave,
        "colony_enter": t.colony_enter,
    }
    counts: dict[str, dict[str, int]] = {}
    strength: dict[str, float] = {}
    for n in names:
        values = [es.strength for es in states[n].edges.values()]
        counts[n] = {"state": len(values)}
        for label, cut in thresholds.items():
            counts[n][label] = sum(1 for v in values if v >= cut)
        counts[n]["latched"] = sum(1 for es in states[n].edges.values() if es.latched)
        strength[n] = math.fsum(values) / len(values) if values else 0.0

    return Summary(
        ticks=ticks,
        layers=names,
        pictures=len(corpus.ids),
        entered=len(live),
        share_end=shares[-1] if shares else 0.0,
        share_mean=math.fsum(shares) / len(shares) if shares else 0.0,
        share_by_day=by_day,
        died=len(dead),
        alive=len(living),
        dead_lifespans=lifespans,
        oldest_alive=max(ages) if ages else None,
        largest=max((c.peak for c in galleries), default=0),
        deaths=deaths,
        membership_changes=changes,
        edge_counts=counts,
        mean_strength=strength,
        engine_hash=engine.state_hash(states),
        colonies_hash=colonies.state_hash(col),
    )


def _share(col: colonies.ColoniesState, live: Sequence[int]) -> float:
    if not live:
        return 0.0
    held: set[int] = set()
    for c in colonies.living(col):
        held |= c.images()
    return len(held & set(live)) / len(live)


# -- output ------------------------------------------------------------------


def _days(ticks: float) -> str:
    return f"{ticks / TICKS_PER_DAY:.1f} days"


def publish_spread(published: Mapping[int, datetime]) -> str:
    if not published:
        return "published  none"
    dates = sorted(published.values())
    first, last = dates[0], dates[-1]
    median = dates[(len(dates) - 1) // 2]
    days = len({d.date() for d in dates})
    span = (last - first).total_seconds() / 86_400
    return (
        f"published  {first:%Y-%m-%d} .. {last:%Y-%m-%d}  {span:.0f} days"
        f"  median {median:%Y-%m-%d}  on {days} distinct days"
    )


def render(
    s: Summary,
    *,
    label: str,
    seed: int,
    published: Mapping[int, datetime] | None = None,
    start: datetime | None = None,
) -> list[str]:
    lines = [
        f"{label}  {s.ticks} ticks ({_days(s.ticks)})  seed {seed}  layers {' '.join(s.layers)}"
    ]
    if published is not None:
        lines.append(publish_spread(published))
    if start is not None:
        lines.append(f"tick 0  {start:%Y-%m-%d %H:%M}  entered {s.entered} of {s.pictures}")
    lines.append(f"in a gallery  end {s.share_end:.2f}  mean {s.share_mean:.2f}")
    if s.share_by_day:
        lines.append("  by day  " + " ".join(f"{x:.2f}" for x in s.share_by_day))
    median = _days(statistics.median(s.dead_lifespans)) if s.dead_lifespans else "-"
    oldest = _days(s.oldest_alive) if s.oldest_alive is not None else "-"
    lines.append(
        f"galleries  died {s.died}  alive {s.alive}"
        f"  median life of the dead {median}  oldest alive {oldest}"
    )
    lines.append(f"largest gallery  {s.largest}")
    lines.append("deaths  " + "  ".join(f"{k} {v}" for k, v in s.deaths.items()))
    per_day = s.membership_changes / (s.ticks / TICKS_PER_DAY) if s.ticks else 0.0
    lines.append(f"membership changes per day  {per_day:.1f}")
    for n in s.layers:
        c = s.edge_counts[n]
        lines.append(
            f"edges {n}  {c['state']} held  {c['wither_min']} >= wither_min"
            f"  {c['hypha_visible']} >= visible  {c['colony_leave']} >= leave"
            f"  {c['colony_enter']} >= enter  {c['latched']} latched"
            f"  mean strength {s.mean_strength[n]:.3f}"
        )
    return lines


# -- cli ---------------------------------------------------------------------


def _parse_set(items: Sequence[str]) -> dict[str, float | int]:
    out: dict[str, float | int] = {}
    for item in items:
        name, sep, raw = item.partition("=")
        if not sep:
            raise TunableError(f"--set wants name=value, not {item}")
        try:
            out[name.strip()] = int(raw)
        except ValueError:
            try:
                out[name.strip()] = float(raw)
            except ValueError:
                raise TunableError(f"{name.strip()} must be a number") from None
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m penish.mycelium.sim", description="writes nothing")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dsn", help="read live pictures from this database")
    src.add_argument("--synthetic", type=int, metavar="N", help="a seeded clustered corpus")
    p.add_argument("--ticks", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", dest="overrides")
    p.add_argument("--from-publish-dates", action="store_true")
    p.add_argument("--layers", default="whole", help="comma separated")
    p.add_argument("--model", help="embedding model, default the configured one")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)
    if args.ticks < 1:
        p.error("--ticks must be at least 1")
    if args.seed < 0:
        p.error("--seed must not be negative")
    try:
        tunables = with_overrides(DEFAULTS, _parse_set(args.overrides))
    except TunableError as e:
        p.error(str(e))
    layers = [x.strip() for x in args.layers.split(",") if x.strip()]
    unknown = [x for x in layers if x not in LAYERS]
    if unknown or not layers:
        p.error(f"unknown layer {unknown[0] if unknown else 'none'}")

    published: Mapping[int, datetime] | None = None
    start: datetime | None = None
    entry: dict[int, int] | None = None
    if args.synthetic is not None:
        if args.synthetic < 1:
            p.error("--synthetic must be at least 1")
        if args.from_publish_dates:
            p.error("--from-publish-dates needs --dsn")
        corpus = synthetic_corpus(args.synthetic, args.seed, layers)
    else:
        if layers != ["whole"]:
            p.error("only whole has vectors in the database")
        corpus = load_corpus(args.dsn, args.model)
        published = corpus.published
        if args.from_publish_dates:
            start, entry = entry_ticks(corpus.published, tunables.min_size)
            if start is None:
                p.error("no publish dates")
            # The stored mean already knows pictures that have not arrived yet.
            corpus = dataclasses.replace(corpus, means={"whole": None})

    summary = run(corpus, args.ticks, args.seed, tunables, layers, entry)
    for line in render(
        summary, label=corpus.label, seed=args.seed, published=published, start=start
    ):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
