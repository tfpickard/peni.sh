"""Colonies: groups of latched edges that keep an identity, fruit, fade and die.

Pure. One call is one tick over every layer, because fruiting is capped across
layers and a candidate is checked against galleries in the other layers. The
rules are the "Colonies" section of docs/mycelium.md; step numbers below refer
to it.

Nothing here draws random numbers: every choice is broken by a fixed order, so
the same inputs give the same colonies, ids and events.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from typing import Any

from penish.mycelium.tunables import DEFAULTS, LAYERS, TICKS_PER_DAY, Tunables

FORMING = "forming"
ALIVE = "alive"
FADING = "fading"
DEAD = "dead"
FAILED = "failed"

STATES: tuple[str, ...] = (FORMING, ALIVE, FADING, DEAD, FAILED)
LIVING: frozenset[str] = frozenset({ALIVE, FADING})

EVENT_KINDS: tuple[str, ...] = (
    "formed",
    "fruited",
    "joined",
    "left",
    "faded",
    "revived",
    "failed",
    "absorbed",
    "schism",
    "starved",
    "drifted",
    "depopulated",
)

# Step 8. The steps run in an order that makes a second cause impossible; the
# tuple is kept so the precedence is written down where the store can read it.
CAUSES: tuple[str, ...] = ("depopulated", "absorbed", "schism", "drifted", "starved")

# The fruit cap is per rolling day, not per fruit_ticks: the two only agree at
# the defaults.
FRUIT_WINDOW = TICKS_PER_DAY

JOIN = "join"
LEAVE = "leave"

Edge = tuple[int, int, float]


@dataclass(slots=True)
class Member:
    image: int
    founder: bool
    joined: int


@dataclass(slots=True)
class Pending:
    """A join or leave that has held for `ticks` consecutive ticks."""

    image: int
    kind: str
    ticks: int


@dataclass(slots=True)
class Colony:
    id: int
    layer: str
    state: str
    born: int
    members: list[Member] = field(default_factory=list)
    pending: list[Pending] = field(default_factory=list)
    serial: int | None = None
    parent: int | None = None
    fruited: int | None = None
    faded: int | None = None
    died: int | None = None
    cause: str | None = None
    absorbed_by: int | None = None
    # Ticks matched since forming; a forming colony is matched every tick it survives.
    held: int = 0
    founders: int = 0
    # The pictures at fruiting. A founder that leaves and comes back is a founder
    # again, so this is kept apart from the members.
    founder_ids: list[int] = field(default_factory=list)
    # Set at max_size, cleared at or below remerge_size.
    full: bool = False
    peak: int = 0

    def images(self) -> set[int]:
        return {m.image for m in self.members}


@dataclass(slots=True)
class ColoniesState:
    colonies: list[Colony] = field(default_factory=list)
    next_id: int = 1
    next_serial: int = 1
    # Fruiting ticks still inside the rolling window, oldest first.
    recent_fruitings: list[int] = field(default_factory=list)
    tick: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonicalize(self) -> None:
        """Sorted in place, so the order a caller loaded rows in never reaches a result."""
        self.colonies.sort(key=lambda c: c.id)
        for c in self.colonies:
            c.members.sort(key=lambda m: m.image)
            c.pending.sort(key=lambda p: (p.image, p.kind))
            c.founder_ids.sort()
        self.recent_fruitings.sort()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ColoniesState:
        cols: list[Colony] = []
        for c in d["colonies"]:
            raw = dict(c)
            raw["members"] = [Member(**m) for m in raw.get("members", [])]
            raw["pending"] = [Pending(**p) for p in raw.get("pending", [])]
            cols.append(Colony(**raw))
        out = cls(
            colonies=cols,
            next_id=int(d["next_id"]),
            next_serial=int(d["next_serial"]),
            recent_fruitings=[int(t) for t in d.get("recent_fruitings", [])],
            tick=None if d.get("tick") is None else int(d["tick"]),
        )
        out.canonicalize()
        return out


@dataclass(frozen=True, slots=True)
class Event:
    tick: int
    kind: str
    layer: str
    colony: int
    image: int | None = None
    other: int | None = None


def state_hash(state: ColoniesState) -> str:
    """sha256 over a canonical rendering. The state holds no floats."""
    text = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def jaccard(a: AbstractSet[int], b: AbstractSet[int]) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 0.0


class _UnionFind:
    def __init__(self, nodes: Iterable[int]) -> None:
        self.parent = {n: n for n in nodes}
        self.size = dict.fromkeys(self.parent, 1)

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        self.parent[hi] = lo
        self.size[lo] += self.size.pop(hi)

    def sets(self) -> list[set[int]]:
        out: dict[int, set[int]] = {}
        for n in sorted(self.parent):
            out.setdefault(self.find(n), set()).add(n)
        return list(out.values())


def groups(
    edges: Iterable[Edge], live: AbstractSet[int], tunables: Tunables = DEFAULTS
) -> list[frozenset[int]]:
    """Groups of one layer, ordered by lowest member id.

    Edges are undirected latched edges. A pair given twice keeps its stronger
    strength, so the caller may pass both directions.
    """
    best: dict[tuple[int, int], float] = {}
    for a, b, s in edges:
        a, b = int(a), int(b)
        if a == b or a not in live or b not in live:
            continue
        key = (a, b) if a < b else (b, a)
        strength = float(s)
        if key not in best or strength > best[key]:
            best[key] = strength
    uf = _UnionFind(sorted({x for key in best for x in key}))
    for a, b in sorted(best):
        uf.union(a, b)
    out: list[frozenset[int]] = []
    for comp in uf.sets():
        if len(comp) < tunables.min_size:
            continue
        if len(comp) <= tunables.max_size:
            out.append(frozenset(comp))
            continue
        inside = sorted(
            ((s, a, b) for (a, b), s in best.items() if a in comp),
            key=lambda e: (-e[0], e[1], e[2]),
        )
        capped = _UnionFind(sorted(comp))
        for _, a, b in inside:
            ra, rb = capped.find(a), capped.find(b)
            if ra != rb and capped.size[ra] + capped.size[rb] <= tunables.max_size:
                capped.union(a, b)
        out.extend(frozenset(p) for p in capped.sets() if len(p) >= tunables.min_size)
    return sorted(out, key=min)


@dataclass(slots=True)
class _Work:
    """One layer's groups and who holds them this tick."""

    layer: str
    groups: list[frozenset[int]]
    holder: dict[int, int] = field(default_factory=dict)
    group_of: dict[int, int] = field(default_factory=dict)
    revived: set[int] = field(default_factory=set)

    def hold(self, gi: int, colony_id: int) -> None:
        self.release(colony_id)
        self.holder[gi] = colony_id
        self.group_of[colony_id] = gi

    def release(self, colony_id: int) -> None:
        """A colony that dies or moves to another group frees the one it held."""
        gi = self.group_of.pop(colony_id, None)
        if gi is not None and self.holder.get(gi) == colony_id:
            del self.holder[gi]


