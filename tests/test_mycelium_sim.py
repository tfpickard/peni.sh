"""Pure smoke tests for the offline harness. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from penish.mycelium import sim
from penish.mycelium.tunables import DEFAULTS, TICK_SECONDS, with_overrides


def test_synthetic_run_is_deterministic_and_sane() -> None:
    corpus = sim.synthetic_corpus(60, 3, ["whole"])
    a = sim.run(corpus, 200, 3)
    b = sim.run(sim.synthetic_corpus(60, 3, ["whole"]), 200, 3)
    assert a.engine_hash == b.engine_hash
    assert a.colonies_hash == b.colonies_hash
    assert 0.0 <= a.share_end <= 1.0
    assert 0.0 <= a.share_mean <= 1.0
    assert a.entered == 60
    counts = a.edge_counts["whole"]
    # Thresholds are nested, so the counts must be too.
    assert counts["state"] >= counts["wither_min"] >= counts["hypha_visible"]
    assert counts["hypha_visible"] >= counts["colony_leave"] >= counts["colony_enter"]
    assert counts["latched"] > 0
    assert 0.0 < a.mean_strength["whole"] <= 1.0


def test_seed_changes_the_run() -> None:
    corpus = sim.synthetic_corpus(60, 3, ["whole"])
    assert sim.run(corpus, 50, 3).engine_hash != sim.run(corpus, 50, 4).engine_hash


def test_galleries_fruit_with_short_holds() -> None:
    t = with_overrides(DEFAULTS, {"fruit_ticks": 10, "admit_ticks": 2})
    s = sim.run(sim.synthetic_corpus(60, 1, ["whole"]), 200, 1, t)
    assert s.alive + s.died > 0
    assert 3 <= s.largest <= t.max_size
    assert s.share_end > 0.0


def test_another_layer_leaves_whole_alone() -> None:
    one = sim.run(sim.synthetic_corpus(40, 2, ["whole"]), 60, 2, layers=["whole"])
    two = sim.run(
        sim.synthetic_corpus(40, 2, ["whole", "facet:mood"]),
        60,
        2,
        layers=["facet:mood", "whole"],
    )
    assert two.layers == ("whole", "facet:mood")
    assert one.edge_counts["whole"] == two.edge_counts["whole"]
    assert one.mean_strength["whole"] == two.mean_strength["whole"]


def test_entry_ticks_start_at_min_size() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    published = {
        5: t0,
        2: t0 + timedelta(hours=1),
        9: t0 + timedelta(hours=2),
        4: t0 + timedelta(hours=2, seconds=TICK_SECONDS * 3 + 1),
    }
    start, entry = sim.entry_ticks(published, 3)
    assert start == t0 + timedelta(hours=2)
    assert entry == {5: 0, 2: 0, 9: 0, 4: 3}
    assert sim.entry_ticks({}, 3) == (None, {})


def test_run_with_entry_grows_the_live_set() -> None:
    corpus = sim.synthetic_corpus(30, 5, ["whole"])
    entry = {pid: (0 if pid <= 10 else 40) for pid in corpus.ids}
    early = sim.run(corpus, 30, 5, entry=entry)
    late = sim.run(corpus, 60, 5, entry=entry)
    assert early.entered == 10
    assert late.entered == 30


def test_cli_prints_the_metrics(capsys: pytest.CaptureFixture[str]) -> None:
    assert sim.main(["--synthetic", "40", "--ticks", "160", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    for label in (
        "in a gallery",
        "by day",
        "galleries  died",
        "largest gallery",
        "deaths",
        "membership changes per day",
        "edges whole",
    ):
        assert label in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--synthetic", "10", "--set", "nope=1"],
        ["--synthetic", "10", "--set", "candidates=1"],
        ["--synthetic", "10", "--from-publish-dates"],
        ["--synthetic", "10", "--layers", "whole,elsewhere"],
    ],
)
def test_cli_rejects(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        sim.main(argv)
