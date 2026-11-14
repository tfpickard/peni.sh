"""Genesis against a real database: a replay from publish dates, all or nothing."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium import genesis, store, tick
from penish.mycelium.tunables import TICK_SECONDS
from penish.worker import jobs
from penish.worker.admin_jobs import make_handlers
from penish.worker.metrics import refresh_corpus_stats
from tests.test_mycelium_tick import DIM, FAST, MODEL, sha, snapshot, sql, vec

TICK = timedelta(seconds=TICK_SECONDS)
P0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CENTERED_MIN = 4
# The fourth picture is published three minutes in: that is tick 0.
START = P0 + timedelta(minutes=3)
LAST = 40
NOW = START + LAST * TICK + timedelta(minutes=5)


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


def published(n: int) -> datetime:
    # Four together at the start, then one every 25 minutes: arrivals mid-replay.
    if n <= 4:
        return P0 + timedelta(minutes=n - 1)
    return START + timedelta(minutes=25 * (n - 4))


def add(dsn: str, n: int, *, status: str = "live", with_vec: bool = True) -> int:
    image_id = int(
        sql(
            dsn,
            "insert into images (sha256, pub_sha, slug, status, published_at)"
            " values (%s, %s, %s, %s, %s) returning id",
            sha(n),
            sha(n),
            f"img-{n}",
            status,
            published(n),
        )[0][0]
    )
    if with_vec:
        sql(
            dsn,
            "insert into embeddings (image_id, kind, vec, model, dim)"
            " values (%s, 'description', %s::vector, %s, %s)",
            image_id,
            str(vec(n)),
            MODEL,
            DIM,
        )
    sql(
        dsn,
        "insert into picture_attention (image_id, last_viewed_at) values (%s, %s)",
        image_id,
        P0,
    )
    return image_id


async def corpus(p: Any, dsn: str, n: int = 12) -> list[int]:
    ids = [add(dsn, i) for i in range(1, n + 1)]
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    async with p.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
    return ids


async def run(p: Any, now: datetime = NOW) -> genesis.Genesis:
    async with p.connection() as conn:
        return await genesis.run(conn, now=now)


async def state(p: Any) -> store.State:
    async with p.connection() as conn:
        return await store.read_state(conn)


def tick_rows(dsn: str) -> list[tuple[Any, ...]]:
    return sql(
        dsn,
        "select tick, scheduled_at, simulated, state_hash, colonies_hash,"
        " (stats -> 'whole' ->> 'live')::int from mycelium_ticks order by tick",
    )


# -- the plan ---------------------------------------------------------------


def test_plan_entries() -> None:
    pub = {i: published(i) for i in range(1, 8)}
    pl = genesis.plan(pub, set(pub), CENTERED_MIN, NOW)
    assert pl.start == START
    assert pl.last_tick == LAST
    # Earlier pictures enter together; later ones at the tick whose window holds them.
    assert [pl.entry[i] for i in range(1, 8)] == [0, 0, 0, 0, 2, 5, 7]
    # Pictures without a vector neither count toward the start nor wait for it.
    no_vec = genesis.plan(pub, {1, 2, 3, 5, 6}, CENTERED_MIN, NOW)
    assert no_vec.start == published(5)
    assert no_vec.entry[4] == 0
    with pytest.raises(genesis.GenesisRefused, match="fewer than 4"):
        genesis.plan(pub, {1, 2, 3}, CENTERED_MIN, NOW)
    with pytest.raises(genesis.GenesisRefused, match="nothing to replay"):
        genesis.plan(pub, set(pub), CENTERED_MIN, START - timedelta(seconds=1))


# -- the replay -------------------------------------------------------------


async def test_genesis_replays_from_publish_dates(p: Any, clean_db: str) -> None:
    ids = await corpus(p, clean_db)
    done = await run(p)
    assert done.start == START and done.last_tick == LAST
    assert done.next_due == START + (LAST + 1) * TICK

    rows = tick_rows(clean_db)
    assert [r[0] for r in rows] == list(range(LAST + 1))
    assert all(r[1] == START + r[0] * TICK for r in rows)
    assert all(r[2] is True for r in rows)
    live = {r[0]: r[5] for r in rows}
    assert (live[0], live[1], live[2], live[4], live[5], live[7]) == (4, 4, 5, 5, 6, 7)
    assert live[LAST] == len(ids)

    events = sql(clean_db, "select kind, simulated, scheduled_at, tick from colony_events")
    assert events, "the fast tunables should form a colony in 40 ticks"
    assert all(sim for _, sim, _, _ in events)
    assert all(at == START + t * TICK for _, _, at, t in events)
    assert sql(clean_db, "select bool_and(simulated) from colonies") == [(True,)]

    st = await state(p)
    assert not st.running
    assert st.last_tick == LAST and st.simulated_through == LAST
    assert st.next_due == done.next_due
    assert not st.graph_dirty and st.graph_version == done.graph_version
    assert st.colonies is not None
    assert st.tunables_version == 1

    # The stored graph is exactly a live rebuild of today's corpus.
    cfg = get_settings()
    async with p.connection() as conn:
        inputs = await store.load_graph_inputs(conn, model=MODEL, dim=DIM)
        stored = await store.load_candidate_edges(conn)
        states = await store.load_edge_states(conn)
    live_graph = tick.build_graph(inputs, await _tunables(p), centered_min=cfg.centered_min_images)
    assert stored == {"whole": live_graph}
    from penish.mycelium import engine

    assert engine.state_hash(states) == done.state_hash == rows[-1][3]
    assert any(lvl > 0 for (lvl,) in sql(clean_db, "select neighbor_level from picture_attention"))


async def test_a_replay_blocks_no_reader(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await corpus(p, clean_db)
    await run(p)
    old = sql(clean_db, "select * from edge_state order by layer, src, dst")
    src, dst = sql(clean_db, "select src, dst from candidate_edges order by src, dst limit 1")[0]
    # A picture with a level, so the view writes a row the replay will write too.
    (seen,) = sql(
        clean_db,
        "select image_id from picture_attention where neighbor_level > 0 order by image_id limit 1",
    )[0]
    inside = threading.Event()
    real = tick.step

    def slow(*a: Any) -> Any:
        inside.set()
        time.sleep(0.03)
        return real(*a)

    monkeypatch.setattr(tick, "step", slow)
    replay = asyncio.create_task(run(p))
    assert await asyncio.to_thread(inside.wait, 10)
    # Past the wipe and into the replay. Every read an image page or a view makes
    # answers at once, from the network as it was.
    async with await psycopg.AsyncConnection.connect(
        clean_db, row_factory=dict_row, autocommit=True
    ) as conn:
        await conn.execute("set lock_timeout = '500ms'")
        assert await store.hyphae_for(conn, ids[0]) is not None
        assert not await store.click_feeds(conn, src, dst)
        assert sql(clean_db, "select * from edge_state order by layer, src, dst") == old
        await conn.execute("select id from images where id = %s", (seen,))
        assert await store.record_view(conn, seen)
    assert not replay.done()
    done = await replay
    assert done.last_tick == LAST
    assert sql(clean_db, "select views from picture_attention where image_id = %s", seen) == [(1,)]


async def _tunables(p: Any) -> Any:
    async with p.connection() as conn:
        return await store.load_tunables(conn)


async def test_same_corpus_same_replay(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db)
    await run(p)
    first = tick_rows(clean_db)
    events = sql(clean_db, "select tick, kind, colony_id, image_id from colony_events order by id")
    await run(p)
    assert tick_rows(clean_db) == first
    assert (
        sql(clean_db, "select tick, kind, colony_id, image_id from colony_events order by id")
        == events
    )


async def test_live_tick_continues_without_a_rebuild(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db)
    done = await run(p)
    async with p.connection() as conn:
        st = await store.start(conn, now=done.next_due - timedelta(minutes=1))
    # Genesis left the clock inside the next tick length, so start keeps it.
    assert st.running and st.next_due == done.next_due
    async with p.connection() as conn:
        ran = await tick.run_one(conn, now=done.next_due)
    assert ran is not None
    assert ran.tick == LAST + 1 and not ran.rebuilt
    assert ran.graph_version == done.graph_version
    assert sql(clean_db, "select simulated from mycelium_ticks where tick = %s", LAST + 1) == [
        (False,)
    ]


async def test_non_live_pictures_stay_out(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db, 8)
    hidden = add(clean_db, 9, status="queued")
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    await run(p)
    assert sql(
        clean_db,
        "select count(*) from candidate_edges where src = %s or dst = %s",
        hidden,
        hidden,
    ) == [(0,)]
    assert tick_rows(clean_db)[-1][5] == 8


async def test_live_picture_without_a_vector_is_live_without_edges(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db, 8)
    bare = add(clean_db, 9, with_vec=False)
    await run(p)
    assert tick_rows(clean_db)[-1][5] == 9
    assert sql(
        clean_db, "select count(*) from candidate_edges where src = %s or dst = %s", bare, bare
    ) == [(0,)]


# -- refusals and failures --------------------------------------------------


async def test_refuses_when_running(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db)
    async with p.connection() as conn:
        await store.save_state(conn, running=True, next_due=NOW)
    before = snapshot(clean_db)
    with pytest.raises(genesis.GenesisRefused, match="running"):
        await run(p)
    assert snapshot(clean_db) == before


async def test_refuses_when_public(p: Any, clean_db: str) -> None:
    await corpus(p, clean_db)
    async with p.connection() as conn:
        await store.save_state(conn, public_since=NOW)
    before = snapshot(clean_db)
    with pytest.raises(genesis.GenesisRefused, match="public"):
        await run(p)
    assert snapshot(clean_db) == before


async def test_refuses_a_small_corpus(p: Any, clean_db: str) -> None:
    for i in range(1, 4):
        add(clean_db, i)
    with pytest.raises(genesis.GenesisRefused, match="fewer than"):
        await run(p)
    assert tick_rows(clean_db) == []


async def test_mismatch_rolls_everything_back(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await corpus(p, clean_db)
    await run(p)
    before = snapshot(clean_db)

    real = store.load_graph_inputs
    calls = 0

    async def drifting(conn: Any, **kw: Any) -> store.GraphInputs:
        nonlocal calls
        calls += 1
        got = await real(conn, **kw)
        if calls == 1:
            return got
        # The second read, the live rebuild, sees one picture fewer.
        drop = max(got.vectors)
        return store.GraphInputs(
            {k: v for k, v in got.vectors.items() if k != drop}, got.mean, got.published
        )

    monkeypatch.setattr(store, "load_graph_inputs", drifting)
    with pytest.raises(genesis.GenesisMismatch):
        await run(p)
    # The wipe rolled back with the rest: the earlier run is still there, whole.
    assert snapshot(clean_db) == before


# -- the job ----------------------------------------------------------------


async def run_job(p: Any, dsn: str) -> tuple[str, str | None]:
    job_id = await jobs.enqueue(p, genesis.JOB, {}, dedupe_key=genesis.JOB)
    assert job_id is not None
    handlers = make_handlers(p, get_settings(), embedder=None)  # type: ignore[arg-type]
    job = await jobs.claim(p, [genesis.JOB])
    assert job is not None and job.id == job_id
    await jobs._run_one(p, job, handlers[genesis.JOB])
    row = sql(dsn, "select status::text, last_error from jobs where id = %s", job_id)[0]
    return row[0], row[1]


async def test_job_runs_and_refusal_fails_for_good(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await corpus(p, clean_db)
    # The job uses the wall clock; pin the plan's end so the replay stays short.
    real = genesis.plan

    def short(*a: Any) -> genesis.Plan:
        return real(*a[:3], NOW)

    monkeypatch.setattr(genesis, "plan", short)
    status, error = await run_job(p, clean_db)
    assert (status, error) == ("done", None)
    assert (await state(p)).simulated_through == LAST

    async with p.connection() as conn:
        await store.save_state(conn, running=True)
    status, error = await run_job(p, clean_db)
    assert status == "failed"
    assert error is not None and "running" in error