class _Log:
    def __init__(self, tick: int) -> None:
        self.tick = tick
        self.items: list[Event] = []

    def add(
        self, kind: str, c: Colony, *, image: int | None = None, other: int | None = None
    ) -> None:
        self.items.append(Event(self.tick, kind, c.layer, c.id, image, other))


def _majority(c: Colony, g: AbstractSet[int]) -> bool:
    return 2 * len(c.images() & g) > len(c.members)


def _match(
    colonies: Sequence[Colony], gs: Sequence[frozenset[int]], free: Iterable[int], threshold: float
) -> list[tuple[Colony, int]]:
    """Step 1 rule: greedy over pairs by Jaccard, intersection, colony id, lowest group id."""
    pairs: list[tuple[float, int, int, int, Colony, int]] = []
    free_gs = sorted(free)
    for c in colonies:
        mine = c.images()
        for gi in free_gs:
            g = gs[gi]
            inter = len(mine & g)
            if inter == 0:
                continue
            j = jaccard(mine, g)
            if j >= threshold:
                pairs.append((-j, -inter, c.id, min(g), c, gi))
    pairs.sort(key=lambda p: p[:4])
    taken_c: set[int] = set()
    taken_g: set[int] = set()
    out: list[tuple[Colony, int]] = []
    for *_, c, gi in pairs:
        if c.id in taken_c or gi in taken_g:
            continue
        taken_c.add(c.id)
        taken_g.add(gi)
        out.append((c, gi))
    return out


