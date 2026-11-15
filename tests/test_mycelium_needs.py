"""The need layer: directed need edges, damping by satisfaction, feeding and drift, and
the tick's load and persist of phrases and satisfaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import numpy as np
import pytest

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium import colonies, engine, genesis, store, tick
from penish.mycelium.engine import (
    EdgeState,
    LayerState,
    damped,
    feed_needs,
    latched_edges,
    run_tick,
    state_hash,
    step,
)
from penish.mycelium.graph import NEED, Edge, Phrase, need_edges
from penish.mycelium.tunables import DEFAULTS, TunableError, with_overrides
from tests.test_mycelium_tick import (  # noqa: F401
    DIM,
    MODEL,
    T0,
    TICK,
    _settings,
    add,
    one,
    running,
    sql,
)


@pytest.fixture
async def p(clean_db: str) -> AsyncIterator[Any]:
    pool = make_pool(clean_db)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def ph(pid: int, image: int, *v: float) -> Phrase:
    return Phrase(pid, image, np.asarray(v, dtype=np.float64))


# -- graph --------------------------------------------------------------------


def test_need_edges_are_directed() -> None:
    # 1 needs what 2 offers; nobody needs what 1 or 3 offer.
    needs = [ph(1, 1, 1.0, 0.0, 0.0)]
    offers = [ph(10, 2, 1.0, 0.1, 0.0), ph(11, 1, 0.0, 1.0, 0.0), ph(12, 3, 0.0, 0.0, 1.0)]
    edges = need_edges(needs, offers, DEFAULTS)
    pairs = [(e.src, e.dst) for e in edges]
    assert pairs == [(1, 2), (1, 3)]
    assert (2, 1) not in pairs
    assert all(e.layer == NEED for e in edges)
    # Its own offer never answers its own need.
    assert all(e.dst != e.src for e in edges)
    first = edges[0]
    assert (first.need, first.offer) == (1, 10)
    assert first.raw == pytest.approx(1.0 / np.sqrt(1.01))
    # Two edges: the quantile is 0 and 1, and the floor lifts the weaker one.
    assert [e.affinity for e in edges] == [1.0, DEFAULTS.affinity_floor]


def test_best_match_per_pair_and_ties_to_lower_ids() -> None:
    # Picture 1 has two needs; picture 2 answers the second better, picture 3 answers
    # both equally, so the lower need id wins, then the lower offer id.
    needs = [ph(1, 1, 1.0, 0.0), ph(2, 1, 0.0, 1.0)]
    offers = [
        ph(20, 2, 0.6, 0.8),
        ph(30, 3, 1.0, 1.0),
        ph(31, 3, 1.0, 1.0),
        ph(40, 4, -1.0, 0.0),
    ]
    edges = {(e.src, e.dst): e for e in need_edges(needs, offers, DEFAULTS)}
    assert (edges[(1, 2)].need, edges[(1, 2)].offer) == (2, 20)
    assert edges[(1, 2)].raw == pytest.approx(0.8)
    assert (edges[(1, 3)].need, edges[(1, 3)].offer) == (1, 30)
    assert edges[(1, 3)].raw == pytest.approx(np.sqrt(0.5))
    # 4's offer is the opposite of one need and orthogonal to the other.
    assert (edges[(1, 4)].need, edges[(1, 4)].offer) == (2, 40)
    assert edges[(1, 4)].raw == pytest.approx(0.0)


def test_keeps_candidates_best_ties_to_lower_picture() -> None:
    t = with_overrides(DEFAULTS, {"candidates": 2})
    needs = [ph(1, 1, 1.0, 0.0)]
    # Pictures 3, 5 and 6 tie at the top, so the two lowest stay; 4 and 2 are weaker.
    offers = [
        ph(10, 2, 0.0, 1.0),
        ph(11, 3, 1.0, 0.0),
        ph(12, 4, 1.0, 1.0),
        ph(13, 5, 2.0, 0.0),
        ph(14, 6, 3.0, 0.0),
    ]
    edges = need_edges(needs, offers, t)
    assert [(e.src, e.dst) for e in edges] == [(1, 3), (1, 5)]


def test_too_few_pictures_or_no_phrases() -> None:
    assert need_edges([ph(1, 1, 1.0)], [ph(2, 2, 1.0)], DEFAULTS) == []
    assert need_edges([], [ph(2, 2, 1.0), ph(3, 3, 1.0), ph(4, 4, 1.0)], DEFAULTS) == []
    assert need_edges([ph(1, 1, 1.0), ph(2, 2, 1.0), ph(3, 3, 1.0)], [], DEFAULTS) == []


def test_need_graph_is_deterministic_and_sorted() -> None:
    rng = np.random.default_rng(3)
    needs = [ph(i, 1 + i % 9, *rng.normal(size=6)) for i in range(1, 30)]
    offers = [ph(100 + i, 1 + i % 11, *rng.normal(size=6)) for i in range(40)]
    a = need_edges(needs, offers, DEFAULTS)
    b = need_edges(list(reversed(needs)), list(reversed(offers)), DEFAULTS)
    assert a == b
    assert [(e.src, e.dst) for e in a] == sorted((e.src, e.dst) for e in a)
    for src in {e.src for e in a}:
        assert sum(1 for e in a if e.src == src) <= DEFAULTS.candidates
    assert all(DEFAULTS.affinity_floor <= e.affinity <= 1.0 for e in a)
    # Every edge names a need of its source and an offer of its destination.
    owner = {x.id: x.image for x in needs + offers}
    assert all(owner[e.need] == e.src and owner[e.offer] == e.dst for e in a if e.need and e.offer)


# -- damping -------------------------------------------------------------------


def test_damped_affinity() -> None:
    assert damped(0.5, 0.0, DEFAULTS) == 0.5
    assert damped(0.5, 1.0, DEFAULTS) == pytest.approx(0.5 * (1 - DEFAULTS.need_damp))
    t = with_overrides(DEFAULTS, {"need_damp": 1.0})
    assert damped(0.5, 1.0, t) == 0.0


def two_needs() -> list[Edge]:
    # Picture 1 can reach 2 through need 7 or 3 through need 8, equally close.
    return [Edge(NEED, 1, 2, 0.9, 0.9, 7, 70), Edge(NEED, 1, 3, 0.9, 0.9, 8, 80)]


def test_a_satisfied_need_draws_fewer_walkers() -> None:
    t = with_overrides(DEFAULTS, {"walkers": 8, "steps": 1, "need_damp": 0.9})
    flow7 = flow8 = 0.0
    for k in range(200):
        _, rep = step(LayerState.empty(NEED), two_needs(), {}, [1, 2, 3], t, 1, k, NEED, {7: 1.0})
        flow7 += rep.flow.get((1, 2), 0.0)
        flow8 += rep.flow.get((1, 3), 0.0)
    # Weight ratio (0.1 ** beta) = 0.01: need 7 is all but ignored.
    assert flow8 > 20 * max(flow7, 1.0)


def test_damping_never_touches_edges_without_a_need() -> None:
    g = [Edge("whole", 1, 2, 0.9, 0.7), Edge("whole", 2, 1, 0.9, 0.7)]
    a, _ = step(LayerState.empty("whole"), g, {}, [1, 2], DEFAULTS, 1, 0, "whole")
    b, _ = step(LayerState.empty("whole"), g, {}, [1, 2], DEFAULTS, 1, 0, "whole", {1: 1.0})
    assert state_hash(a) == state_hash(b)


# -- feeding and drift ---------------------------------------------------------


def test_feed_and_drift_math() -> None:
    t = DEFAULTS
    graph = two_needs()
    out = feed_needs({7: 0.2, 8: 0.5, 9: 0.0}, graph, {(1, 2): 0.5, (1, 3): 0.0}, t)
    fed = 0.2 + t.need_feed * (1.0 - 0.2) * 0.5
    assert out[7] == fed - t.need_drift * fed
    assert out[8] == 0.5 - t.need_drift * 0.5
    assert out[9] == 0.0
    # Flow counts at most 1.
    big = feed_needs({7: 0.2}, graph, {(1, 2): 40.0}, t)
    fed = 0.2 + t.need_feed * (1.0 - 0.2) * 1.0
    assert big[7] == fed - t.need_drift * fed


def test_a_need_on_two_edges_sums_their_flow() -> None:
    graph = [Edge(NEED, 1, 2, 0.9, 0.9, 7, 70), Edge(NEED, 1, 3, 0.9, 0.9, 7, 80)]
    a = feed_needs({7: 0.0}, graph, {(1, 2): 0.25, (1, 3): 0.25}, DEFAULTS)
    b = feed_needs({7: 0.0}, graph[:1], {(1, 2): 0.5}, DEFAULTS)
    assert a == b


def test_feeding_only_writes_needs_it_was_given() -> None:
    out = feed_needs({8: 0.5}, two_needs(), {(1, 2): 1.0}, DEFAULTS)
    assert set(out) == {8}


def test_satisfaction_stays_in_bounds_and_settles() -> None:
    t = with_overrides(DEFAULTS, {"need_feed": 1.0, "need_drift": 0.5})
    s = {7: 0.0}
    for _ in range(50):
        s = feed_needs(s, two_needs(), {(1, 2): 3.0}, t)
        assert 0.0 <= s[7] <= 1.0
    # Fed to 1 and halved every tick: it sits at one half.
    assert s[7] == 0.5
    for _ in range(2000):
        s = feed_needs(s, two_needs(), {}, DEFAULTS)
    assert 0.0 <= s[7] < 0.01


def test_need_tunables_have_bounds() -> None:
    assert (DEFAULTS.need_damp, DEFAULTS.need_feed, DEFAULTS.need_drift) == (0.8, 0.05, 0.002)
    for name in ("need_damp", "need_feed", "need_drift"):
        with pytest.raises(TunableError):
            with_overrides(DEFAULTS, {name: -0.1})
        with pytest.raises(TunableError):
            with_overrides(DEFAULTS, {name: 1.5})


# -- one tick ------------------------------------------------------------------


def need_ring() -> list[Edge]:
    # Six pictures; each needs what the next one offers, so the edges run one way.
    return [Edge(NEED, i, i % 6 + 1, 0.9, 0.9, 100 + i, 200 + i % 6 + 1) for i in range(1, 7)]


def test_run_tick_feeds_needs_and_is_deterministic() -> None:
    t = with_overrides(DEFAULTS, {"rho": 0.2})
    sat = {100 + i: 0.0 for i in range(1, 7)}
    a = run_tick({}, {NEED: need_ring()}, {}, range(1, 7), {}, t, 3, 0, sat)
    b = run_tick({}, {NEED: need_ring()}, {}, range(1, 7), {}, t, 3, 0, dict(sat))
    assert a.satisfaction == b.satisfaction
    assert state_hash(a.states, a.satisfaction) == state_hash(b.states, b.satisfaction)
    # Every walker crosses its picture's one edge, so every need was fed.
    assert all(
        v == pytest.approx(t.need_feed * (1 - t.need_drift)) for v in a.satisfaction.values()
    )
    # The edge state is the same with or without satisfaction to carry.
    c = run_tick({}, {NEED: need_ring()}, {}, range(1, 7), {}, t, 3, 0)
    assert c.satisfaction == {}
    assert state_hash(c.states) == state_hash(a.states)


def test_state_hash_includes_satisfaction_but_not_zeros() -> None:
    st = {NEED: LayerState(NEED, {(1, 2): EdgeState(strength=0.3)})}
    plain = state_hash(st)
    assert state_hash(st, {7: 0.0, 8: 0.0}) == plain
    assert state_hash(st, {}) == plain
    assert state_hash(st, {7: 0.25}) != plain
    assert state_hash(st, {7: 0.25}) != state_hash(st, {7: 0.25000000000000006})
    assert state_hash(st, {7: 0.25, 8: 0.0}) == state_hash(st, {7: 0.25})


def test_colonies_grow_on_the_need_layer() -> None:
    t = with_overrides(DEFAULTS, {"rho": 0.2, "fruit_ticks": 3, "admit_ticks": 1})
    graph = {NEED: need_ring()}
    states: dict[str, LayerState] = {}
    sat = {100 + i: 0.0 for i in range(1, 7)}
    col = colonies.ColoniesState()
    kinds: list[str] = []
    for k in range(40):
        s = tick.step(states, graph, {}, list(range(1, 7)), {}, t, 1, k, col, None, sat)
        states, col, sat = dict(s.result.states), s.colonies, dict(s.result.satisfaction)
        kinds.extend(e.kind for e in s.events)
    # One-way edges latch and group as undirected pairs, as on every other layer.
    assert latched_edges(states[NEED])
    assert all(not es.fused for es in states[NEED].edges.values())
    assert "formed" in kinds
    assert any(c.layer == NEED for c in col.colonies)
    assert all(0.0 < v <= 1.0 for v in sat.values())


def test_genesis_never_replays_the_need_layer() -> None:
    with pytest.raises(genesis.GenesisRefused, match="unknown layer need"):
        genesis.scope(["need"])
    with pytest.raises(genesis.GenesisRefused, match="unknown layer need"):
        genesis.scope([*tick.ACTIVE_LAYERS, "need"])
    assert NEED not in tick.ACTIVE_LAYERS
    assert tick.GRAPH_LAYERS == (*tick.ACTIVE_LAYERS, NEED)


# -- the live tick ---------------------------------------------------------------

# Four directions in DIM = 4; picture n needs direction n % 4 and offers direction
# (n + 1) % 4, so a need of n meets the offers of pictures that sit one step behind.
AXES = np.eye(DIM)


def phrase(dsn: str, image_id: int, role: str, text: str, v: np.ndarray) -> int:
    rows = sql(
        dsn,
        "insert into offers_needs (image_id, role, phrase, vec, model, dim)"
        " values (%s, %s, %s, %s::vector, %s, %s) returning id",
        image_id,
        role,
        text,
        str([float(x) for x in v]),
        MODEL,
        DIM,
    )
    return int(rows[0][0])


def phrased(dsn: str, n: int = 8) -> tuple[list[int], dict[int, int]]:
    """Live pictures with one need and one offer each; returns ids and need id by picture."""
    ids = [add(dsn, i) for i in range(1, n + 1)]
    needs: dict[int, int] = {}
    for k, pid in enumerate(ids):
        needs[pid] = phrase(dsn, pid, "need", f"need {k}", AXES[k % DIM] + 0.01 * k)
        phrase(dsn, pid, "offer", f"offer {k}", AXES[(k + 1) % DIM] + 0.02 * k)
    return ids, needs


async def test_the_tick_builds_need_edges_and_persists_satisfaction(p: Any, clean_db: str) -> None:
    ids, needs = phrased(clean_db)
    await running(p)
    for k in range(6):
        assert await one(p, T0 + k * TICK) is not None
    rows = sql(
        clean_db,
        "select c.src, c.dst, n.image_id, n.role, o.image_id, o.role from candidate_edges c"
        " join offers_needs n on n.id = c.need_id join offers_needs o on o.id = c.offer_id"
        " where c.layer = 'need'",
    )
    assert rows
    for src, dst, nimg, nrole, oimg, orole in rows:
        assert (nimg, nrole, oimg, orole) == (src, "need", dst, "offer")
    assert sql(
        clean_db,
        "select count(*) from candidate_edges where layer <> 'need' and need_id is not null",
    ) == [(0,)]
    sat = dict(sql(clean_db, "select id, satisfaction from offers_needs where role = 'need'"))
    assert any(v > 0 for v in sat.values())
    assert sql(clean_db, "select max(satisfaction) from offers_needs where role = 'offer'") == [
        (0.0,)
    ]
    stats = sql(clean_db, "select stats from mycelium_ticks order by tick desc limit 1")[0][0]
    assert stats[NEED]["needs"] == len(needs)
    # A reload hashes as the state that was saved.
    async with p.connection() as conn:
        states = await store.load_edge_states(conn)
        loaded = await store.load_satisfaction(conn, model=MODEL, dim=DIM)
    stored = sql(clean_db, "select state_hash from mycelium_ticks order by tick desc limit 1")
    assert engine.state_hash(states, loaded) == stored[0][0]


async def test_unpublished_pictures_hold_no_need_edges(p: Any, clean_db: str) -> None:
    ids, _ = phrased(clean_db)
    sql(clean_db, "update images set status = 'unpublished' where id = %s", ids[0])
    await running(p)
    assert await one(p, T0) is not None
    assert sql(
        clean_db,
        "select count(*) from candidate_edges where layer = 'need' and (src = %s or dst = %s)",
        ids[0],
        ids[0],
    ) == [(0,)]
    async with p.connection() as conn:
        loaded = await store.load_satisfaction(conn, model=MODEL, dim=DIM)
        phrases = await store.load_phrases(conn, model=MODEL, dim=DIM)
    assert all(x.image != ids[0] for x in phrases.needs + phrases.offers)
    assert len(loaded) == len(ids) - 1


async def test_a_satisfied_need_pulls_less(p: Any, clean_db: str) -> None:
    ids, needs = phrased(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    # The tick has fed some needs already; start the comparison from unsatisfied.
    sql(clean_db, "update offers_needs set satisfaction = 0")
    async with p.connection() as conn:
        before = {
            (n["slug"], n["phrase"]): n
            for n in await store.needs_listing(conn, model=MODEL, dim=DIM)
        }
    target = next(k for k, n in before.items() if n["edges"])
    need_id = needs[ids[int(target[1].split()[1])]]
    sql(clean_db, "update offers_needs set satisfaction = 1 where id = %s", need_id)
    async with p.connection() as conn:
        after = {
            (n["slug"], n["phrase"]): n
            for n in await store.needs_listing(conn, model=MODEL, dim=DIM)
        }
    assert after[target]["satisfaction"] == 1.0
    assert after[target]["pull"] == pytest.approx(before[target]["pull"] * (1 - DEFAULTS.need_damp))
    assert after[target]["pull"] < before[target]["pull"]
    edge = after[target]["edges"][0]
    assert set(edge) == {"to", "offer", "affinity", "strength"}
    assert edge["offer"].startswith("offer ")


async def test_reset_and_genesis_start_needs_from_zero(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CENTERED_MIN_IMAGES", "4")
    get_settings.cache_clear()
    phrased(clean_db)
    sql(clean_db, "update offers_needs set satisfaction = 0.5 where role = 'need'")
    # `add` publishes picture n at T0 - 30 days + n minutes: a replay of a few ticks.
    now = T0 - timedelta(days=30) + timedelta(minutes=8) + 2 * TICK
    async with p.connection() as conn:
        done = await genesis.run(conn, now=now, settings=get_settings())
    assert done.last_tick >= 1
    assert sql(clean_db, "select max(satisfaction) from offers_needs") == [(0.0,)]
    # Today's need candidates are written, with no state and no simulated past.
    assert sql(clean_db, "select count(*) > 0 from candidate_edges where layer = 'need'") == [
        (True,)
    ]
    assert sql(clean_db, "select count(*) from edge_state where layer = 'need'") == [(0,)]
    assert all(NEED not in s for (s,) in sql(clean_db, "select stats from mycelium_ticks"))
    sql(clean_db, "update offers_needs set satisfaction = 0.5 where role = 'need'")
    async with p.connection() as conn:
        assert await store.reset(conn) == "full"
    assert sql(clean_db, "select max(satisfaction) from offers_needs") == [(0.0,)]


async def test_a_public_reset_also_starts_needs_from_zero(p: Any, clean_db: str) -> None:
    # Satisfaction feeds the state hash; a public reset that kept it would hash the old
    # network's needs on its first tick.
    phrased(clean_db)
    sql(clean_db, "update offers_needs set satisfaction = 0.5 where role = 'need'")
    async with p.connection() as conn:
        await store.save_state(conn, public_since=T0)
    async with p.connection() as conn:
        assert await store.reset(conn) == "public"
    assert sql(clean_db, "select max(satisfaction) from offers_needs") == [(0.0,)]


async def test_needs_listing_drops_pictures_unpublished_since_the_last_tick(
    p: Any, clean_db: str
) -> None:
    phrased(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    async with p.connection() as conn:
        before = await store.needs_listing(conn, model=MODEL, dim=DIM)
    # A picture some need reaches, unpublished with no tick since: its candidate need
    # edges are still stored, at both ends.
    gone = next(e["to"] for n in before for e in n["edges"])
    assert any(n["slug"] == gone for n in before)
    gone_id = sql(clean_db, "select id from images where slug = %s", gone)[0][0]
    sql(clean_db, "update images set status = 'unpublished' where id = %s", gone_id)
    assert sql(
        clean_db,
        "select count(*) > 0 from candidate_edges where layer = 'need' and dst = %s",
        gone_id,
    ) == [(True,)]
    async with p.connection() as conn:
        after = await store.needs_listing(conn, model=MODEL, dim=DIM)
    assert after
    assert all(e["to"] != gone for n in after for e in n["edges"])
    assert all(n["slug"] != gone for n in after)
