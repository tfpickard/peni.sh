"""Facet layers against a real database: schema, means, the active recipe, scoped genesis."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import numpy as np
import psycopg
import pytest

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium import engine, facets, genesis, store, tick
from penish.worker import jobs, metrics
from penish.worker.admin_jobs import make_handlers
from penish.worker.metrics import refresh_corpus_stats, refresh_facet_stats
from tests.test_mycelium_genesis import CENTERED_MIN, LAST, NOW
from tests.test_mycelium_genesis import add as add_dated
from tests.test_mycelium_tick import DIM, FAST, MODEL, T0, TICK, sql
from tests.test_mycelium_tick import add as add_live

MOOD, SUBJECT = "facet:mood", "facet:subject"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EMBED_MODEL", MODEL)
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    monkeypatch.setenv("CENTERED_MIN_IMAGES", str(CENTERED_MIN))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def p(clean_db: str) -> AsyncIterator[Any]:
    pool = make_pool(clean_db)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def fvec(n: int, facet: str, recipe: str) -> list[float]:
    # Each facet clusters the pictures its own way, and the recipes disagree, so the
    # layers and the recipes build different graphs.
    fi = facets.FACETS.index(facet)
    shift = 1 if recipe == facets.SPLIT else 2
    c = (n * shift + fi) % 3
    v = [0.05 * ((n * 5 + fi * 3 + j) % 7) / 7 for j in range(DIM)]
    v[c] += 1.0
    return v


def give(
    dsn: str,
    image_id: int,
    n: int,
    recipe: str = facets.SPLIT,
    which: tuple[str, ...] = facets.FACETS,
    *,
    bodies: bool = True,
) -> None:
    for f in which:
        if bodies:
            sql(
                dsn,
                "insert into image_facets (image_id, facet, recipe, body, model)"
                " values (%s, %s, %s, %s, %s)",
                image_id,
                f,
                recipe,
                f"{f} of {n}",
                None if recipe == facets.SPLIT else "m",
            )
        sql(
            dsn,
            "insert into embeddings (image_id, kind, vec, model, dim)"
            " values (%s, %s, %s::vector, %s, %s)",
            image_id,
            facets.kind(f, recipe),
            str(fvec(n, f, recipe)),
            MODEL,
            DIM,
        )


async def state(p: Any) -> store.State:
    async with p.connection() as conn:
        return await store.read_state(conn)


# -- schema -------------------------------------------------------------------


def test_embedding_kinds_accept_facets_and_reject_junk(clean_db: str) -> None:
    image_id = add_live(clean_db, 1)
    for k in facets.kinds():
        sql(
            clean_db,
            "insert into embeddings (image_id, kind, vec, model, dim)"
            " values (%s, %s, '[1,0,0,0]'::vector, %s, %s)",
            image_id,
            k,
            MODEL,
            DIM,
        )
    for junk in ("facet:mood", "facet:mood/guess", "facet:taste/split", "Facet:mood/split", "x"):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(
                clean_db,
                "insert into embeddings (image_id, kind, vec, model, dim)"
                " values (%s, %s, '[1,0,0,0]'::vector, %s, %s)",
                image_id,
                junk,
                MODEL,
                DIM,
            )


def test_facet_tables_check_and_cascade(clean_db: str) -> None:
    image_id = add_live(clean_db, 1)
    give(clean_db, image_id, 1)
    sql(
        clean_db,
        "insert into image_extracts (image_id, model, body) values (%s, 'm', '{}'::jsonb)",
        image_id,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(
            clean_db,
            "insert into image_facets (image_id, facet, recipe, body)"
            " values (%s, 'taste', 'split', 'x')",
            image_id,
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(
            clean_db,
            "insert into image_facets (image_id, facet, recipe, body)"
            " values (%s, 'mood', 'guess', 'x')",
            image_id,
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(clean_db, "update mycelium_state set facet_recipe = 'guess'")
    assert sql(clean_db, "select facet_recipe from mycelium_state") == [("split",)]
    sql(clean_db, "delete from images where id = %s", image_id)
    for table in ("image_facets", "image_extracts", "embeddings"):
        assert sql(clean_db, f"select count(*) from {table}") == [(0,)]


# -- means --------------------------------------------------------------------


async def test_description_refresh_carries_the_facet_means(p: Any, clean_db: str) -> None:
    ids = [add_live(clean_db, n) for n in range(1, 7)]
    for n, image_id in enumerate(ids, 1):
        give(clean_db, image_id, n, which=("mood",) if n < 6 else facets.FACETS)
    hidden = add_live(clean_db, 7, status="queued")
    give(clean_db, hidden, 7)
    sql(clean_db, "update mycelium_state set graph_dirty = false")
    await refresh_corpus_stats(p, kind="description", model=MODEL)

    rows = sql(
        clean_db,
        "select kind, n, temperature, mean::real[] from corpus_stats where kind like 'facet:%%'"
        " order by kind",
    )
    assert [r[0] for r in rows] == sorted(facets.kinds(facets.SPLIT))
    by_kind = {r[0]: r for r in rows}
    mood = by_kind["facet:mood/split"]
    assert mood[1] == 6 and mood[2] is None
    want = np.mean([fvec(n, "mood", facets.SPLIT) for n in range(1, 7)], axis=0)
    assert np.allclose(mood[3], want, atol=1e-6)
    assert by_kind["facet:subject/split"][1] == 1
    # Only the description is charted, so only it has history.
    assert sql(clean_db, "select kind from corpus_stats_history") == [("description",)]
    assert sql(clean_db, "select graph_dirty from mycelium_state") == [(True,)]

    # A space whose last vector went away follows to empty rather than keep its old mean.
    sql(clean_db, "delete from embeddings where kind = 'facet:subject/split'")
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    assert sql(clean_db, "select n, mean from corpus_stats where kind = 'facet:subject/split'") == [
        (0, None)
    ]

    sql(clean_db, "update mycelium_state set graph_dirty = false")
    await refresh_facet_stats(p, model=MODEL, recipe=facets.EXTRACT)
    assert sql(clean_db, "select graph_dirty from mycelium_state") == [(True,)]
    assert sql(
        clean_db, "select count(*) from corpus_stats where kind like 'facet:%%/extract'"
    ) == [(4,)]


async def test_both_refreshers_lock_facet_rows_in_one_order(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each upsert holds its corpus_stats row to commit; a description refresh and a
    # facet job's refresh running at once deadlock unless they walk the rows alike.
    image_id = add_live(clean_db, 1)
    give(clean_db, image_id, 1)
    give(clean_db, image_id, 1, facets.EXTRACT)
    seen: list[str] = []
    real = metrics._refresh_mean

    async def spy(conn: Any, *, kind: str, model: str) -> None:
        seen.append(kind)
        await real(conn, kind=kind, model=model)

    monkeypatch.setattr(metrics, "_refresh_mean", spy)
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    by_description = list(seen)
    seen.clear()
    await refresh_facet_stats(p, model=MODEL)
    assert seen == by_description == sorted(facets.kinds())
    seen.clear()
    await refresh_facet_stats(p, model=MODEL, recipe=facets.SPLIT)
    assert seen == sorted(facets.kinds(facets.SPLIT))


# -- the live tick ------------------------------------------------------------


async def ticked(p: Any, at: Any) -> tick.Ran:
    async with p.connection() as conn:
        ran = await tick.run_one(conn, now=at)
    assert ran is not None
    return ran


async def live_rebuild(p: Any, recipe: str) -> dict[str, list[Any]]:
    cfg = get_settings()
    async with p.connection() as conn:
        t = await store.load_tunables(conn)
        inputs = await store.load_facet_inputs(conn, recipe=recipe, model=MODEL, dim=DIM)
    return tick.build_facet_graphs(inputs, t, centered_min=cfg.centered_min_images)


async def test_the_tick_grows_facet_layers_on_the_active_recipe(p: Any, clean_db: str) -> None:
    ids = [add_live(clean_db, n) for n in range(1, 13)]
    for n, image_id in enumerate(ids, 1):
        give(clean_db, image_id, n)
        # Extract vectors for everyone, but bodies for all but the last.
        give(clean_db, image_id, n, facets.EXTRACT, bodies=n < 12)
    # One picture has no mood vector under split: absent from that layer only.
    sql(
        clean_db,
        "delete from embeddings where image_id = %s and kind = 'facet:mood/split'",
        ids[0],
    )
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    async with p.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
        await store.save_state(conn, running=True, next_due=T0)

    await ticked(p, T0)
    stored = await _stored(p)
    split = await live_rebuild(p, facets.SPLIT)
    for name in facets.LAYERS:
        assert stored.get(name, []) == split[name]
        assert stored[name], name
    assert not any(ids[0] in (e.src, e.dst) for e in stored[MOOD])
    assert any(ids[0] in (e.src, e.dst) for e in stored[SUBJECT])
    stats = sql(clean_db, "select stats from mycelium_ticks where tick = 0")[0][0]
    assert set(stats) == {"whole", *facets.LAYERS}

    async with p.connection() as conn:
        counts = await store.facet_counts(conn, model=MODEL, dim=DIM)
        assert counts == {"split": 11, "extract": 11, "live": 12}
        # Not every live picture has all four extract facets with vectors.
        with pytest.raises(store.FacetSourceRefused, match="not every live picture has extract"):
            await store.set_facet_recipe(conn, facets.EXTRACT, model=MODEL, dim=DIM)
    assert (await state(p)).facet_recipe == facets.SPLIT
    assert not (await state(p)).graph_dirty

    sql(
        clean_db,
        "insert into image_facets (image_id, facet, recipe, body)"
        " select %s, f, 'extract', 'x' from unnest(%s::text[]) f",
        ids[-1],
        list(facets.FACETS),
    )
    async with p.connection() as conn:
        st = await store.set_facet_recipe(conn, facets.EXTRACT, model=MODEL, dim=DIM)
    assert st.facet_recipe == facets.EXTRACT and st.graph_dirty

    ran = await ticked(p, T0 + TICK)
    assert ran.rebuilt
    stored = await _stored(p)
    extract = await live_rebuild(p, facets.EXTRACT)
    for name in facets.LAYERS:
        assert stored.get(name, []) == extract[name]
    assert stored[MOOD] != split[MOOD]
    # The whole layer is untouched by the switch.
    assert stored["whole"]


async def _stored(p: Any) -> dict[str, list[Any]]:
    async with p.connection() as conn:
        return await store.load_candidate_edges(conn)


# -- genesis ------------------------------------------------------------------


async def dated_corpus(p: Any, dsn: str, n: int = 12) -> list[int]:
    ids = [add_dated(dsn, i) for i in range(1, n + 1)]
    for i, image_id in enumerate(ids, 1):
        give(dsn, image_id, i)
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    async with p.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
    return ids


async def full(p: Any) -> genesis.Genesis:
    async with p.connection() as conn:
        return await genesis.run(conn, now=NOW)


async def scoped(p: Any, layers: list[str]) -> genesis.Genesis:
    async with p.connection() as conn:
        return await genesis.run(conn, now=NOW, layers=layers)


def edge_rows(dsn: str, layer: str) -> list[tuple[Any, ...]]:
    return sql(dsn, "select * from edge_state where layer = %s order by src, dst", layer)


async def test_full_genesis_replays_every_active_layer(p: Any, clean_db: str) -> None:
    await dated_corpus(p, clean_db)
    done = await full(p)
    stored = await _stored(p)
    assert stored == {name: edges for name, edges in (await _live_all(p)).items() if edges}
    assert set(stored) == set(tick.ACTIVE_LAYERS)
    stats = sql(clean_db, "select stats from mycelium_ticks where tick = %s", LAST)[0][0]
    assert set(stats) == set(tick.ACTIVE_LAYERS)
    async with p.connection() as conn:
        states = await store.load_edge_states(conn)
    assert engine.state_hash(states) == done.state_hash
    assert set(states) == set(tick.ACTIVE_LAYERS)


async def _live_all(p: Any) -> dict[str, list[Any]]:
    cfg = get_settings()
    async with p.connection() as conn:
        t = await store.load_tunables(conn)
        whole = await store.load_graph_inputs(conn, model=MODEL, dim=DIM)
        fin = await store.load_facet_inputs(conn, recipe=facets.SPLIT, model=MODEL, dim=DIM)
    return tick.build_graphs(whole, fin, t, centered_min=cfg.centered_min_images)


async def test_scoped_genesis_replays_one_layer_and_holds_the_rest(p: Any, clean_db: str) -> None:
    await dated_corpus(p, clean_db)
    await full(p)
    mood_after_full = edge_rows(clean_db, MOOD)
    others = {n: edge_rows(clean_db, n) for n in tick.ACTIVE_LAYERS if n != MOOD}
    hashes = sql(clean_db, "select tick, state_hash, colonies_hash from mycelium_ticks")
    before = await state(p)
    held_colonies = sql(clean_db, "select * from colonies where layer <> %s order by id", MOOD)
    # Galleries are public now; only the whole-network genesis refuses that.
    async with p.connection() as conn:
        await store.save_state(conn, public_since=NOW)
    with pytest.raises(genesis.GenesisRefused, match="public"):
        await full(p)

    # Forget the mood layer, as a recipe switch would leave it.
    sql(clean_db, "delete from edge_state where layer = %s", MOOD)
    sql(clean_db, "update colonies set serial = null where layer = %s", MOOD)
    st = (await state(p)).colonies_state()
    for c in st.colonies:
        if c.layer == MOOD:
            c.serial = None
    async with p.connection() as conn:
        await store.save_state(conn, colonies=st.to_dict())

    done = await scoped(p, [MOOD])
    after = await state(p)
    assert done.last_tick == LAST and done.next_due == before.next_due
    assert (after.last_tick, after.next_due, after.simulated_through) == (
        before.last_tick,
        before.next_due,
        before.simulated_through,
    )
    assert after.graph_version == before.graph_version + 1

    # Each layer draws from its own stream, so the replayed layer grows exactly as it
    # did in the full run, and nothing else moved.
    assert edge_rows(clean_db, MOOD) == mood_after_full
    assert {n: edge_rows(clean_db, n) for n in others} == others
    assert sql(clean_db, "select tick, state_hash, colonies_hash from mycelium_ticks") == hashes
    assert sql(clean_db, "select * from colonies where layer <> %s order by id", MOOD) == (
        held_colonies
    )
    stored = await _stored(p)
    assert stored[MOOD] == (await live_rebuild(p, facets.SPLIT))[MOOD]
    stats = sql(clean_db, "select stats -> 'facet:mood' from mycelium_ticks where tick = 0")
    assert stats[0][0] is not None

    # Colonies in the replayed layer are new and simulated; held ids were not reused.
    final = after.colonies_state()
    mood_ids = [c.id for c in final.colonies if c.layer == MOOD]
    held_ids = [c.id for c in final.colonies if c.layer != MOOD]
    assert not set(mood_ids) & set(held_ids)
    assert all(c.layer != MOOD or c.born <= LAST for c in final.colonies)
    rows = sql(clean_db, "select simulated from colonies where layer = %s", MOOD)
    assert all(sim for (sim,) in rows)


async def test_scoped_genesis_refusals(p: Any, clean_db: str) -> None:
    await dated_corpus(p, clean_db, 8)
    with pytest.raises(genesis.GenesisRefused, match="nothing to replay"):
        await scoped(p, [MOOD])
    await full(p)
    with pytest.raises(genesis.GenesisRefused, match="whole replays with every layer"):
        await scoped(p, ["whole", MOOD])
    sql(
        clean_db,
        "insert into colonies (id, layer, state, serial, born_tick)"
        " values (9999, %s, 'dead', 99, 0)",
        MOOD,
    )
    with pytest.raises(genesis.GenesisRefused, match="facet:mood has galleries"):
        await scoped(p, [SUBJECT, MOOD])
    async with p.connection() as conn:
        await store.save_state(conn, running=True)
    with pytest.raises(genesis.GenesisRefused, match="running"):
        await scoped(p, [SUBJECT])


async def test_the_job_takes_layers(p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await dated_corpus(p, clean_db, 8)
    real = genesis.plan

    def short(*a: Any) -> genesis.Plan:
        return real(*a[:3], NOW)

    monkeypatch.setattr(genesis, "plan", short)

    async def run_job(payload: dict[str, Any]) -> tuple[str, str | None]:
        job_id = await jobs.enqueue(p, genesis.JOB, payload, dedupe_key=genesis.JOB)
        assert job_id is not None
        handlers = make_handlers(p, get_settings(), embedder=None)  # type: ignore[arg-type]
        job = await jobs.claim(p, [genesis.JOB])
        assert job is not None
        await jobs._run_one(p, job, handlers[genesis.JOB])
        row = sql(clean_db, "select status::text, last_error from jobs where id = %s", job_id)
        return row[0][0], row[0][1]

    assert await run_job({}) == ("done", None)
    assert await run_job({"layers": [MOOD]}) == ("done", None)
    status, error = await run_job({"layers": "facet:mood"})
    assert status == "failed"
    assert error is not None and "layers must be a list of names" in error