def _remove(c: Colony, images: AbstractSet[int], log: _Log | None) -> None:
    if not images:
        return
    c.members = [m for m in c.members if m.image not in images]
    c.pending = [p for p in c.pending if p.image not in images]
    if log is not None:
        for image in sorted(images):
            log.add("left", c, image=image)


def _add(c: Colony, images: Iterable[int], tick: int, log: _Log | None) -> None:
    founders = set(c.founder_ids)
    for image in sorted(images):
        c.members.append(Member(image, image in founders, tick))
        if log is not None:
            log.add("joined", c, image=image)
    c.members.sort(key=lambda m: m.image)


def _take(c: Colony, g: AbstractSet[int], tick: int, log: _Log | None) -> None:
    """Membership becomes the group as is; members who stay keep founder flag and joined tick."""
    mine = c.images()
    _remove(c, mine - g, log)
    _add(c, g - mine, tick, log)
    c.pending = []


def _note_size(c: Colony, t: Tunables) -> None:
    n = len(c.members)
    if n >= t.max_size:
        c.full = True
    elif n <= t.remerge_size:
        c.full = False
    if c.state in LIVING:
        c.peak = max(c.peak, n)


def _claim(
    cols: Sequence[Colony],
    claimer: Colony,
    images: AbstractSet[int],
    states: AbstractSet[str],
    log: _Log,
) -> None:
    """One living gallery per layer per picture: a group taken as is wins its pictures."""
    for o in cols:
        if o.id != claimer.id and o.state in states:
            _remove(o, o.images() & images, log)


def _die(c: Colony, cause: str, tick: int, log: _Log, other: int | None = None) -> None:
    c.state = DEAD
    c.cause = cause
    c.died = tick
    c.pending = []
    if cause == "absorbed":
        c.absorbed_by = other
    log.add(cause, c, other=other)


def _new(
    state: ColoniesState, layer: str, g: AbstractSet[int], tick: int, parent: int | None
) -> Colony:
    c = Colony(
        id=state.next_id,
        layer=layer,
        state=FORMING,
        born=tick,
        members=[Member(i, False, tick) for i in sorted(g)],
        parent=parent,
    )
    state.next_id += 1
    state.colonies.append(c)
    return c


def _revive(c: Colony, log: _Log, w: _Work) -> None:
    c.state = ALIVE
    c.faded = None
    c.pending = []
    w.revived.add(c.id)
    log.add("revived", c)


def _schism(members: AbstractSet[int], gs: Sequence[frozenset[int]]) -> bool:
    counts = [len(members & g) for g in gs]
    n = len(members)
    return sum(1 for k in counts if k >= 2) >= 2 and all(2 * k < n for k in counts)


