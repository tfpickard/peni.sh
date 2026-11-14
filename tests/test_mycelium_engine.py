"""Pure tests for the candidate graph, the tick and overgrowth. No database."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from penish.mycelium import colonies, overgrowth
from penish.mycelium.colonies import ColoniesState
from penish.mycelium.engine import (
    EdgeState,
    LayerState,
    latched_edges,
    layer_key,
    layer_rng,
    neighbor_levels,
    run_tick,
    state_hash,
    step,
)
from penish.mycelium.graph import Edge, candidate_edges, quantiles, scores
from penish.mycelium.tunables import DEFAULTS, LAYERS, TunableError, Tunables, with_overrides

L = "whole"
LAYER_KEY_WHOLE = 6725957608508256278
GOLDEN_ENGINE = "2352634fd3a498e97b9648faa8fcc471ab14665e337b49a2fad158c59e1afeb2"
GOLDEN_COLONIES = "6fd6142b20c3282421c2197b7e663db639e3480f20926d35b2f30f9e86da6955"


def corpus(n: int = 30, dim: int = 8, seed: int = 7) -> dict[int, np.ndarray]:
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(4, dim))
    return {10 + i: centers[i % 4] + 0.3 * rng.normal(size=dim) for i in range(n)}


def graph_for(t: Tunables = DEFAULTS, n: int = 30) -> list[Edge]:
    vecs = corpus(n)
    mean = np.mean(np.stack(list(vecs.values())), axis=0)
    return candidate_edges(L, vecs, mean, t)


def pair_graph() -> list[Edge]:
    # Two pictures: a walker from either side has exactly one move, then none.
    return [Edge(L, 1, 2, 0.9, 0.5), Edge(L, 2, 1, 0.9, 0.5)]


def run(
    t: Tunables,
    ticks: int,
    seed: int = 1,
    graph: list[Edge] | None = None,
    live: list[int] | None = None,
    traffic: dict[tuple[int, int], int] | None = None,
) -> tuple[LayerState, list[LayerState]]:
    g = graph if graph is not None else graph_for(t)
    ids = live if live is not None else sorted({e.src for e in g} | {e.dst for e in g})
    state = LayerState.empty(L)
    history = []
    for tick in range(ticks):
        state, _ = step(state, g, traffic or {}, ids, t, seed, tick, L)
        history.append(state)
    return state, history


# graph


def test_graph_too_small_has_no_edges() -> None:
    vecs = {1: np.ones(3), 2: np.arange(3.0)}
    assert candidate_edges(L, vecs, None, DEFAULTS) == []


def test_graph_k_nearest_sorted_and_floored() -> None:
    edges = graph_for()
    assert [(e.src, e.dst) for e in edges] == sorted((e.src, e.dst) for e in edges)
    by_src: dict[int, int] = {}
    for e in edges:
        by_src[e.src] = by_src.get(e.src, 0) + 1
        assert e.src != e.dst
        assert DEFAULTS.affinity_floor <= e.affinity <= 1.0
    assert set(by_src.values()) == {DEFAULTS.candidates}
    assert min(e.affinity for e in edges) == DEFAULTS.affinity_floor
    # The top raw score belongs to a mutual pair, so its two edges share the top ranks.
    assert max(e.affinity for e in edges) > 0.99


def test_quantile_ties_averaged() -> None:
    q = quantiles(np.array([3.0, 1.0, 2.0, 2.0]))
    assert q.tolist() == [1.0, 0.0, 0.5, 0.5]


def test_affinity_floor_lifts_low_quantiles() -> None:
    t = with_overrides(DEFAULTS, {"affinity_floor": 0.4})
    edges = graph_for(t)
    q = quantiles(np.array([e.raw for e in edges]))
    for e, qq in zip(edges, q, strict=True):
        assert e.affinity == max(0.4, float(qq))
    assert sum(1 for e in edges if e.affinity == 0.4) > 1


def test_graph_ties_break_by_lower_id() -> None:
    v = np.array([1.0, 0.0])
    vecs = {5: v, 3: v, 9: v, 7: v}
    t = with_overrides(DEFAULTS, {"candidates": 2})
    edges = candidate_edges(L, vecs, None, t)
    assert [(e.src, e.dst) for e in edges if e.src == 9] == [(9, 3), (9, 5)]


def test_centered_falls_back_to_raw_for_the_mean_itself() -> None:
    x = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
    s = scores(x, np.array([0.5, 0.5]))
    raw = scores(x, None)
    assert s[2, 0] == raw[2, 0]
    assert s[0, 1] == pytest.approx(-1.0)


def test_bonus_hook_changes_selection() -> None:
    vecs = corpus(12)
    t = with_overrides(DEFAULTS, {"candidates": 2})
    plain = candidate_edges(L, vecs, None, t)
    boosted = candidate_edges(
        L, vecs, None, t, bonus=lambda a, b: 10.0 if (a, b) == (10, 21) else 0.0
    )
    assert (10, 21) not in {(e.src, e.dst) for e in plain}
    hit = [e for e in boosted if (e.src, e.dst) == (10, 21)]
    assert hit and hit[0].affinity == 1.0


# engine


def test_layer_key_is_from_name() -> None:
    assert layer_key("whole") == layer_key("whole")
    assert len({layer_key(name) for name in LAYERS}) == len(LAYERS)
    for name in LAYERS:
        digest = hashlib.sha256(name.encode()).digest()
        assert layer_key(name) == int.from_bytes(digest[:8], "big")
    # Pinned, so a key that differs between processes (a salted hash) fails here.
    assert layer_key("whole") == LAYER_KEY_WHOLE


def test_state_hash_covers_every_field() -> None:
    base = EdgeState(strength=0.4, latched=True, fatigue=0.25, fused=True, idle=3)
    seen = {state_hash(LayerState(L, {(1, 2): base}))}
    changes: dict[str, object] = {
        "strength": 0.41,
        "latched": False,
        "fatigue": 0.26,
        "fused": False,
        "idle": 4,
    }
    assert set(changes) == {f.name for f in dataclasses.fields(EdgeState)}
    for name, value in changes.items():
        other = dataclasses.replace(base, **{name: value})
        seen.add(state_hash(LayerState(L, {(1, 2): other})))
    seen.add(state_hash(LayerState(L, {(2, 1): base})))
    seen.add(state_hash(LayerState("facet:mood", {(1, 2): base})))
    assert len(seen) == len(changes) + 3


def test_strength_bounded_and_budget_held() -> None:
    t = with_overrides(
        DEFAULTS, {"rho": 0.2, "walkers": 8, "steps": 12, "budget": 0.6, "colony_leave": 0.2}
    )
    _, history = run(t, 60)
    assert any(s.edges for s in history)
    for state in history:
        out: dict[int, float] = {}
        for (src, _), es in state.edges.items():
            assert 0.0 <= es.strength <= 1.0
            assert 0.0 <= es.fatigue <= 1.0
            out[src] = out.get(src, 0.0) + es.strength
        assert max(out.values()) <= t.budget + 1e-12
    assert max(out.values()) == pytest.approx(t.budget)


def test_replay_is_identical_and_seed_matters() -> None:
    t = with_overrides(DEFAULTS, {"rho": 0.1})
    a, _ = run(t, 20, seed=3)
    b, _ = run(t, 20, seed=3)
    c, _ = run(t, 20, seed=4)
    assert state_hash(a) == state_hash(b)
    assert state_hash(a) != state_hash(c)
    assert state_hash({L: a}) == state_hash({L: b})


def test_wither_needs_weak_and_idle() -> None:
    t = DEFAULTS
    idle = t.wither_idle - 1
    state = LayerState(
        L,
        {
            (1, 2): EdgeState(strength=0.01, idle=idle),  # weak and idle: goes
            (1, 3): EdgeState(strength=0.01, idle=0),  # weak, recently used: stays
            (2, 3): EdgeState(strength=0.2, idle=idle),  # idle but strong: stays
        },
    )
    new, report = step(state, [], {}, [1, 2, 3], t, 1, 0, L)
    assert set(new.edges) == {(1, 3), (2, 3)}
    assert report.withered == 1
    assert new.edges[(1, 3)].idle == 1
    assert new.edges[(2, 3)].idle == t.wither_idle


def test_flow_resets_idle() -> None:
    state = LayerState(L, {(1, 2): EdgeState(strength=0.01, idle=DEFAULTS.wither_idle - 1)})
    new, _ = step(state, pair_graph(), {}, [1, 2], DEFAULTS, 1, 0, L)
    assert new.edges[(1, 2)].idle == 0


@pytest.mark.parametrize(
    ("before", "latched", "after"),
    [
        (0.31, False, True),  # 0.31 decays to 0.3069, still at or above enter
        (0.27, False, False),  # between the thresholds: stays unlatched
        (0.27, True, True),  # between the thresholds: stays latched
        (0.242, True, False),  # decays under leave: lets go
    ],
)
def test_hysteresis(before: float, latched: bool, after: bool) -> None:
    state = LayerState(L, {(1, 2): EdgeState(strength=before, latched=latched)})
    new, _ = step(state, [], {}, [1, 2], DEFAULTS, 1, 0, L)
    assert new.edges[(1, 2)].latched is after


def test_traffic_capped_per_edge_and_by_share() -> None:
    t = with_overrides(DEFAULTS, {"walkers": 8, "steps": 12, "traffic_share": 4.0})
    g = graph_for(t)
    ids = sorted({e.src for e in g})
    clicked = [(e.src, e.dst) for e in g[:5]]
    traffic = {p: 100 for p in clicked}
    traffic[(ids[0], ids[0])] = 100  # not a candidate edge: ignored
    _, rep = step(LayerState.empty(L), g, traffic, ids, t, 1, 0, L)
    assert set(rep.traffic_flow) == set(clicked)
    assert all(v == t.traffic_edge_cap for v in rep.traffic_flow.values())
    assert rep.traffic_scale == 1.0

    tight = with_overrides(t, {"traffic_share": 0.001})
    _, rep = step(LayerState.empty(L), g, traffic, ids, tight, 1, 0, L)
    assert rep.traffic == pytest.approx(0.001 * rep.crossings)
    assert rep.traffic_scale < 1.0
    vals = set(rep.traffic_flow.values())
    assert len(vals) == 1 and vals.pop() < t.traffic_edge_cap

    still = with_overrides(t, {"walkers": 0})
    _, rep = step(LayerState.empty(L), g, traffic, ids, still, 1, 0, L)
    assert rep.crossings == 0 and rep.traffic == 0.0


def test_fused_halves_decay_and_deposit() -> None:
    t = DEFAULTS
    s0 = 0.4

    def after(fused: bool, graph: list[Edge]) -> float:
        state = LayerState(
            L,
            {
                (1, 2): EdgeState(strength=s0, latched=True, fused=fused),
                (2, 1): EdgeState(strength=s0, latched=True, fused=fused),
            },
        )
        new, _ = step(state, graph, {}, [1, 2], t, 1, 0, L)
        return new.edges[(1, 2)].strength

    # Decay only.
    assert s0 - after(True, []) == pytest.approx((s0 - after(False, [])) / 2)
    # One crossing each way: g = 0.5, so strength moves toward it at half the rate.
    up_fused = after(True, pair_graph()) - s0
    up_plain = after(False, pair_graph()) - s0
    assert up_plain == pytest.approx(t.rho * (0.5 - s0))
    assert up_fused == pytest.approx(up_plain / 2)


def test_fused_needs_both_directions_latched() -> None:
    state = LayerState(
        L,
        {
            (1, 2): EdgeState(strength=0.5, latched=True),
            (2, 1): EdgeState(strength=0.5, latched=True),
            (1, 3): EdgeState(strength=0.5, latched=True),
        },
    )
    new, rep = step(state, [], {}, [1, 2, 3], DEFAULTS, 1, 0, L)
    assert new.edges[(1, 2)].fused and new.edges[(2, 1)].fused
    assert not new.edges[(1, 3)].fused
    assert rep.fused == 2


def test_fatigue_rises_while_latched_and_cuts_deposit() -> None:
    state = LayerState(L, {(1, 2): EdgeState(strength=0.5, latched=True, fatigue=1.0)})
    new, _ = step(state, pair_graph(), {}, [1, 2], DEFAULTS, 1, 0, L)
    g = 0.5 * (1 - DEFAULTS.fatigue_gamma)
    assert new.edges[(1, 2)].strength == pytest.approx(0.99 * 0.5 + 0.01 * g)
    assert new.edges[(1, 2)].fatigue == 1.0
    loose = LayerState(L, {(1, 2): EdgeState(strength=0.1, fatigue=0.5)})
    new, _ = step(loose, [], {}, [1, 2], DEFAULTS, 1, 0, L)
    assert new.edges[(1, 2)].fatigue == pytest.approx(0.5 - 1 / DEFAULTS.fatigue_ticks)


def test_non_live_endpoints_drop() -> None:
    state = LayerState(L, {(1, 2): EdgeState(strength=0.5), (1, 5): EdgeState(strength=0.5)})
    g = pair_graph() + [Edge(L, 1, 5, 0.9, 1.0), Edge(L, 5, 1, 0.9, 1.0)]
    new, rep = step(state, g, {(1, 5): 3}, [1, 2], DEFAULTS, 1, 0, L)
    assert set(new.edges) <= {(1, 2), (2, 1)}
    assert rep.dropped == 1
    assert all(5 not in p for p in rep.flow)


def test_walkers_stay_on_candidate_edges() -> None:
    g = graph_for()
    pairs = {(e.src, e.dst) for e in g}
    _, rep = step(LayerState.empty(L), g, {}, sorted({e.src for e in g}), DEFAULTS, 9, 0, L)
    assert rep.crossings > 0
    assert set(rep.walker_flow) <= pairs


def test_walkers_never_backtrack() -> None:
    # A path 1 - 2 - 3: from 1 a walker must go on to 3, and one from 2 stops after
    # one step, so every tick crosses exactly five times.
    path = [Edge(L, a, b, 0.9, 0.5) for a, b in ((1, 2), (2, 1), (2, 3), (3, 2))]
    t = with_overrides(DEFAULTS, {"walkers": 1, "steps": 2})
    for tick in range(50):
        _, rep = step(LayerState.empty(L), path, {}, [1, 2, 3], t, 1, tick, L)
        assert rep.crossings == 5
        assert rep.walker_flow[(1, 2)] == 1.0
        assert rep.walker_flow[(3, 2)] == 1.0


def test_walker_weight_uses_strength_and_affinity() -> None:
    t = with_overrides(
        DEFAULTS, {"walkers": 1, "steps": 1, "epsilon": 0.5, "alpha": 2.0, "beta": 2.0}
    )
    a12, a13, s12 = 0.4, 0.8, 0.6
    fork = [Edge(L, 1, 2, 0.9, a12), Edge(L, 1, 3, 0.9, a13)]
    state = LayerState(L, {(1, 2): EdgeState(strength=s12)})
    w2 = (t.epsilon + s12) ** t.alpha * a12**t.beta
    w3 = (t.epsilon + 0.0) ** t.alpha * a13**t.beta
    picks = {2: 0, 3: 0}
    for tick in range(200):
        # The walker from 1 is the first to draw; 2 and 3 have nowhere to go.
        draw = float(layer_rng(7, tick, L).random())
        want = 2 if w2 > draw * (w2 + w3) else 3
        _, rep = step(state, fork, {}, [1, 2, 3], t, 7, tick, L)
        assert rep.walker_flow == {(1, want): 1.0}
        picks[want] += 1
    # Strength tips an affinity-led choice the other way most of the time.
    assert picks[2] > picks[3] > 0


def test_latched_edges_undirected() -> None:
    state = LayerState(
        L,
        {
            (3, 1): EdgeState(strength=0.4, latched=True),
            (1, 3): EdgeState(strength=0.35, latched=True),
            (2, 4): EdgeState(strength=0.33, latched=True),
            (1, 2): EdgeState(strength=0.2),
        },
    )
    assert latched_edges(state) == [(1, 3, 0.4), (2, 4, 0.33)]


def test_wither_min_above_colony_leave_is_refused() -> None:
    # Otherwise a latched edge could wither after step 8 and leave its reverse fused alone.
    with pytest.raises(TunableError):
        with_overrides(DEFAULTS, {"wither_min": 0.1, "colony_leave": 0.05})
    ok = with_overrides(DEFAULTS, {"wither_min": 0.05, "colony_leave": 0.05})
    state = LayerState(
        L,
        {
            (1, 2): EdgeState(strength=0.0501, latched=True, fused=True, idle=ok.wither_idle),
            (2, 1): EdgeState(strength=0.5, latched=True, fused=True),
        },
    )
    new, rep = step(state, [], {}, [1, 2], ok, 1, 0, L)
    # An edge weak enough to wither has already let go, so its reverse is not fused.
    assert rep.withered == 1
    assert set(new.edges) == {(2, 1)}
    assert not new.edges[(2, 1)].fused
    assert rep.fused == 0


def test_golden_hashes() -> None:
    # Literal values: a change of stream, encoding or rule order across processes,
    # numpy versions or refactors shows up here and nowhere else.
    t = with_overrides(DEFAULTS, {"rho": 0.2, "walkers": 2, "fruit_ticks": 5, "admit_ticks": 2})
    ring = [(1, 2), (2, 3), (3, 1), (4, 5), (5, 6), (6, 4), (3, 4)]
    graph = sorted(
        [Edge(L, a, b, 0.5, 0.9 if (a, b) != (3, 4) else 0.1) for a, b in ring]
        + [Edge(L, b, a, 0.5, 0.9 if (a, b) != (3, 4) else 0.1) for a, b in ring],
        key=lambda e: (e.src, e.dst),
    )
    ids = [1, 2, 3, 4, 5, 6]
    state = LayerState.empty(L)
    cols = ColoniesState()
    kinds: list[str] = []
    for tick in range(40):
        state, _ = step(state, graph, {}, ids, t, 11, tick, L)
        cols, ev = colonies.step(cols, tick, {L: latched_edges(state)}, frozenset(ids), t)
        kinds.extend(e.kind for e in ev)
    assert "fruited" in kinds
    assert state_hash(state) == GOLDEN_ENGINE
    assert colonies.state_hash(cols) == GOLDEN_COLONIES


def test_neighbor_level_weighted_mean_of_visible() -> None:
    whole = LayerState("whole", {(1, 2): EdgeState(strength=0.3), (1, 3): EdgeState(strength=0.1)})
    mood = LayerState("facet:mood", {(4, 1): EdgeState(strength=0.6)})
    base = {1: 0.0, 2: 1.0, 3: 0.5, 4: 0.25}
    levels = neighbor_levels([whole, mood], base, [1, 2, 3, 4, 5], DEFAULTS)
    assert levels[1] == pytest.approx((0.3 * 1.0 + 0.6 * 0.25) / 0.9)
    assert levels[2] == 0.0
    assert levels[3] == 0.0  # 0.1 is below hypha_visible
    assert levels[5] == 0.0


def test_layers_are_independent() -> None:
    t = with_overrides(DEFAULTS, {"rho": 0.1})
    g = graph_for(t)
    ids = sorted({e.src for e in g})
    mood = [Edge("facet:mood", e.src, e.dst, e.raw, e.affinity) for e in g]
    alone = run_tick({}, {L: g}, {}, ids, {}, t, 5, 0)
    both = run_tick({}, {L: g, "facet:mood": mood}, {}, ids, {}, t, 5, 0)
    assert state_hash(alone.states[L]) == state_hash(both.states[L])
    assert list(both.states) == [L, "facet:mood"]
    assert set(both.neighbor_level) == set(ids)


# overgrowth


def test_tau_floor_and_pace() -> None:
    assert overgrowth.tau_hours(10, 1000.0, DEFAULTS) == DEFAULTS.overgrowth_tau_min_h
    assert overgrowth.tau_hours(1000, 10.0, DEFAULTS) == pytest.approx(2 * 1000 / 10 * 24)
    assert overgrowth.tau_hours(1000, 0.0, DEFAULTS) == pytest.approx(2 * 1000 * 24)
    assert overgrowth.views_per_day(0) == 1.0
    assert overgrowth.views_per_day(70) == 10.0


def test_overgrowth_bounds_and_fresh_view() -> None:
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    tau = 72.0
    assert overgrowth.base_level(now, now, tau) == 0.0
    assert overgrowth.level(overgrowth.base_level(now, now, tau), 1.0, DEFAULTS) == 0.0
    assert overgrowth.base_level(now + timedelta(hours=1), now, tau) == 0.0
    for hours in (0.0, 1.0, 72.0, 1e4, 1e9):
        b = overgrowth.base_from_hours(hours, tau)
        assert 0.0 <= b <= 1.0
        for n in (0.0, 0.5, 1.0):
            lv = overgrowth.level(b, n, DEFAULTS)
            assert 0.0 <= lv <= 1.0
            assert 0.0 <= overgrowth.coverage(lv, DEFAULTS) <= DEFAULTS.overgrowth_cap
    assert overgrowth.base_from_hours(tau, tau) == pytest.approx(1 - np.exp(-1))
    assert overgrowth.level(0.9, 1.0, DEFAULTS) == 1.0
    assert overgrowth.level(0.4, 1.0, DEFAULTS) == pytest.approx(0.6)
