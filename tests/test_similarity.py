"""Similarity and metrics against numpy references on synthetic 8-dim vectors."""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pytest

from penish import similarity
from penish.similarity import image_metrics, neighbors
from penish.worker import metrics as wm
from penish.worker.metrics import refresh_corpus_stats, surprise_at_arrival

KIND = "description"
MODEL = "test"
DIM = 8

_seq = itertools.count()


async def _add(
    pool: Any,
    vec: np.ndarray,
    *,
    status: str = "live",
    model: str = MODEL,
    surprise: float | None = None,
) -> int:
    i = next(_seq)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, slug, status, surprise)"
            " values (%s, %s, %s, %s) returning id",
            (f"sha-{i:08d}", f"img-{i}", status, surprise),
        )
        row = await cur.fetchone()
        image_id = int(row["id"])
        await conn.execute(
            "insert into embeddings (image_id, kind, vec, model, dim) values (%s, %s, %s, %s, %s)",
            (image_id, KIND, np.asarray(vec, dtype=np.float32), model, len(vec)),
        )
    return image_id


def _cos_dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(1 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _f32(v: np.ndarray) -> np.ndarray:
    # Postgres stores float4, so references must start from the same rounded values.
    return np.asarray(v, dtype=np.float32).astype(np.float64)


def _unit(axis: int, scale: float = 1.0) -> np.ndarray:
    v = np.zeros(DIM)
    v[axis] = scale
    return v


def _anisotropic_corpus() -> tuple[list[np.ndarray], int, int]:
    """40 cluster vectors plus one planted vector; returns (vecs, probe idx, planted idx).

    Every vector carries c = 10*e0, which dominates raw cosine. The cluster sits
    at +1 along e1, with symmetric +-m noise on e2..e7 (m in 2..5) so the noise
    cancels in the mean. The planted vector sits at -2 along e1 with no noise.

    Mean = c + (38/41) e1. Probe = c + e1 (no noise), so probe - mean = +0.073 e1
    and planted - mean = -2.93 e1: centered cosine is exactly -1, while every
    cluster member has centered cosine > 0 with the probe.

    Raw: cos(probe, planted) = 98 / sqrt(101 * 104) = 0.956, but a cluster member
    with m = 5 gives 101 / sqrt(101 * 126) = 0.895, so raw ranks noisy cluster
    members as more dissimilar than the planted vector.
    """
    c = _unit(0, 10.0)
    vecs = [c + _unit(1), c + _unit(1)]  # probe, plus a noiseless twin
    for j in range(38):
        axis = 2 + (j // 2) % 6
        sign = 1.0 if j % 2 == 0 else -1.0
        mag = 2.0 + j // 12
        vecs.append(c + _unit(1) + _unit(axis, sign * mag))
    vecs.append(c + _unit(1, -2.0))
    return vecs, 0, len(vecs) - 1


async def test_centered_finds_planted_antipode_raw_does_not(pool: Any) -> None:
    vecs, probe_i, planted_i = _anisotropic_corpus()
    ids = [await _add(pool, v) for v in vecs]
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)
    probe, planted = ids[probe_i], ids[planted_i]

    raw = await neighbors(pool, probe, mode="raw", k=5)
    assert raw.mode == "raw"
    assert planted not in [n.image_id for n in raw.dissimilar]

    cen = await neighbors(pool, probe, mode="centered", k=5)
    assert cen.mode == "centered"
    assert cen.dissimilar[0].image_id == planted
    assert cen.dissimilar[0].score == pytest.approx(-1.0, abs=1e-4)

    # 41 live images is past the threshold, so auto picks centered.
    auto = await neighbors(pool, probe, k=5)
    assert auto.mode == "centered"
    assert auto.dissimilar[0].image_id == planted

    # Scores agree with numpy for both modes.
    f = [_f32(v) for v in vecs]
    mu = np.mean(f, axis=0)
    for n in raw.similar + raw.dissimilar:
        j = ids.index(n.image_id)
        assert n.score == pytest.approx(1 - _cos_dist(f[j], f[probe_i]), abs=1e-5)
    for n in cen.similar + cen.dissimilar:
        j = ids.index(n.image_id)
        assert n.score == pytest.approx(1 - _cos_dist(f[j] - mu, f[probe_i] - mu), abs=1e-4)


async def test_auto_uses_raw_below_threshold(pool: Any) -> None:
    rng = np.random.default_rng(1)
    ids = [await _add(pool, rng.normal(size=DIM)) for _ in range(5)]
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)
    assert (await neighbors(pool, ids[0])).mode == "raw"
    assert (await neighbors(pool, ids[0], mode="centered")).mode == "centered"


