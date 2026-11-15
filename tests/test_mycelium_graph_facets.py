"""Facet layers, the facet bonus and layer-scoped colonies. Pure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import combinations

import numpy as np
import pytest

from penish.mycelium import colonies as col
from penish.mycelium import genesis, store, tick
from penish.mycelium.graph import (
    BonusMatrix,
    candidate_edges,
    facet_bonuses,
    facet_graphs,
    quantiles,
    scores,
)
from penish.mycelium.tunables import DEFAULTS, TICK_SECONDS, Tunables, with_overrides

FloatArray = np.ndarray
SUBJECT, SETTING, STYLE, MOOD = "facet:subject", "facet:setting", "facet:style", "facet:mood"


def corpus(n: int, dim: int, seed: int) -> dict[int, FloatArray]:
    rng = np.random.default_rng(seed)
    return {i: rng.normal(size=dim) for i in range(1, n + 1)}


def brute_bonus(
    vectors: dict[str, dict[int, FloatArray]], weight: float
) -> dict[str, dict[tuple[int, int], float]]:
    """The formula written out pair by pair, to hold the vectorised one to it."""
    q: dict[str, dict[tuple[int, int], float]] = {}
    for name, vs in vectors.items():
        ids = sorted(vs)
        if len(ids) < 2:
            q[name] = {}
            continue
        s = scores(np.stack([vs[i] for i in ids]), None)
        pairs = list(combinations(range(len(ids)), 2))
        qq = quantiles(np.asarray([s[a, b] for a, b in pairs]))
        q[name] = {}
        for (a, b), v in zip(pairs, qq, strict=True):
            q[name][(ids[a], ids[b])] = float(v)
            q[name][(ids[b], ids[a])] = float(v)
    out: dict[str, dict[tuple[int, int], float]] = {}
    union = sorted({i for vs in vectors.values() for i in vs})
    for f in vectors:
        out[f] = {}
        for a in union:
            for b in union:
                if a == b or (a, b) not in q[f]:
                    out[f][(a, b)] = 0.0
                    continue
                gaps = [q[f][(a, b)] - q[g][(a, b)] for g in vectors if g != f and (a, b) in q[g]]
                out[f][(a, b)] = weight * max(0.0, max(gaps)) if gaps else 0.0
    return out


def test_bonus_matches_the_formula_pair_by_pair() -> None:
    vectors = {
        SUBJECT: corpus(9, 5, 1),
        SETTING: corpus(9, 5, 2),
        STYLE: corpus(9, 5, 3),
        # One picture has no mood: it sits out of that layer only.
        MOOD: {i: v for i, v in corpus(9, 5, 4).items() if i != 4},
    }
    got = facet_bonuses(vectors, {}, 0.15)
    want = brute_bonus(vectors, 0.15)
    assert set(got) == set(vectors)
    for f in vectors:
        for (a, b), v in want[f].items():
            assert got[f](a, b) == pytest.approx(v, abs=1e-12), (f, a, b)
        # Symmetric, never negative, at most the weight, zero on the diagonal.
        m = got[f].values
        assert np.allclose(m, m.T)
        assert m.min() >= 0.0 and m.max() <= 0.15 + 1e-12
        assert np.all(np.diag(m) == 0.0)
    # Picture 4 has no mood, so the mood layer gives it nothing and no other layer
    # measures itself against a mood it does not have.
    assert all(got[MOOD](4, j) == 0.0 for j in range(1, 10))


def test_same_mood_different_subject_is_the_case_it_rewards() -> None:
    # 1 and 2 share a mood and nothing else; 3 and 4 share a subject and nothing else.
    e = np.eye(4)
    vectors = {
        SUBJECT: {1: e[0], 2: e[1], 3: e[2], 4: e[2] + 0.01 * e[3], 5: e[3]},
        MOOD: {1: e[0], 2: e[0] + 0.01 * e[1], 3: e[1], 4: e[2], 5: e[3]},
    }
    b = facet_bonuses(vectors, {}, 0.5)
    assert b[MOOD](1, 2) > 0.3
    assert b[SUBJECT](1, 2) == 0.0
    assert b[SUBJECT](3, 4) > 0.3
    assert b[MOOD](3, 4) == 0.0


def test_no_bonus_with_zero_weight_or_one_layer() -> None:
    vs = corpus(6, 3, 7)
    assert facet_bonuses({MOOD: vs, STYLE: corpus(6, 3, 8)}, {}, 0.0) == {}
    assert facet_bonuses({MOOD: vs}, {}, 0.15) == {}
    t = with_overrides(DEFAULTS, {"facet_bonus": 0.0})
    plain = {name: candidate_edges(name, vs, None, t) for name in (MOOD, STYLE)}
    assert facet_graphs({MOOD: vs, STYLE: vs}, {}, t) == plain


def test_the_bonus_moves_neighbors_and_stays_in_raw() -> None:
    vectors = {SUBJECT: corpus(12, 6, 11), MOOD: corpus(12, 6, 12)}
    t = with_overrides(DEFAULTS, {"facet_bonus": 1.0, "candidates": 3})
    plain = candidate_edges(MOOD, vectors[MOOD], None, t)
    got = facet_graphs(vectors, {}, t)[MOOD]
    b = facet_bonuses(vectors, {}, 1.0)[MOOD]
    s = scores(np.stack([vectors[MOOD][i] for i in sorted(vectors[MOOD])]), None)
    for e in got:
        assert e.raw == pytest.approx(s[e.src - 1, e.dst - 1] + b(e.src, e.dst))
    assert got != plain


def test_matrix_bonus_equals_the_same_bonus_as_a_callable() -> None:
    vs = corpus(10, 4, 5)
    rng = np.random.default_rng(0)
    m = rng.random((12, 12))
    # Covers ids the layer does not have (11, 12) and leaves 0 for none.
    bm = BonusMatrix(list(range(1, 13)), m)
    a = candidate_edges(MOOD, vs, None, DEFAULTS, bonus=bm)
    b = candidate_edges(MOOD, vs, None, DEFAULTS, bonus=lambda i, j: bm(i, j))
    assert a == b
    assert bm(1, 99) == 0.0
    sub = bm.take([2, 5, 50])
    assert sub[0, 1] == m[1, 4] and sub[2, 2] == 0.0 and sub[0, 2] == 0.0
    with pytest.raises(ValueError):
        BonusMatrix([2, 1], np.zeros((2, 2)))
    with pytest.raises(ValueError):
        BonusMatrix([1, 2], np.zeros((3, 3)))


def test_a_picture_without_a_facet_vector_is_absent_from_that_layer() -> None:
    whole = store.GraphInputs(corpus(8, 4, 1), None)
    mood = store.GraphInputs({i: v for i, v in corpus(8, 4, 2).items() if i != 3}, None)
    inputs = {SUBJECT: store.GraphInputs(corpus(8, 4, 3), None), MOOD: mood}
    graphs = tick.build_graphs(whole, inputs, DEFAULTS, centered_min=30)
    assert list(graphs) == list(tick.ACTIVE_LAYERS)
    assert graphs[SETTING] == [] and graphs[STYLE] == []
    assert not any(3 in (e.src, e.dst) for e in graphs[MOOD])
    assert any(3 in (e.src, e.dst) for e in graphs[SUBJECT])
    assert any(3 in (e.src, e.dst) for e in graphs["whole"])
    assert all(e.layer == MOOD for e in graphs[MOOD])
    # Only layers with edges step, beside the whole layer.
    assert set(tick.stepping(graphs)) == {"whole", SUBJECT, MOOD}


def test_each_facet_layer_centers_on_its_own_mean() -> None:
    vs = corpus(10, 4, 9)
    mean = np.mean(np.stack(list(vs.values())), axis=0) + 0.3
    inputs = {MOOD: store.GraphInputs(vs, mean), STYLE: store.GraphInputs(vs, None)}
    t = with_overrides(DEFAULTS, {"facet_bonus": 0.0})
    got = tick.build_facet_graphs(inputs, t, centered_min=5)
    assert got[MOOD] == candidate_edges(MOOD, vs, mean, t)
    assert got[STYLE] == candidate_edges(STYLE, vs, None, t)
    # Below the threshold the mean is not used, as for the whole layer.
    small = tick.build_facet_graphs(inputs, t, centered_min=11)
    assert small[MOOD] == candidate_edges(MOOD, vs, None, t)


def test_facet_bonus_is_a_tunable_that_rebuilds_the_graph() -> None:
    assert Tunables().facet_bonus == 0.15
    assert "facet_bonus" in store.GRAPH_TUNABLES
    with pytest.raises(ValueError):
        with_overrides(DEFAULTS, {"facet_bonus": 1.5})


# -- layer-scoped colonies and genesis ----------------------------------------


def chain(*ids: int) -> list[tuple[int, int, float]]:
    return [(a, b, 0.5) for a, b in zip(ids, ids[1:], strict=False)]


FAST = with_overrides(DEFAULTS, {"fruit_ticks": 2, "admit_ticks": 1})


def test_scoped_step_holds_other_layers_and_lets_only_its_own_fruit() -> None:
    live = set(range(1, 20))
    # A whole colony forms and is about to fruit; a mood colony matches it exactly.
    state = col.ColoniesState()
    for k in range(3):
        state, _ = col.step(state, k, {"whole": chain(1, 2, 3)}, live, FAST)
    assert [c.state for c in state.colonies] == ["alive"]
    whole = state.colonies[0]
    held = col.ColoniesState(
        colonies=list(state.colonies),
        next_id=state.next_id,
        next_serial=state.next_serial,
        recent_fruitings=[],
        tick=None,
    )
    events: list[col.Event] = []
    s = held
    for k in range(10, 16):
        # Nothing feeds the whole layer here; scoped, it must not fade or change.
        s, ev = col.step(s, k, {MOOD: chain(1, 2, 3)}, live, FAST, [MOOD])
        events += ev
    held_now = next(c for c in s.colonies if c.id == whole.id)
    assert held_now == whole
    mood = [c for c in s.colonies if c.layer == MOOD]
    # The same members as a living whole gallery: a duplicate, so it never fruits.
    assert len(mood) == 1 and mood[0].state == "forming" and mood[0].serial is None
    assert all(e.layer == MOOD for e in events)
    # Different members fruit, with the next serial, and count toward the cap.
    s2, _ = col.step(s, 16, {MOOD: chain(5, 6, 7)}, live, FAST, [MOOD])
    for k in range(17, 20):
        s2, _ = col.step(s2, k, {MOOD: chain(5, 6, 7)}, live, FAST, [MOOD])
    fruited = [c for c in s2.colonies if c.layer == MOOD and c.serial is not None]
    assert [c.serial for c in fruited] == [whole.serial + 1]  # type: ignore[operator]
    assert len(s2.recent_fruitings) == 1


def test_scoped_step_rejects_edges_for_a_layer_that_does_not_step() -> None:
    with pytest.raises(ValueError, match="does not step"):
        col.step(col.ColoniesState(), 0, {"whole": chain(1, 2, 3)}, {1, 2, 3}, FAST, [MOOD])
    with pytest.raises(ValueError, match="unknown layer"):
        col.step(col.ColoniesState(), 0, {}, {1, 2, 3}, FAST, ["facet:taste"])


def test_genesis_scope() -> None:
    assert genesis.scope(None) is None
    assert genesis.scope(tick.ACTIVE_LAYERS) is None
    assert genesis.scope([MOOD, SUBJECT]) == (SUBJECT, MOOD)
    for bad, msg in (
        (["whole", MOOD], "whole replays with every layer"),
        (["whole"], "whole replays with every layer"),
        (["need"], "unknown layer need"),
        ([], "no layers"),
    ):
        with pytest.raises(genesis.GenesisRefused, match=msg):
            genesis.scope(bad)


def test_entries_at_follow_the_recorded_clock() -> None:
    t0 = datetime(2026, 5, 1, tzinfo=UTC)
    tick_len = timedelta(seconds=TICK_SECONDS)
    # Ticks 0..2, a pause, then ticks 3..4 a day later.
    times = [t0, t0 + tick_len, t0 + 2 * tick_len]
    later = t0 + timedelta(days=1)
    times += [later, later + tick_len]
    published = {
        1: None,
        2: t0 - timedelta(days=3),
        3: t0 + tick_len + timedelta(minutes=1),
        4: t0 + timedelta(hours=5),  # during the pause
        5: later + tick_len,
        6: later + timedelta(days=2),  # after the last window
    }
    assert genesis.entries_at(published, times) == {1: 0, 2: 0, 3: 1, 4: 3, 5: 4, 6: 4}
    assert genesis.entries_at(published, []) == {}