def _phase_a(
    state: ColoniesState,
    layer: str,
    gs: list[frozenset[int]],
    live: AbstractSet[int],
    tick: int,
    t: Tunables,
    log: _Log,
) -> _Work:
    """Steps 0 to 5 and membership for one layer."""
    cols = [c for c in state.colonies if c.layer == layer]
    living = [c for c in cols if c.state in LIVING]

    # 0
    for c in living:
        _remove(c, {i for i in c.images() if i not in live}, log)
        if len(c.members) < t.min_size:
            _die(c, "depopulated", tick, log)
    living = [c for c in living if c.state in LIVING]

    w = _Work(layer, gs)

    # 1
    for c, gi in _match(living, gs, range(len(gs)), t.match_jaccard):
        w.hold(gi, c.id)
        if c.state == FADING:
            _revive(c, log, w)

    # 2
    by_id = {c.id: c for c in living}
    for gi, g in enumerate(gs):
        k = [c for c in living if c.state in LIVING and _majority(c, g)]
        if len(k) < 2:
            continue
        sid = w.holder.get(gi)
        if sid is None:
            surv = max(k, key=lambda c: (jaccard(c.images(), g), len(c.images() & g), -c.id))
            sid = surv.id
            w.hold(gi, sid)
            if surv.state == FADING:
                _revive(surv, log, w)
        for c in k:
            if c.id != sid:
                # A colony matched to another group in step 1 can still be absorbed
                # here; its group goes back to steps 3 and 5.
                w.release(c.id)
                _die(c, "absorbed", tick, log, other=sid)

    # 3
    for c in living:
        if c.state not in LIVING or c.id in w.group_of:
            continue
        for gi, g in enumerate(gs):
            if gi not in w.holder and _majority(c, g):
                w.hold(gi, c.id)
                if c.state == FADING:
                    _revive(c, log, w)
                break

    # 4
    for c in living:
        if c.state == ALIVE and c.id not in w.group_of:
            c.state = FADING
            c.faded = tick
            c.pending = []
            log.add("faded", c)

    _membership(cols, w, by_id, tick, t, log)

    # 5
    forming = [c for c in cols if c.state == FORMING]
    leftover = [gi for gi in range(len(gs)) if gi not in w.holder]
    for c, gi in _match(forming, gs, leftover, t.match_jaccard):
        w.hold(gi, c.id)
        _take(c, gs[gi], tick, None)
        c.held += 1
    fading = [c for c in cols if c.state == FADING]
    for gi in leftover:
        if gi in w.holder:
            continue
        g = gs[gi]
        parent: Colony | None = None
        best = 0
        for f in fading:
            n = len(f.images() & g)
            if n > best:
                parent, best = f, n
        c = _new(state, layer, g, tick, None if parent is None else parent.id)
        w.hold(gi, c.id)
        log.add("formed", c, other=c.parent)
    for c in forming:
        if c.id not in w.group_of:
            c.state = FAILED
            c.died = tick
            log.add("failed", c)
    return w


def _membership(
    cols: Sequence[Colony],
    w: _Work,
    by_id: Mapping[int, Colony],
    tick: int,
    t: Tunables,
    log: _Log,
) -> None:
    held = [by_id[cid] for cid in sorted(w.group_of) if by_id[cid].state == ALIVE]

    for c in held:
        if c.id in w.revived:
            g = w.groups[w.group_of[c.id]]
            _claim(cols, c, g, LIVING, log)
            _take(c, g, tick, log)
            _note_size(c, t)

    gated = [c for c in held if c.id not in w.revived]
    old: dict[int, dict[tuple[int, str], int]] = {
        c.id: {(p.image, p.kind): p.ticks for p in c.pending} for c in gated
    }

    # Every leave in the layer goes before any join, so a picture moving between
    # two galleries is never in both.
    for c in gated:
        g = w.groups[w.group_of[c.id]]
        leaving = [
            Pending(i, LEAVE, old[c.id].get((i, LEAVE), 0) + 1) for i in sorted(c.images() - g)
        ]
        ripe = {p.image for p in leaving if p.ticks >= t.admit_ticks}
        c.pending = [p for p in leaving if p.image not in ripe]
        _remove(c, ripe, log)
        _note_size(c, t)

    owner = {m.image: c.id for c in cols if c.state in LIVING for m in c.members}
    for c in gated:
        if c.full:
            continue
        g = w.groups[w.group_of[c.id]]
        joining = [
            Pending(i, JOIN, old[c.id].get((i, JOIN), 0) + 1) for i in sorted(g - c.images())
        ]
        admitted: list[int] = []
        for p in joining:
            if p.ticks < t.admit_ticks or p.image in owner:
                continue
            if len(c.members) + len(admitted) >= t.max_size:
                break
            admitted.append(p.image)
            owner[p.image] = c.id
        _add(c, admitted, tick, log)
        c.pending = sorted(
            c.pending + [p for p in joining if p.image not in admitted],
            key=lambda p: (p.image, p.kind),
        )
        _note_size(c, t)


