"""Colonies over scripted tick sequences. Pure: no database."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from penish.mycelium import colonies as col
from penish.mycelium.colonies import ColoniesState, Edge, Event
from penish.mycelium.tunables import DEFAULTS, Tunables, with_overrides

T = with_overrides(
    DEFAULTS,
    {
        "fruit_ticks": 4,
        "grace_ticks": 5,
        "admit_ticks": 3,
        "min_size": 3,
        "max_size": 8,
        "remerge_size": 6,
        "fruit_cap": 3,
    },
)
ALL = frozenset(range(1, 100))


def chain(*ids: int, s: float = 0.5) -> list[Edge]:
    return [(a, b, s) for a, b in zip(ids, ids[1:], strict=False)]


Layers = Mapping[str, Sequence[Edge]] | Sequence[Edge]


class Run:
    def __init__(self, t: Tunables = T) -> None:
        self.t = t
        self.state = ColoniesState()
        self.tick = -1
        self.events: list[Event] = []

    def go(self, layers: Layers, *, n: int = 1, live: frozenset[int] = ALL) -> list[Event]:
        edges = layers if isinstance(layers, Mapping) else {"whole": layers}
        out: list[Event] = []
        for _ in range(n):
            self.tick += 1
            self.state, ev = col.step(self.state, self.tick, edges, live, self.t)
            out.extend(ev)
        self.events.extend(out)
        return out

    def colony(self, cid: int) -> col.Colony:
        return next(c for c in self.state.colonies if c.id == cid)


def brief(events: Sequence[Event]) -> list[tuple[int, str, int, int | None, int | None]]:
    return [(e.tick, e.kind, e.colony, e.image, e.other) for e in events]


def fruited_run(*ids: int, t: Tunables = T) -> Run:
    r = Run(t)
    r.go(chain(*ids), n=t.fruit_ticks + 1)
    assert r.colony(1).state == col.ALIVE
    return r


def test_groups_single_linkage_with_cap() -> None:
    edges = chain(1, 2, 3, 4, 5, s=0.9) + chain(6, 7, 8, 9, 10, s=0.8) + [(5, 6, 0.31)]
    assert col.groups(edges, ALL, T) == [frozenset({1, 2, 3, 4, 5}), frozenset({6, 7, 8, 9, 10})]
    small = with_overrides(T, {"max_size": 4, "remerge_size": 4})
    # 1..5 is cut at four by strength order; 5 is left alone and is no group.
    out = col.groups(chain(1, 2, 3, 4, 5, s=0.9), ALL, small)
    assert out == [frozenset({1, 2, 3, 4})]
    assert col.groups(chain(1, 2, 3), ALL - {2}, T) == []


def test_forming_then_fruiting_after_fruit_ticks() -> None:
    r = Run()
    assert brief(r.go(chain(1, 2, 3))) == [(0, "formed", 1, None, None)]
    assert r.go(chain(1, 2, 3), n=T.fruit_ticks - 1) == []
    assert brief(r.go(chain(1, 2, 3))) == [(4, "fruited", 1, None, None)]
    c = r.colony(1)
    assert (c.state, c.serial, c.fruited, c.founders, c.peak) == (col.ALIVE, 1, 4, 3, 3)
    assert all(m.founder for m in c.members)
    assert r.state.next_serial == 2


def test_failing_to_fruit() -> None:
    r = Run()
    r.go(chain(1, 2, 3), n=2)
    assert brief(r.go([])) == [(2, "failed", 1, None, None)]
    c = r.colony(1)
    assert (c.state, c.serial, c.died) == (col.FAILED, None, 2)


def test_two_living_colonies_merge_one_absorbed() -> None:
    r = Run()
    r.go(chain(1, 2, 3) + chain(4, 5, 6), n=T.fruit_ticks + 1)
    assert [r.colony(1).serial, r.colony(2).serial] == [1, 2]
    merged = chain(1, 2, 3, 4, 5, 6)
    # Both match at 0.5 with three in common; the lower colony id keeps the group.
    assert brief(r.go(merged)) == [(5, "absorbed", 2, None, 1)]
    assert r.colony(2).absorbed_by == 1
    assert r.colony(2).cause == "absorbed"
    assert r.go(merged) == []
    assert brief(r.go(merged)) == [(7, "joined", 1, i, None) for i in (4, 5, 6)]
    assert r.colony(1).images() == {1, 2, 3, 4, 5, 6}


def test_split_dies_as_schism_at_grace_expiry() -> None:
    t = with_overrides(T, {"grace_ticks": 2})
    r = fruited_run(*range(1, 9), t=t)
    split = chain(1, 2, 3) + chain(4, 5, 6)
    assert brief(r.go(split)) == [
        (5, "faded", 1, None, None),
        (5, "formed", 2, None, 1),
        (5, "formed", 3, None, 1),
    ]
    assert r.go(split) == []
    assert brief(r.go(split)) == [(7, "schism", 1, None, None)]
    c = r.colony(1)
    assert (c.state, c.cause, c.died) == (col.DEAD, "schism", 7)
    # Frozen while fading: nobody left.
    assert c.images() == set(range(1, 9))


def test_unreconnected_gallery_starves() -> None:
    t = with_overrides(T, {"grace_ticks": 2})
    r = fruited_run(*range(1, 9), t=t)
    assert brief(r.go([])) == [(5, "faded", 1, None, None)]
    assert r.go([]) == []
    assert brief(r.go([])) == [(7, "starved", 1, None, None)]


def test_one_sided_split_starves_not_schism() -> None:
    t = with_overrides(T, {"grace_ticks": 2})
    r = fruited_run(*range(1, 9), t=t)
    # One group holds two, the rest scattered: no schism.
    edges = chain(1, 2, 20)
    r.go(edges, n=2)
    assert [e.kind for e in r.go(edges) if e.colony == 1] == ["starved"]


def test_fading_dies_when_a_sibling_fruits() -> None:
    r = fruited_run(*range(1, 9))
    split = chain(1, 2, 3) + chain(4, 5, 6)
    r.go(split, n=T.fruit_ticks)
    assert brief(r.go(split)) == [
        (9, "fruited", 2, None, None),
        (9, "fruited", 3, None, None),
        (9, "schism", 1, None, None),
    ]
    assert r.colony(2).parent == 1


def test_fading_gallery_revives() -> None:
    r = fruited_run(1, 2, 3)
    r.go([], n=2)
    assert r.colony(1).state == col.FADING
    assert brief(r.go(chain(1, 2, 3, 4))) == [
        (7, "revived", 1, None, None),
        (7, "joined", 1, 4, None),
    ]
    assert r.colony(1).faded is None


def test_drift_below_drift_keep_of_founders() -> None:
    r = fruited_run(1, 2, 3)
    r.go(chain(1, 2, 3, 4, 5, 6), n=3)
    assert r.colony(1).images() == {1, 2, 3, 4, 5, 6}
    r.go(chain(3, 4, 5, 6), n=3)
    # One founder of three is exactly a third, which is not below 0.3333.
    assert r.colony(1).images() == {3, 4, 5, 6}
    assert r.colony(1).state == col.ALIVE
    ev = r.go(chain(4, 5, 6, 7), n=3)
    assert brief(ev) == [
        (13, "left", 1, 3, None),
        (13, "joined", 1, 7, None),
        (13, "drifted", 1, None, 2),
        (13, "formed", 2, None, 1),
    ]
    child = r.colony(2)
    assert (child.state, child.parent, child.images()) == (col.FORMING, 1, {4, 5, 6, 7})
    assert r.colony(1).cause == "drifted"


def test_depopulated_when_members_stop_being_live() -> None:
    r = fruited_run(1, 2, 3)
    ev = r.go(chain(1, 2, 3), live=ALL - {3})
    assert brief(ev) == [(5, "left", 1, 3, None), (5, "depopulated", 1, None, None)]
    assert r.colony(1).cause == "depopulated"


def test_admission_waits_admit_ticks_to_admit_and_release() -> None:
    r = fruited_run(1, 2, 3)
    grown = chain(1, 2, 3, 4)
    assert r.go(grown, n=T.admit_ticks - 1) == []
    assert brief(r.go(grown)) == [(7, "joined", 1, 4, None)]
    assert r.colony(1).members[-1].joined == 7
    assert not r.colony(1).members[-1].founder
    # A difference that flickers does not count.
    base = chain(1, 2, 3)
    r.go(base)
    r.go(grown)
    assert r.go(base, n=T.admit_ticks - 1) == []
    assert brief(r.go(base)) == [(12, "left", 1, 4, None)]


def test_full_gallery_admits_nobody_until_remerge_size() -> None:
    t = with_overrides(T, {"max_size": 6, "remerge_size": 4})
    r = fruited_run(1, 2, 3, 4, 5, 6, t=t)
    assert r.colony(1).full
    ev = r.go(chain(2, 3, 4, 5, 6, 7), n=8)
    assert brief(ev) == [(7, "left", 1, 1, None)]
    assert r.colony(1).full
    assert all(p.kind == "leave" for p in r.colony(1).pending)
    ev = r.go(chain(4, 5, 6, 7), n=5)
    assert brief(ev) == [
        (15, "left", 1, 2, None),
        (15, "left", 1, 3, None),
        (17, "joined", 1, 7, None),
    ]
    assert not r.colony(1).full
    assert r.colony(1).peak == 6


def test_fruit_cap_across_layers_in_rolling_window() -> None:
    t = with_overrides(T, {"fruit_cap": 2})
    r = Run(t)
    layers = {
        "whole": chain(1, 2, 3),
        "facet:subject": chain(4, 5, 6),
        "facet:setting": chain(7, 8, 9),
    }
    assert [(e.kind, e.layer, e.colony) for e in r.go(layers)] == [
        ("formed", "whole", 1),
        ("formed", "facet:subject", 2),
        ("formed", "facet:setting", 3),
    ]
    r.go(layers, n=3)
    assert brief(r.go(layers)) == [(4, "fruited", 1, None, None), (4, "fruited", 2, None, None)]
    waited = r.go(layers, n=143)
    assert waited == []
    assert r.colony(3).state == col.FORMING
    assert brief(r.go(layers)) == [(148, "fruited", 3, None, None)]
    assert r.colony(3).serial == 3


def test_cross_layer_duplicate_blocked_until_gallery_dies() -> None:
    r = Run()
    both = {"whole": chain(1, 2, 3), "facet:subject": chain(1, 2, 3)}
    r.go(both, n=T.fruit_ticks)
    assert brief(r.go(both)) == [(4, "fruited", 1, None, None)]
    alone = {"facet:subject": chain(1, 2, 3)}
    # A fading gallery is still living and still blocks.
    assert brief(r.go(alone)) == [(5, "faded", 1, None, None)]
    assert r.go(alone, n=4) == []
    assert brief(r.go(alone)) == [(10, "starved", 1, None, None)]
    assert brief(r.go(alone)) == [(11, "fruited", 2, None, None)]


def test_cross_layer_overlap_below_duplicate_jaccard_fruits() -> None:
    r = Run()
    both = {"whole": chain(1, 2, 3), "facet:subject": chain(1, 2, 3, 4, 5)}
    r.go(both, n=T.fruit_ticks)
    assert [e.colony for e in r.go(both) if e.kind == "fruited"] == [1, 2]


def script(r: Run) -> None:
    r.go(
        {
            "whole": chain(7, 8, 9) + chain(1, 2, 3),
            "facet:mood": chain(4, 5, 6),
            "facet:subject": chain(10, 11, 12),
        }
    )
    r.go({"whole": chain(1, 2, 3, 7, 8, 9), "facet:mood": chain(4, 5, 6)}, n=6)
    r.go({"whole": chain(1, 2, 3)}, n=4, live=ALL - {9})
    r.go({"whole": chain(2, 3, 4, 5)}, n=8)


def test_ids_and_events_are_deterministic() -> None:
    a, b = Run(), Run()
    script(a)
    script(b)
    assert a.events == b.events
    assert col.state_hash(a.state) == col.state_hash(b.state)
    first = [(e.kind, e.layer, e.colony) for e in a.events if e.tick == 0]
    # Layers in spec order, then groups by lowest member id.
    assert first == [
        ("formed", "whole", 1),
        ("formed", "whole", 2),
        ("formed", "facet:subject", 3),
        ("formed", "facet:mood", 4),
    ]
    # Tied forming colonies: the lower id keeps the group, the other fails.
    assert [(e.tick, e.kind, e.colony) for e in a.events if e.tick == 1] == [
        (1, "failed", 2),
        (1, "failed", 3),
    ]
    # {2, 3, 4, 5} is under match_jaccard but holds a strict majority: engulfed.
    assert a.colony(1).images() == {2, 3, 4, 5}
    assert a.colony(1).state == col.ALIVE
    assert a.colony(4).cause == "starved"
    assert {e.kind for e in a.events} <= set(col.EVENT_KINDS)


def test_step_leaves_prev_untouched_and_round_trips() -> None:
    r = fruited_run(1, 2, 3)
    before = col.state_hash(r.state)
    prev = r.state
    nxt, _ = col.step(prev, r.tick + 1, {"whole": chain(1, 2, 3, 4)}, ALL, T)
    assert col.state_hash(prev) == before
    again = ColoniesState.from_dict(nxt.to_dict())
    assert again == nxt
    assert col.state_hash(again) == col.state_hash(nxt)


def test_founder_who_leaves_and_returns_still_counts() -> None:
    t = with_overrides(T, {"admit_ticks": 1, "max_size": 12, "remerge_size": 10})
    r = fruited_run(*range(1, 10), t=t)
    r.go(chain(*range(1, 10)), live=ALL - {1, 2, 3, 4})
    assert r.colony(1).images() == {5, 6, 7, 8, 9}
    r.go(chain(*range(1, 10)))
    c = r.colony(1)
    assert c.images() == set(range(1, 10))
    assert all(m.founder for m in c.members)
    # Five of the nine pictures at fruiting are still members: not drifted.
    ev = r.go(chain(1, 2, 3, 4, 9, 30, 31, 32))
    assert "drifted" not in [e.kind for e in ev]
    c = r.colony(1)
    assert (c.state, c.images()) == (col.ALIVE, {1, 2, 3, 4, 9, 30, 31, 32})
    assert c.founder_ids == list(range(1, 10))


def test_absorbed_colony_frees_the_group_it_matched() -> None:
    t = with_overrides(T, {"match_jaccard": 0.3})
    r = Run(t)
    r.go(chain(1, 2, 3, 4, 5, 6, 7) + chain(8, 9, 10), n=t.fruit_ticks + 1)
    assert [r.colony(1).state, r.colony(2).state] == [col.ALIVE, col.ALIVE]
    # 1 matches {5, 6, 7} and 2 matches the other group, where 1 holds a majority.
    ev = r.go(chain(5, 6, 7) + chain(1, 2, 3, 4, 8, 9, 10))
    tick = r.tick
    assert brief(ev) == [(tick, "absorbed", 1, None, 2), (tick, "formed", 3, None, None)]
    assert r.colony(3).images() == {5, 6, 7}


def test_survivor_from_another_group_frees_the_one_it_matched() -> None:
    t = with_overrides(T, {"match_jaccard": 0.3, "max_size": 12, "remerge_size": 10})
    r = Run(t)
    r.go(chain(1, 2, 3, 4, 5, 6, 7) + chain(8, 9, 11), n=t.fruit_ticks + 1)
    # 1 matches {5, 6, 7}, then wins the big group on Jaccard and takes it instead.
    ev = r.go(chain(5, 6, 7) + chain(1, 2, 3, 4, 8, 9, 11, 12, 13, 14, 15))
    tick = r.tick
    assert brief(ev) == [(tick, "absorbed", 2, None, 1), (tick, "formed", 3, None, None)]
    assert r.colony(3).images() == {5, 6, 7}
    assert r.colony(1).state == col.ALIVE


def _drifting_pair() -> ColoniesState:
    def gallery(cid: int, members: range, founders: range) -> col.Colony:
        return col.Colony(
            id=cid,
            layer="whole",
            state=col.ALIVE,
            born=0,
            members=[col.Member(i, False, 5) for i in members],
            serial=cid,
            fruited=1,
            founders=len(founders),
            founder_ids=list(founders),
            peak=len(members),
        )

    return ColoniesState(
        colonies=[gallery(1, range(10, 13), range(1, 4)), gallery(2, range(20, 23), range(4, 7))],
        next_id=3,
        next_serial=3,
        tick=9,
    )


def test_colony_list_order_does_not_matter() -> None:
    edges = {"whole": chain(10, 11, 12) + chain(20, 21, 22)}
    ordered = _drifting_pair()
    scrambled = _drifting_pair()
    scrambled.colonies.reverse()
    for c in scrambled.colonies:
        c.members.reverse()
    a, ev_a = col.step(ordered, 10, edges, ALL, T)
    b, ev_b = col.step(scrambled, 10, edges, ALL, T)
    assert brief(ev_a) == [
        (10, "drifted", 1, None, 3),
        (10, "formed", 3, None, 1),
        (10, "drifted", 2, None, 4),
        (10, "formed", 4, None, 2),
    ]
    assert ev_a == ev_b
    assert col.state_hash(a) == col.state_hash(b)
    loaded = ColoniesState.from_dict(scrambled.to_dict())
    assert col.state_hash(loaded) == col.state_hash(ordered)