async def test_centered_falls_back_to_raw_for_single_image(pool: Any) -> None:
    only = await _add(pool, np.ones(DIM))
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)
    res = await neighbors(pool, only, mode="centered")
    assert res.mode == "raw"
    assert res.similar == [] and res.dissimilar == []


async def test_exclusions(pool: Any) -> None:
    rng = np.random.default_rng(2)
    src = await _add(pool, rng.normal(size=DIM))
    live = [await _add(pool, rng.normal(size=DIM)) for _ in range(3)]
    queued = await _add(pool, rng.normal(size=DIM), status="queued")
    failed = await _add(pool, rng.normal(size=DIM), status="failed")
    other_model = await _add(pool, rng.normal(size=DIM), model="other")
    # Different width under another model must not break the scan either.
    await _add(pool, rng.normal(size=4), model="narrow")

    res = await neighbors(pool, src, k=10)
    got = {n.image_id for n in res.similar}
    assert got == set(live)
    assert {n.image_id for n in res.dissimilar} == set(live)
    assert src not in got and queued not in got and failed not in got
    assert other_model not in got
    assert all(n.slug and n.slug.startswith("img-") for n in res.similar)
    scores = [n.score for n in res.similar]
    assert scores == sorted(scores, reverse=True)


async def test_refresh_corpus_stats_matches_numpy(pool: Any) -> None:
    rng = np.random.default_rng(3)
    raw = rng.normal(size=(20, DIM)) + 0.5
    for v in raw:
        await _add(pool, v)
    await _add(pool, rng.normal(size=DIM), status="queued")
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)

    f = _f32(raw)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select mean, n, temperature from corpus_stats where kind = %s and model = %s",
            (KIND, MODEL),
        )
        row = await cur.fetchone()
    assert row["n"] == 20
    np.testing.assert_allclose(row["mean"].to_numpy(), f.mean(axis=0), atol=1e-5)
    ref = np.mean([_cos_dist(f[a], f[b]) for a, b in itertools.combinations(range(20), 2)])
    assert row["temperature"] == pytest.approx(ref, abs=1e-5)


async def test_refresh_corpus_stats_sampled_temperature(
    pool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wm, "EXACT_TEMPERATURE_MAX_N", 10)
    rng = np.random.default_rng(4)
    raw = rng.normal(size=(60, DIM))
    for v in raw:
        await _add(pool, v)
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)
    async with pool.connection() as conn:
        cur = await conn.execute("select temperature from corpus_stats")
        row = await cur.fetchone()
    f = _f32(raw)
    ref = np.mean([_cos_dist(f[a], f[b]) for a, b in itertools.combinations(range(60), 2)])
    assert row["temperature"] == pytest.approx(ref, abs=5e-3)


async def test_refresh_corpus_stats_empty(pool: Any) -> None:
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)
    async with pool.connection() as conn:
        cur = await conn.execute("select mean, n, temperature from corpus_stats")
        row = await cur.fetchone()
    assert row == {"mean": None, "n": 0, "temperature": None}


async def test_surprise_at_arrival(pool: Any) -> None:
    a = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=float)
    b = np.array([1, 1, 0, 0, 0, 0, 0, 0], dtype=float)
    first = await _add(pool, a, status="processing")
    assert await surprise_at_arrival(pool, first, kind=KIND, model=MODEL) is None
    async with pool.connection() as conn:
        await conn.execute("update images set status = 'live' where id = %s", (first,))

    second = await _add(pool, b, status="processing")
    s = await surprise_at_arrival(pool, second, kind=KIND, model=MODEL)
    assert s == pytest.approx(_cos_dist(a, b), abs=1e-6)

    with pytest.raises(LookupError):
        await surprise_at_arrival(pool, second, kind=KIND, model="other")


async def test_image_metrics_match_numpy(pool: Any) -> None:
    rng = np.random.default_rng(5)
    raw = rng.normal(size=(9, DIM)) + 0.3
    ids = [await _add(pool, v, surprise=0.123456 if i == 0 else None) for i, v in enumerate(raw)]
    await refresh_corpus_stats(pool, kind=KIND, model=MODEL)

    f = _f32(raw)
    m = await image_metrics(pool, ids[0], k=5)
    dists = sorted(_cos_dist(f[0], f[j]) for j in range(1, 9))
    assert m.loneliness == pytest.approx(np.mean(dists[:5]), abs=6e-4)
    assert m.typicality == pytest.approx(1 - _cos_dist(f[0], f.mean(axis=0)), abs=6e-4)
    assert m.surprise == pytest.approx(0.123, abs=1e-9)
    for v in (m.loneliness, m.typicality, m.surprise):
        assert v is not None and round(v, 3) == v


async def test_image_metrics_single_image(pool: Any) -> None:
    only = await _add(pool, np.ones(DIM))
    m = await image_metrics(pool, only)
    assert m == similarity.Metrics(loneliness=None, typicality=None, surprise=None)