def _fruit(state: ColoniesState, tick: int, t: Tunables, log: _Log) -> dict[str, list[Colony]]:
    recent = [x for x in state.recent_fruitings if x > tick - FRUIT_WINDOW]
    order = {name: i for i, name in enumerate(LAYERS)}
    cands = sorted(
        (c for c in state.colonies if c.state == FORMING and c.held >= t.fruit_ticks),
        key=lambda c: (c.born, min(c.images()), order[c.layer], c.id),
    )
    out: dict[str, list[Colony]] = {}
    for c in cands:
        if len(recent) >= t.fruit_cap:
            break
        mine = c.images()
        if any(
            o.layer != c.layer
            and o.state in LIVING
            and jaccard(mine, o.images()) >= t.duplicate_jaccard
            for o in state.colonies
        ):
            continue
        cols = [o for o in state.colonies if o.layer == c.layer]
        # A fading gallery sharing a member dies in step 6 with its frozen members intact.
        _claim(cols, c, mine, {ALIVE}, log)
        c.state = ALIVE
        c.serial = state.next_serial
        state.next_serial += 1
        c.fruited = tick
        for m in c.members:
            m.founder = True
        c.founders = len(c.members)
        c.founder_ids = sorted(mine)
        _note_size(c, t)
        log.add("fruited", c)
        recent.append(tick)
        out.setdefault(c.layer, []).append(c)
    state.recent_fruitings = recent
    return out


def _phase_b(
    state: ColoniesState,
    w: _Work,
    fruited: Sequence[Colony],
    tick: int,
    t: Tunables,
    log: _Log,
) -> None:
    """Steps 6 and 7 for one layer."""
    cols = [c for c in state.colonies if c.layer == w.layer]
    sprouts = [c.images() for c in fruited]

    # 6
    for c in cols:
        if c.state != FADING or c.faded is None:
            continue
        mine = c.images()
        if tick - c.faded >= t.grace_ticks or any(mine & s for s in sprouts):
            _die(c, "schism" if _schism(mine, w.groups) else "starved", tick, log)

    # 7
    for c in cols:
        if c.state != ALIVE or c.founders == 0:
            continue
        remaining = len(c.images().intersection(c.founder_ids))
        if remaining >= t.drift_keep * c.founders:
            continue
        gi = w.group_of.get(c.id)
        if gi is None:
            _die(c, "drifted", tick, log)
            continue
        child = _new(state, w.layer, w.groups[gi], tick, c.id)
        w.hold(gi, child.id)
        _die(c, "drifted", tick, log, other=child.id)
        log.add("formed", child, other=c.id)


def step(
    prev: ColoniesState,
    tick: int,
    edges: Mapping[str, Sequence[Edge]],
    live: AbstractSet[int],
    tunables: Tunables = DEFAULTS,
) -> tuple[ColoniesState, list[Event]]:
    """One tick over every layer. `prev` is not modified.

    `edges` maps a layer to its latched edges `(a, b, strength)` with a < b; a
    missing layer has none. Events come out in the order the rules apply:
    steps 0 to 5 layer by layer, then fruiting, then steps 6 and 7 layer by
    layer.
    """
    if prev.tick is not None and tick <= prev.tick:
        raise ValueError(f"tick {tick} is not after {prev.tick}")
    unknown = sorted(set(edges) - set(LAYERS))
    if unknown:
        raise ValueError(f"unknown layer {unknown[0]}")
    state = copy.deepcopy(prev)
    state.canonicalize()
    state.tick = tick
    log = _Log(tick)
    works = {
        layer: _phase_a(
            state, layer, groups(edges.get(layer, ()), live, tunables), live, tick, tunables, log
        )
        for layer in LAYERS
    }
    fruited = _fruit(state, tick, tunables, log)
    for layer in LAYERS:
        _phase_b(state, works[layer], fruited.get(layer, []), tick, tunables, log)
    state.colonies.sort(key=lambda c: c.id)
    return state, log.items


def living(state: ColoniesState, layer: str | None = None) -> list[Colony]:
    return [c for c in state.colonies if c.state in LIVING and (layer is None or c.layer == layer)]
