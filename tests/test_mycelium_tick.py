"""The live tick against a real database: one transaction per tick, nothing lost or doubled."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import psycopg
import pytest

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium import engine, store, tick
from penish.mycelium.colonies import ColoniesState
from penish.mycelium.tunables import TICK_SECONDS
from penish.worker import admin_jobs, ingest
from penish.worker.describe import Described
from penish.worker.jobs import SoftError
from penish.worker.metrics import refresh_corpus_stats
from penish.worker.sidecars import Group
from tests.test_admin_pictures import XHR, client  # noqa: F401
from tests.test_admin_pictures import add as add_picture

MODEL = "test/embed"
DIM = 4
T0 = datetime(2026, 1, 1, tzinfo=UTC)
TICK = timedelta(seconds=TICK_SECONDS)
# Fast enough that a colony forms and fruits within a handful of ticks.
FAST = {"rho": 0.2, "colony_enter": 0.1, "colony_leave": 0.08, "fruit_ticks": 3, "admit_ticks": 1}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EMBED_MODEL", MODEL)
    monkeypatch.setenv("EMBED_DIM", str(DIM))
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


def sha(n: int) -> str:
    return f"{n:064x}"


def vec(n: int) -> list[float]:
    # Three tight clusters of four, a little apart inside each.
    c, k = divmod(n - 1, 4)
    v = [0.1 * ((k * 7 + c * 3 + j) % 5) / 5 for j in range(DIM)]
    v[c % DIM] += 1.0
    return v


def sql(dsn: str, q: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(q.encode(), args)
        return cur.fetchall() if cur.description else []


def add(dsn: str, n: int, *, status: str = "live") -> int:
    rows = sql(
        dsn,
        "insert into images (sha256, pub_sha, slug, status, published_at)"
        " values (%s, %s, %s, %s, %s) returning id",
        sha(n),
        sha(n),
        f"img-{n}",
        status,
        T0 - timedelta(days=30) + timedelta(minutes=n),
    )
    image_id = int(rows[0][0])
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
        T0 - timedelta(days=3),
    )
    return image_id


def corpus(dsn: str, n: int = 12) -> list[int]:
    return [add(dsn, i) for i in range(1, n + 1)]


async def running(p: Any, due: datetime = T0, fast: bool = True) -> None:
    async with p.connection() as conn:
        if fast:
            await store.set_tunables(conn, FAST, "fast")
        await store.save_state(conn, running=True, next_due=due)


async def one(p: Any, now: datetime) -> tick.Ran | None:
    async with p.connection() as conn:
        return await tick.run_one(conn, now=now)


async def state(p: Any) -> store.State:
    async with p.connection() as conn:
        return await store.read_state(conn)


def ticks(dsn: str) -> list[tuple[Any, ...]]:
    return sql(
        dsn,
        "select tick, scheduled_at, state_hash, colonies_hash from mycelium_ticks order by tick",
    )


def snapshot(dsn: str) -> dict[str, Any]:
    """Every table a tick writes, in a comparable form."""
    out: dict[str, Any] = {}
    for table, order in (
        ("candidate_edges", "layer, src, dst"),
        ("edge_state", "layer, src, dst"),
        ("mycelium_traffic", "id"),
        ("mycelium_ticks", "tick"),
        ("colonies", "id"),
        ("colony_members", "id"),
        ("colony_events", "id"),
        ("colony_ticks", "colony_id, tick"),
        ("picture_attention", "image_id"),
    ):
        out[table] = sql(dsn, f"select * from {table} order by {order}")
    out["state"] = sql(
        dsn,
        "select running, seed, next_due, last_tick, graph_version, graph_dirty,"
        " tunables_version, colonies from mycelium_state",
    )
    return out


async def click(dsn: str, at: datetime, pair: tuple[int, int] | None = None) -> tuple[int, int]:
    if pair is None:
        row = sql(dsn, "select src, dst from candidate_edges order by src, dst limit 1")[0]
        pair = (int(row[0]), int(row[1]))
    sql(
        dsn,
        "insert into mycelium_traffic (src, dst, created_at) values (%s, %s, %s)",
        pair[0],
        pair[1],
        at,
    )
    return pair


# -- stopped ----------------------------------------------------------------


async def test_stopped_changes_nothing(p: Any, clean_db: str) -> None:
    ids = corpus(clean_db)
    async with p.connection() as conn:
        await store.save_state(conn, running=False, next_due=T0)
        await conn.commit()
    await click(clean_db, T0 - TICK, (ids[0], ids[1]))
    before = snapshot(clean_db)
    assert await one(p, T0 + 50 * TICK) is None
    assert await tick.run_due(p, now=T0 + 50 * TICK) == 0
    assert snapshot(clean_db) == before
    assert (await state(p)).behind(T0 + 50 * TICK) == 0


async def test_not_due_is_not_run(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    assert await one(p, T0 - timedelta(seconds=1)) is None
    assert ticks(clean_db) == []
    assert await one(p, T0) is not None


# -- one tick ---------------------------------------------------------------


async def test_a_tick_writes_everything_once(p: Any, clean_db: str) -> None:
    ids = corpus(clean_db)
    await running(p)
    ran = await one(p, T0)
    assert ran is not None and ran.tick == 0 and ran.rebuilt and ran.graph_version == 1
    st = await state(p)
    assert st.last_tick == 0
    assert st.next_due == T0 + TICK
    assert st.graph_version == 1 and not st.graph_dirty
    assert st.tunables_version == 1
    assert st.colonies is not None and ColoniesState.from_dict(st.colonies).tick == 0
    assert sql(clean_db, "select count(*) from candidate_edges where layer = 'whole'") == [
        (len(ids) * 8,)
    ]
    assert sql(clean_db, "select count(*) > 0 from edge_state") == [(True,)]
    rows = sql(
        clean_db,
        "select tick, scheduled_at, simulated, stats, tunables_version,"
        " graph_version from mycelium_ticks",
    )
    assert len(rows) == 1
    t, at, simulated, stats, tv, gv = rows[0]
    assert (t, at, simulated, tv, gv) == (0, T0, False, 1, 1)
    assert set(stats) == {"whole"}
    assert set(stats["whole"]) == {
        "live",
        "in_gallery",
        "edges",
        "visible",
        "latched",
        "fused",
        "mean_strength",
        "crossings",
        "traffic",
        "clicks",
    }
    assert stats["whole"]["live"] == len(ids)
    assert stats["whole"]["crossings"] == len(ids) * 3
    # The same tick again is not due, and could not be committed twice anyway.
    assert await one(p, T0) is None
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with p.connection() as conn:
            await store.insert_tick(conn, store.TickRow(0, T0, False, "x", "y", {}, None, None))


async def test_neighbor_level_written_for_live(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    for k in range(4):
        assert await one(p, T0 + k * TICK) is not None
    levels = sql(clean_db, "select neighbor_level from picture_attention")
    # Every picture was last seen three days before the run, so its neighbors are too.
    assert any(lvl > 0 for (lvl,) in levels)
    assert all(0 <= lvl <= 1 for (lvl,) in levels)


async def test_base_overgrowth_is_read_at_the_scheduled_time(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    # Seen a day after these ticks are scheduled: no base at any of them, so no level
    # either. The wall clock is long past that day and would give every one a base.
    sql(clean_db, "update picture_attention set last_viewed_at = %s", T0 + timedelta(days=1))
    await running(p)
    for k in range(4):
        assert await one(p, T0 + k * TICK) is not None
    assert sql(clean_db, "select count(*) from edge_state") != [(0,)]
    assert sql(clean_db, "select count(*) from picture_attention where neighbor_level <> 0") == [
        (0,)
    ]


async def test_interrupted_tick_neither_lost_nor_doubled(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    assert await one(p, T0 + TICK) is not None
    pair = await click(clean_db, T0 + TICK)
    before = snapshot(clean_db)

    real = store.insert_tick

    async def boom(conn: Any, row: store.TickRow) -> None:
        # Everything else of the tick is already written in this transaction.
        raise RuntimeError("worker killed")

    monkeypatch.setattr(store, "insert_tick", boom)
    with pytest.raises(RuntimeError):
        await one(p, T0 + 2 * TICK)
    assert snapshot(clean_db) == before

    monkeypatch.setattr(store, "insert_tick", real)
    ran = await one(p, T0 + 2 * TICK)
    assert ran is not None and ran.tick == 2
    assert [r[0] for r in ticks(clean_db)] == [0, 1, 2]
    assert sql(clean_db, "select count(*) from mycelium_traffic") == [(0,)]
    assert await one(p, T0 + 2 * TICK) is None
    got = [r[2:] for r in ticks(clean_db)]

    # The same ticks from scratch, the same click before tick 2: the same hashes, so the
    # failed attempt left no trace in the result either.
    async with p.connection() as conn:
        assert await store.reset(conn) == "full"
        await store.save_state(conn, next_due=T0)
    assert await one(p, T0) is not None
    assert await one(p, T0 + TICK) is not None
    await click(clean_db, T0 + TICK, pair)
    assert await one(p, T0 + 2 * TICK) is not None
    assert [r[2:] for r in ticks(clean_db)] == got


async def test_same_state_same_hashes(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    for k in range(6):
        assert await one(p, T0 + k * TICK) is not None
    first = ticks(clean_db)
    events = sql(clean_db, "select tick, kind, colony_id, image_id from colony_events order by id")
    assert events, "the fast tunables should have formed a colony"
    async with p.connection() as conn:
        await store.reset(conn)
        await store.save_state(conn, next_due=T0)
    assert ticks(clean_db) == []
    for k in range(6):
        assert await one(p, T0 + k * TICK) is not None
    assert ticks(clean_db) == first
    assert (
        sql(clean_db, "select tick, kind, colony_id, image_id from colony_events order by id")
        == events
    )


async def test_restored_state_hashes_like_memory(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    ran = None
    for k in range(3):
        ran = await one(p, T0 + k * TICK)
    assert ran is not None
    async with p.connection() as conn:
        states = await store.load_edge_states(conn)
    # Float64 survives the round trip exactly, or a restart would fork the run.
    assert engine.state_hash(states) == ran.state_hash


async def test_restored_empty_state_hashes_like_memory(p: Any, clean_db: str) -> None:
    # Below min_size there is no graph: the tick saves an empty whole layer, and a reload
    # has no row to name it by.
    add(clean_db, 1)
    await running(p)
    ran = await one(p, T0)
    assert ran is not None
    async with p.connection() as conn:
        states = await store.load_edge_states(conn)
    assert states == {}
    assert engine.state_hash(states) == ran.state_hash


# -- traffic ----------------------------------------------------------------


async def test_clicks_during_a_tick_neither_lost_nor_doubled(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    pair = await click(clean_db, T0 + TICK - timedelta(minutes=1))
    # Stamped after tick 2's window: left for tick 3 even though it is already here.
    await click(clean_db, T0 + 3 * TICK + timedelta(seconds=1), pair)

    real = store.consume_traffic
    calls = 0

    async def during(conn: Any, before: datetime) -> dict[tuple[int, int], int]:
        nonlocal calls
        out = await real(conn, before)
        calls += 1
        if calls == 1:
            # Committed by another connection while the tick is still open.
            sql(
                clean_db,
                "insert into mycelium_traffic (src, dst, created_at) values (%s, %s, %s)",
                pair[0],
                pair[1],
                T0 + TICK + timedelta(seconds=30),
            )
        return out

    monkeypatch.setattr(store, "consume_traffic", during)
    assert await one(p, T0 + TICK) is not None
    assert sql(clean_db, "select count(*) from mycelium_traffic") == [(2,)]
    assert await one(p, T0 + 2 * TICK) is not None
    assert sql(clean_db, "select count(*) from mycelium_traffic") == [(1,)]
    assert await one(p, T0 + 3 * TICK) is not None
    assert sql(clean_db, "select count(*) from mycelium_traffic") == [(0,)]
    clicks = sql(
        clean_db,
        "select tick, (stats -> 'whole' ->> 'clicks')::int from mycelium_ticks order by tick",
    )
    assert clicks == [(0, 0), (1, 1), (2, 1), (3, 1)]


# -- catch-up ---------------------------------------------------------------


async def test_catch_up_runs_in_order(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    now = T0 + 2 * TICK + timedelta(minutes=3)
    assert (await state(p)).behind(now) == 3
    assert await tick.run_due(p, now=now) == 3
    assert [(r[0], r[1]) for r in ticks(clean_db)] == [(k, T0 + k * TICK) for k in range(3)]
    st = await state(p)
    assert st.next_due == T0 + 3 * TICK
    assert st.behind(now) == 0


async def test_catch_up_cap_shifts_the_clock(p: Any, clean_db: str) -> None:
    assert tick.CATCH_UP_MAX == 144
    corpus(clean_db)
    await running(p)
    now = T0 + 9 * TICK
    assert await tick.run_due(p, now=now, max_ticks=4) == 4
    assert [r[0] for r in ticks(clean_db)] == [0, 1, 2, 3]
    st = await state(p)
    assert st.next_due == now
    # The next wake runs one tick at the shifted time, then the clock is regular again.
    assert await tick.run_due(p, now=now, max_ticks=4) == 1
    assert ticks(clean_db)[-1][:2] == (4, now)
    assert (await state(p)).next_due == now + TICK


async def test_pause_stops_between_ticks(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    async with p.connection() as conn:
        st = await store.pause(conn)
    assert not st.running
    assert await tick.run_due(p, now=T0 + 20 * TICK) == 0
    async with p.connection() as conn:
        st = await store.start(conn, now=T0 + 20 * TICK)
    # A resume does not replay the pause.
    assert st.running and st.next_due == T0 + 21 * TICK
    assert await tick.run_due(p, now=T0 + 21 * TICK) == 1
    assert ticks(clean_db)[-1][:2] == (1, T0 + 21 * TICK)


# -- the graph --------------------------------------------------------------


async def test_dirty_mark_triggers_a_rebuild(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    assert (await state(p)).graph_dirty
    assert await one(p, T0) is not None
    new = add(clean_db, 13)
    ran = await one(p, T0 + TICK)
    assert ran is not None and not ran.rebuilt and ran.graph_version == 1
    has_new = "select count(*) > 0 from candidate_edges where src = %s or dst = %s"
    assert sql(clean_db, has_new, new, new) == [(False,)]

    async with p.connection() as conn:
        await store.mark_dirty(conn)
    ran = await one(p, T0 + 2 * TICK)
    assert ran is not None and ran.rebuilt and ran.graph_version == 2
    assert sql(clean_db, has_new, new, new) == [(True,)]
    assert not (await state(p)).graph_dirty


async def test_graph_tunables_mark_dirty(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p, fast=False)
    assert await one(p, T0) is not None
    async with p.connection() as conn:
        await store.set_tunables(conn, {"rho": 0.05})
        assert not (await store.read_state(conn)).graph_dirty
        await store.set_tunables(conn, {"candidates": 4})
        assert (await store.read_state(conn)).graph_dirty
    assert await one(p, T0 + TICK) is not None
    assert sql(clean_db, "select count(*) from candidate_edges") == [(12 * 4,)]


async def test_go_live_marks_dirty(p: Any, clean_db: str) -> None:
    rows = sql(
        clean_db,
        "insert into images (sha256, status) values (%s, 'processing') returning id",
        sha(1),
    )
    async with p.connection() as conn:
        await store.save_state(conn, graph_dirty=False)
        await conn.commit()
        assert await ingest._go_live(conn, int(rows[0][0]), "a-cup", None)
        await conn.commit()
    assert (await state(p)).graph_dirty


async def test_admin_delete_and_analysis_edit_mark_dirty(
    client: Any,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_picture(clean_db, 1, vec=[1, 0, 0, 0])
    b = add_picture(clean_db, 2, vec=[0, 1, 0, 0])
    dirty = "select graph_dirty from mycelium_state"
    sql(clean_db, "update mycelium_state set graph_dirty = false")
    r = await client.patch(
        f"/api/admin/pictures/{b}", json={"analysis": "Subject: x."}, headers=XHR
    )
    assert r.status_code == 200
    assert sql(clean_db, dirty) == [(True,)]
    sql(clean_db, "update mycelium_state set graph_dirty = false")
    assert (await client.delete(f"/api/admin/pictures/{a}", headers=XHR)).status_code == 200
    assert sql(clean_db, dirty) == [(True,)]


async def test_analysis_edit_during_a_tick_does_not_deadlock(
    p: Any,
    clean_db: str,
    client: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus(clean_db)
    await running(p)
    assert await one(p, T0) is not None
    # A picture the tick will write edge rows for, each of which key-share locks it.
    x = int(sql(clean_db, "select src from edge_state order by src limit 1")[0][0])
    inside, go = threading.Event(), threading.Event()
    real = tick.step

    def held(*a: Any) -> Any:
        # The tick holds the state row while this runs.
        inside.set()
        go.wait(10)
        return real(*a)

    monkeypatch.setattr(tick, "step", held)
    ticking = asyncio.create_task(one(p, T0 + TICK))
    assert await asyncio.to_thread(inside.wait, 10)
    editing = asyncio.create_task(
        client.patch(f"/api/admin/pictures/{x}", json={"analysis": "Subject: x."}, headers=XHR)
    )
    # Long enough for the edit to take every lock it takes before the state row.
    await asyncio.sleep(0.3)
    go.set()
    ran = await asyncio.wait_for(ticking, 20)
    r = await asyncio.wait_for(editing, 20)
    assert ran is not None and ran.tick == 1
    assert r.status_code == 200
    # The edit waited for the tick, so the tick cannot have cleared its mark.
    assert (await state(p)).graph_dirty


# -- embedding writes mark the graph ----------------------------------------


class FakeEmbedder:
    def __init__(self, during: Any = None) -> None:
        self.during = during

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        if self.during is not None:
            self.during()
        return np.ones((len(texts), DIM), dtype=np.float32) / 2


def clean(dsn: str) -> None:
    sql(dsn, "update mycelium_state set graph_dirty = false")


def dirty(dsn: str) -> bool:
    return bool(sql(dsn, "select graph_dirty from mycelium_state")[0][0])


def texted(dsn: str, n: int, analysis: str = "Subject: a cup.") -> int:
    image_id = add(dsn, n)
    sql(
        dsn,
        "insert into image_texts (image_id, field, body, source, model)"
        " values (%s, 'analysis', %s, 'model', 'm')",
        image_id,
        analysis,
    )
    return image_id


async def test_admin_embed_marks_dirty(p: Any, clean_db: str) -> None:
    image_id = texted(clean_db, 1)
    sql(clean_db, "delete from embeddings")
    clean(clean_db)
    await admin_jobs._embed(p, image_id, get_settings(), FakeEmbedder())
    assert dirty(clean_db)
    assert sql(clean_db, "select count(*) from embeddings") == [(1,)]


async def test_admin_embed_that_writes_nothing_leaves_the_graph(p: Any, clean_db: str) -> None:
    image_id = texted(clean_db, 1)
    sql(clean_db, "delete from embeddings")
    clean(clean_db)

    def edited() -> None:
        sql(
            clean_db,
            "update image_texts set body = 'Subject: a jug.' where image_id = %s",
            image_id,
        )

    with pytest.raises(SoftError):
        await admin_jobs._embed(p, image_id, get_settings(), FakeEmbedder(edited))
    assert not dirty(clean_db)
    assert sql(clean_db, "select count(*) from embeddings") == [(0,)]


async def test_described_analysis_marks_dirty(p: Any, clean_db: str) -> None:
    image_id = texted(clean_db, 1)
    clean(clean_db)
    same = Described(None, None, None, "m", analysis="Subject: a cup.")
    async with p.connection() as conn:
        assert not await admin_jobs._apply_described(conn, image_id, same, {"analysis"}, set())
    assert not dirty(clean_db)
    new = Described(None, None, None, "m", analysis="Subject: a jug.")
    async with p.connection() as conn:
        assert await admin_jobs._apply_described(conn, image_id, new, {"analysis"}, set())
    assert dirty(clean_db)
    assert sql(clean_db, "select count(*) from embeddings") == [(0,)]


@pytest.mark.parametrize("autocommit", [False, True])
async def test_ingest_embedding_writes_mark_dirty(p: Any, clean_db: str, autocommit: bool) -> None:
    image_id = texted(clean_db, 1)
    clean(clean_db)
    async with p.connection() as conn:
        await conn.set_autocommit(autocommit)
        await ingest._write_text(
            conn, image_id, "analysis", "Subject: a jug.", "model", "m", "Subject: a cup."
        )
        await conn.commit()
        await conn.set_autocommit(False)
    assert dirty(clean_db)
    assert sql(clean_db, "select count(*) from embeddings") == [(0,)]

    clean(clean_db)
    group = Group(stem="a-cup", image=Path("a-cup.png"))
    async with p.connection() as conn:
        await conn.set_autocommit(autocommit)
        await ingest._ensure_embedding(conn, image_id, group, get_settings(), FakeEmbedder())
        await conn.commit()
        await conn.set_autocommit(False)
    assert dirty(clean_db)
    assert sql(clean_db, "select count(*) from embeddings") == [(1,)]


async def test_corpus_mean_refresh_marks_dirty(p: Any, clean_db: str) -> None:
    # The writer's mark and the refreshed mean commit apart; a tick between the two
    # would rebuild on the old mean and clear the mark.
    corpus(clean_db, 4)
    clean(clean_db)
    await refresh_corpus_stats(p, kind="description", model=MODEL)
    assert dirty(clean_db)
    clean(clean_db)
    await refresh_corpus_stats(p, kind="caption", model=MODEL)
    assert not dirty(clean_db)


# -- pictures that leave ----------------------------------------------------


async def test_non_live_pictures_leave_edges_and_colonies(p: Any, clean_db: str) -> None:
    ids = corpus(clean_db)
    await running(p)
    k = 0
    for k in range(5):
        assert await one(p, T0 + k * TICK) is not None
    st = ColoniesState.from_dict((await state(p)).colonies or {})
    alive = [c for c in st.colonies if c.state == "alive"]
    assert alive, "the fast tunables should have fruited a gallery"
    gallery = alive[0]
    gone, deleted = sorted(gallery.images())[:2]
    assert sql(
        clean_db,
        "select count(*) from colony_members where colony_id = %s"
        " and founder and left_tick is null",
        gallery.id,
    ) == [(len(gallery.members),)]

    # One stops being live, one is deleted outright.
    sql(clean_db, "update images set status = 'failed' where id = %s", gone)
    sql(clean_db, "delete from images where id = %s", deleted)
    k += 1
    assert await one(p, T0 + k * TICK) is not None

    touching = "select count(*) from {} where src = %s or dst = %s"
    for table in ("edge_state", "candidate_edges"):
        assert sql(clean_db, touching.format(table), gone, gone) == [(0,)], table
        assert sql(clean_db, touching.format(table), deleted, deleted) == [(0,)], table
    st = ColoniesState.from_dict((await state(p)).colonies or {})
    for c in st.colonies:
        if c.state in ("forming", "alive", "fading"):
            assert gone not in c.images() and deleted not in c.images()
    # Their membership rows are closed, the deleted one keeping only its slug.
    rows = sql(
        clean_db,
        "select image_id, slug, left_tick from colony_members where colony_id = %s"
        " and (image_id = %s or image_id is null)",
        gallery.id,
        gone,
    )
    assert sorted(rows, key=lambda r: r[1]) == sorted(
        [(gone, f"img-{ids.index(gone) + 1}", k), (None, f"img-{ids.index(deleted) + 1}", k)],
        key=lambda r: r[1],
    )
    left = sql(
        clean_db,
        "select image_id from colony_events where kind = 'left' and colony_id = %s"
        " order by image_id nulls last",
        gallery.id,
    )
    assert left == [(gone,), (None,)]


# -- reset ------------------------------------------------------------------


async def test_reset_after_public_keeps_tombstones(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    for k in range(5):
        assert await one(p, T0 + k * TICK) is not None
    serials = sql(clean_db, "select id, serial from colonies where serial is not null")
    assert serials
    async with p.connection() as conn:
        await store.save_state(conn, public_since=T0)
        await conn.commit()
        assert await store.reset(conn) == "public"
    assert sql(clean_db, "select count(*) from edge_state") == [(0,)]
    assert sql(clean_db, "select id, serial from colonies where serial is not null") == serials
    assert sql(
        clean_db, "select count(*) from colonies where state in ('forming', 'alive', 'fading')"
    ) == [(0,)]
    assert sql(clean_db, "select distinct cause from colonies where died_tick = 4") == [("reset",)]
    assert sql(clean_db, "select count(*) > 0 from colony_events where kind = 'reset'") == [(True,)]
    st = await state(p)
    assert st.last_tick == 4 and st.graph_dirty
    # Life goes on from the next tick with the old serials kept.
    assert await one(p, T0 + 5 * TICK) is not None
    col = ColoniesState.from_dict((await state(p)).colonies or {})
    assert col.next_serial == max(s for _, s in serials) + 1


# -- public reads -----------------------------------------------------------


async def test_hyphae_for_one_picture(p: Any, clean_db: str) -> None:
    ids = corpus(clean_db)
    async with p.connection() as conn:
        assert await store.hyphae_for(conn, ids[0]) == []
    await running(p)
    for k in range(4):
        assert await one(p, T0 + k * TICK) is not None
    async with p.connection() as conn:
        t = await store.load_tunables(conn)
        got = await store.hyphae_for(conn, ids[0])
    assert 0 < len(got) <= store.HYPHAE_MAX
    assert all(h.strength >= t.hypha_visible and h.layer == "whole" for h in got)
    assert len({h.image_id for h in got}) == len(got)
    assert ids[0] not in {h.image_id for h in got}
    assert got == sorted(got, key=lambda h: (-h.strength, h.slug))
    best = sql(
        clean_db,
        "select max(strength) from edge_state where (src = %s and dst = %s)"
        " or (src = %s and dst = %s)",
        ids[0],
        got[0].image_id,
        got[0].image_id,
        ids[0],
    )
    assert got[0].strength == best[0][0]
    # A neighbor that stops being live disappears from the strip at once.
    sql(clean_db, "update images set status = 'failed' where id = %s", got[0].image_id)
    async with p.connection() as conn:
        again = await store.hyphae_for(conn, ids[0])
    assert got[0].image_id not in {h.image_id for h in again}


async def test_admin_reads(p: Any, clean_db: str) -> None:
    corpus(clean_db)
    await running(p)
    for k in range(5):
        assert await one(p, T0 + k * TICK) is not None
    async with p.connection() as conn:
        s = await store.series(conn)
        listing = await store.colonies_listing(conn)
        events = await store.recent_events(conn)
        history = await store.tunables_history(conn)
    assert [x["tick"] for x in s] == [0, 1, 2, 3, 4]
    assert set(s[0]) == {
        "tick",
        "scheduled_at",
        "simulated",
        "live",
        "in_gallery",
        "visible",
        "latched",
        "mean_strength",
        "crossings",
        "traffic",
    }
    assert s[-1]["in_gallery"] > 0
    assert set(listing) == {"forming", "living"}
    living = listing["living"]
    assert living and living[0]["serial"] == 1 and living[0]["size"] == len(living[0]["members"])
    assert all(set(m) == {"slug", "founder"} for m in living[0]["members"])
    assert events[0]["tick"] >= events[-1]["tick"]
    assert {e["kind"] for e in events} >= {"formed", "fruited"}
    assert history[0]["version"] == 1 and history[0]["note"] == "fast"
    assert history[0]["values"] == FAST


async def test_tunables_store_only_overrides(p: Any) -> None:
    from penish.mycelium.tunables import DEFAULTS, TunableError

    async with p.connection() as conn:
        assert await store.load_tunables(conn) == DEFAULTS
        v1 = await store.set_tunables(conn, {"rho": 0.02, "walkers": 2}, "a")
        v2 = await store.set_tunables(conn, {"rho": DEFAULTS.rho})
        assert (v1, v2) == (1, 2)
        t = await store.load_tunables(conn)
        assert t.walkers == 2 and t.rho == DEFAULTS.rho
        hist = await store.tunables_history(conn)
        assert [h["values"] for h in hist] == [{"walkers": 2}, {"rho": 0.02, "walkers": 2}]
        with pytest.raises(TunableError):
            await store.set_tunables(conn, {"rho": 5})
        with pytest.raises(TunableError):
            await store.set_tunables(conn, {"nope": 1})
        assert len(await store.tunables_history(conn)) == 2


async def test_overgrowth_reads_current_tunables(p: Any, clean_db: str) -> None:
    a = add(clean_db, 1)
    sql(
        clean_db,
        "update picture_attention set last_viewed_at = now() - interval '72 hours',"
        " neighbor_level = 1 where image_id = %s",
        a,
    )
    async with p.connection() as conn:
        before = await store.overgrowth_by_id(conn, [a])
        await store.set_tunables(conn, {"overgrowth_sigma": 0.0})
        after = await store.overgrowth_by_id(conn, [a])
    assert cast(float, before[a]) > after[a] == 0.632


async def test_loop_survives_a_failing_tick(
    p: Any, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    calls = 0
    stop = asyncio.Event()

    async def bad(pool: Any, **kw: Any) -> int:
        nonlocal calls
        calls += 1
        if calls >= 3:
            stop.set()
        raise RuntimeError("no")

    monkeypatch.setattr(tick, "run_due", bad)
    await asyncio.wait_for(tick.run_loop(p, stop, interval=0.01), timeout=5)
    assert calls == 3
