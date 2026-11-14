"""One tick of the organism: walkers, traffic, strength, budget, latch, wither.

Pure: numpy and the standard library. The same inputs give the same state and
the same `state_hash` on every run, which is what lets genesis replay the live
tick and the harness stand in for both.

Edge state is keyed by directed `(src, dst)`. An edge with no state has strength
zero; state appears the first tick an edge carries flow and goes away when it
withers or loses a live endpoint.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from penish.mycelium.graph import Edge
from penish.mycelium.tunables import LAYERS, Tunables

Pair = tuple[int, int]


@dataclass(frozen=True, slots=True)
class EdgeState:
    strength: float = 0.0
    latched: bool = False
    fatigue: float = 0.0
    fused: bool = False
    # Consecutive ticks, up to and including the last one stepped, with no flow.
    idle: int = 0


@dataclass(frozen=True, slots=True)
class LayerState:
    layer: str
    edges: Mapping[Pair, EdgeState] = field(default_factory=dict)

    @classmethod
    def empty(cls, layer: str) -> LayerState:
        return cls(layer, {})


@dataclass(frozen=True, slots=True)
class LayerReport:
    layer: str
    tick: int
    crossings: int
    traffic: float
    traffic_scale: float
    walker_flow: Mapping[Pair, float]
    traffic_flow: Mapping[Pair, float]
    flow: Mapping[Pair, float]
    edges: int
    visible: int
    latched: int
    fused: int
    withered: int
    dropped: int


@dataclass(frozen=True, slots=True)
class TickResult:
    tick: int
    states: Mapping[str, LayerState]
    reports: Mapping[str, LayerReport]
    neighbor_level: Mapping[int, float]


def layer_key(layer: str) -> int:
    """A layer's number, from its name only, so adding a layer moves nobody else's stream."""
    return int.from_bytes(hashlib.sha256(layer.encode()).digest()[:8], "big")


def layer_rng(seed: int, tick: int, layer: str) -> np.random.Generator:
    return np.random.Generator(
        np.random.PCG64(np.random.SeedSequence([seed, tick, layer_key(layer)]))
    )


def _pick(cum: list[float], draw: float) -> int:
    # By hand so numpy's choice() internals can never change a result.
    target = draw * cum[-1]
    for i, c in enumerate(cum):
        if c > target:
            return i
    # draw * total can round up to total; the last positive option takes it.
    for i in range(len(cum) - 1, 0, -1):
        if cum[i] > cum[i - 1]:
            return i
    return 0


def _walk(
    rng: np.random.Generator,
    live: Sequence[int],
    out: Mapping[int, list[tuple[int, float]]],
    strength: Mapping[Pair, float],
    t: Tunables,
) -> dict[Pair, float]:
    flow: dict[Pair, float] = {}
    for start in live:
        for _ in range(t.walkers):
            node: int = start
            prev: int | None = None
            for _ in range(t.steps):
                opts: list[int] = []
                cum: list[float] = []
                total = 0.0
                for dst, a_beta in out.get(node, ()):
                    if dst == prev:
                        continue
                    w = (t.epsilon + strength.get((node, dst), 0.0)) ** t.alpha * a_beta
                    total += w
                    opts.append(dst)
                    cum.append(total)
                if not opts or total <= 0.0:
                    break
                nxt = opts[_pick(cum, float(rng.random()))]
                pair = (node, nxt)
                flow[pair] = flow.get(pair, 0.0) + 1.0
                prev, node = node, nxt
    return flow


def step(
    state: LayerState,
    graph: Sequence[Edge],
    traffic: Mapping[Pair, int],
    live_ids: Iterable[int],
    tunables: Tunables,
    seed: int,
    tick: int,
    layer: str,
) -> tuple[LayerState, LayerReport]:
    """One layer, one tick, in the order of docs/mycelium.md."""
    t = tunables
    if state.layer != layer:
        raise ValueError(f"state is for {state.layer}, not {layer}")

    # 1. Random stream.
    rng = layer_rng(seed, tick, layer)

    # 10. Endpoints that are not both live go first, from state and from the graph.
    live = sorted(set(live_ids))
    live_set = set(live)
    edges: dict[Pair, EdgeState] = {}
    dropped = 0
    for pair in sorted(state.edges):
        if pair[0] in live_set and pair[1] in live_set:
            edges[pair] = state.edges[pair]
        else:
            dropped += 1
    out: dict[int, list[tuple[int, float]]] = {}
    candidates: set[Pair] = set()
    for e in sorted(graph, key=lambda e: (e.src, e.dst)):
        if e.layer != layer:
            raise ValueError(f"edge for {e.layer} in {layer}")
        if e.src == e.dst or e.src not in live_set or e.dst not in live_set:
            continue
        if (e.src, e.dst) in candidates:
            continue
        candidates.add((e.src, e.dst))
        out.setdefault(e.src, []).append((e.dst, e.affinity**t.beta))

    # 2. Walkers.
    strength = {p: s.strength for p, s in edges.items()}
    walker_flow = _walk(rng, live, out, strength, t)
    crossings = int(sum(walker_flow.values()))

    # 3. Traffic.
    raw_traffic: dict[Pair, float] = {}
    for pair in sorted(traffic):
        clicks = traffic[pair]
        if pair in candidates and clicks > 0:
            raw_traffic[pair] = min(t.traffic_edge_cap, clicks * t.traffic_per_click)
    total = math.fsum(raw_traffic.values())
    allowed = t.traffic_share * crossings
    scale = 1.0
    if total > allowed:
        scale = allowed / total
    traffic_flow = {p: v * scale for p, v in raw_traffic.items()}

    flow: dict[Pair, float] = dict(walker_flow)
    for p, v in traffic_flow.items():
        flow[p] = flow.get(p, 0.0) + v

    # 4. Strength. State that does not exist yet is zero, so only flow creates it.
    for pair in sorted(flow):
        if pair not in edges and flow[pair] > 0.0:
            edges[pair] = EdgeState()
    step4: dict[Pair, EdgeState] = {}
    for pair in sorted(edges):
        es = edges[pair]
        f = flow.get(pair, 0.0)
        g = f / (1.0 + f) * (1.0 - t.fatigue_gamma * es.fatigue)
        r = t.rho / 2.0 if es.fused else t.rho
        s = min(1.0, max(0.0, (1.0 - r) * es.strength + r * g))
        idle = 0 if f > 0.0 else es.idle + 1
        step4[pair] = EdgeState(s, es.latched, es.fatigue, es.fused, idle)

    # 5. Budget.
    out_sum: dict[int, float] = {}
    for pair in sorted(step4):
        out_sum[pair[0]] = out_sum.get(pair[0], 0.0) + step4[pair].strength
    for pair in sorted(step4):
        tot = out_sum[pair[0]]
        if tot > t.budget:
            es = step4[pair]
            step4[pair] = EdgeState(
                es.strength * (t.budget / tot), es.latched, es.fatigue, es.fused, es.idle
            )

    # 6. Latch and 7. fatigue.
    step7: dict[Pair, EdgeState] = {}
    for pair in sorted(step4):
        es = step4[pair]
        latched = es.latched
        if es.strength >= t.colony_enter:
            latched = True
        elif es.strength < t.colony_leave:
            latched = False
        delta = 1.0 / t.fatigue_ticks
        fatigue = es.fatigue + delta if latched else es.fatigue - delta
        fatigue = min(1.0, max(0.0, fatigue))
        step7[pair] = EdgeState(es.strength, latched, fatigue, es.fused, es.idle)

    # 8. Fused.
    step8: dict[Pair, EdgeState] = {}
    for pair in sorted(step7):
        es = step7[pair]
        rev = step7.get((pair[1], pair[0]))
        fused = es.latched and rev is not None and rev.latched
        step8[pair] = EdgeState(es.strength, es.latched, es.fatigue, fused, es.idle)

    # 9. Wither: weak alone is not enough, nor is idle alone.
    final: dict[Pair, EdgeState] = {}
    withered = 0
    for pair in sorted(step8):
        es = step8[pair]
        if es.strength < t.wither_min and es.idle >= t.wither_idle:
            withered += 1
            continue
        final[pair] = es

    report = LayerReport(
        layer=layer,
        tick=tick,
        crossings=crossings,
        traffic=math.fsum(traffic_flow.values()),
        traffic_scale=scale,
        walker_flow={p: walker_flow[p] for p in sorted(walker_flow)},
        traffic_flow={p: traffic_flow[p] for p in sorted(traffic_flow)},
        flow={p: flow[p] for p in sorted(flow)},
        edges=len(final),
        visible=sum(1 for es in final.values() if es.strength >= t.hypha_visible),
        latched=sum(1 for es in final.values() if es.latched),
        fused=sum(1 for es in final.values() if es.fused),
        withered=withered,
        dropped=dropped,
    )
    return LayerState(layer, final), report


def layer_order(names: Iterable[str]) -> list[str]:
    """Known layers in their display order, anything else after them by name."""
    present = set(names)
    known = [name for name in LAYERS if name in present]
    return known + sorted(present - set(LAYERS))


def neighbor_levels(
    states: Iterable[LayerState],
    base: Mapping[int, float],
    live_ids: Iterable[int],
    tunables: Tunables,
) -> dict[int, float]:
    """Strength-weighted mean base overgrowth over each picture's visible hyphae.

    Every visible directed edge counts once for each endpoint, in every layer, so
    a pair visible both ways or in two layers weighs more. 0 with none.
    """
    by_name = {s.layer: s for s in states}
    num: dict[int, float] = {}
    den: dict[int, float] = {}
    for name in layer_order(by_name):
        edges = by_name[name].edges
        for a, b in sorted(edges):
            s = edges[(a, b)].strength
            if s < tunables.hypha_visible:
                continue
            for me, other in ((a, b), (b, a)):
                num[me] = num.get(me, 0.0) + s * base.get(other, 0.0)
                den[me] = den.get(me, 0.0) + s
    out: dict[int, float] = {}
    for pid in sorted(set(live_ids)):
        d = den.get(pid, 0.0)
        out[pid] = num[pid] / d if d > 0.0 else 0.0
    return out


def run_tick(
    states: Mapping[str, LayerState],
    graphs: Mapping[str, Sequence[Edge]],
    traffic: Mapping[Pair, int],
    live_ids: Iterable[int],
    base: Mapping[int, float],
    tunables: Tunables,
    seed: int,
    tick: int,
) -> TickResult:
    """Every layer that has state or a graph, then the neighbor level across all of them."""
    live = sorted(set(live_ids))
    new_states: dict[str, LayerState] = {}
    reports: dict[str, LayerReport] = {}
    for name in layer_order(set(states) | set(graphs)):
        prev = states.get(name) or LayerState.empty(name)
        new_states[name], reports[name] = step(
            prev, graphs.get(name, ()), traffic, live, tunables, seed, tick, name
        )
    levels = neighbor_levels(new_states.values(), base, live, tunables)
    return TickResult(tick, new_states, reports, levels)


def latched_edges(state: LayerState) -> list[tuple[int, int, float]]:
    """Undirected latched edges `(a, b, strength)` with a < b, sorted by (a, b).

    Latched in either direction counts; strength is the larger of the two
    directions. This is what colonies groups pictures by.
    """
    best: dict[Pair, float] = {}
    for (src, dst), es in state.edges.items():
        if not es.latched:
            continue
        key = (src, dst) if src < dst else (dst, src)
        best[key] = max(best.get(key, 0.0), es.strength)
    return [(a, b, best[(a, b)]) for a, b in sorted(best)]


def _encode(state: LayerState) -> bytes:
    lines = [f"layer {state.layer}"]
    for src, dst in sorted(state.edges):
        es = state.edges[(src, dst)]
        lines.append(
            f"{src} {dst} {float(es.strength).hex()} {int(es.latched)} "
            f"{float(es.fatigue).hex()} {int(es.fused)} {es.idle}"
        )
    return ("\n".join(lines) + "\n").encode()


def state_hash(state: LayerState | Mapping[str, LayerState]) -> str:
    """sha256 over the sorted edge state, floats written exactly with float.hex.

    In a mapping a layer with no edges is skipped: an empty layer and an absent one
    step the same, and a reload from edge_state, which has no rows to name an empty
    layer by, must hash as the state that was saved.
    """
    h = hashlib.sha256()
    if isinstance(state, LayerState):
        h.update(_encode(state))
    else:
        for name in layer_order(state):
            if state[name].edges:
                h.update(_encode(state[name]))
    return h.hexdigest()
