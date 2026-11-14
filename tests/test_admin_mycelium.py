"""The admin router for the organism: gate, payload, actions and refusals."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from penish.config import get_settings
from penish.mycelium import genesis, store
from penish.mycelium.tunables import DEFAULTS, SPECS
from tests.test_admin import PW, XHR, _build, _login
from tests.test_mycelium_tick import DIM, FAST, MODEL, T0, TICK, corpus, sql

URL = "/api/admin/mycelium"
POSTS = ("start", "pause", "tunables", "regrow", "reset", "genesis")
KEYS = {
    "running",
    "tick",
    "next_due",
    "behind",
    "graph_version",
    "graph_dirty",
    "public",
    "simulated_through",
    "genesis",
    "tunables",
    "tunables_version",
    "history",
    "series",
    "colonies",
    "events",
}


@pytest.fixture
async def app(clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("EMBED_MODEL", MODEL)
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    a = _build(clean_db, tmp_path, monkeypatch, PW)
    async with a.router.lifespan_context(a):
        yield a
    get_settings.cache_clear()


@pytest.fixture
async def anon(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        yield c


@pytest.fixture
async def client(anon: httpx.AsyncClient) -> httpx.AsyncClient:
    await _login(anon)
    return anon


async def post(c: httpx.AsyncClient, path: str, body: Any = None) -> httpx.Response:
    return await c.post(f"{URL}/{path}", json=body, headers=XHR)


async def run_ticks(app: Any, n: int, *, start: datetime = T0) -> None:
    from penish.mycelium import tick

    pool = app.state.pool
    for k in range(n):
        async with pool.connection() as conn:
            assert await tick.run_one(conn, now=start + k * TICK) is not None


async def running(app: Any) -> None:
    async with app.state.pool.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
        await store.save_state(conn, running=True, next_due=T0)


# -- the gate ---------------------------------------------------------------


async def test_every_route_needs_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get(URL)
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"
    for path in POSTS:
        r = await post(anon, path, {"values": {"rho": 0.02}})
        assert r.status_code == 401, path


async def test_every_post_needs_the_header(client: httpx.AsyncClient) -> None:
    for path in POSTS:
        r = await client.post(f"{URL}/{path}", json={"values": {"rho": 0.02}})
        assert r.status_code == 403, path
    # Nothing happened behind the refusals.
    body = (await client.get(URL)).json()
    assert not body["running"] and body["tunables_version"] is None
    assert body["genesis"] == {"status": "none", "error": None}


async def test_off_without_password(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _build(clean_db, tmp_path, monkeypatch, None)
    async with a.router.lifespan_context(a):
        transport = httpx.ASGITransport(app=a)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            assert (await c.get(URL)).status_code == 404
            assert (await post(c, "start")).status_code == 404
    get_settings.cache_clear()


# -- the payload ------------------------------------------------------------


async def test_fresh_payload(client: httpx.AsyncClient) -> None:
    r = await client.get(URL)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == KEYS
    assert body["running"] is False
    assert body["tick"] is None and body["next_due"] is None and body["behind"] == 0
    assert body["graph_version"] == 0 and body["graph_dirty"] is True
    assert body["public"] is False and body["simulated_through"] is None
    assert body["history"] == [] and body["series"] == [] and body["events"] == []
    assert body["colonies"] == {"forming": [], "living": []}
    names = [t["name"] for t in body["tunables"]]
    assert names == list(DEFAULTS.as_dict())
    assert set(names) == set(SPECS)
    rho = next(t for t in body["tunables"] if t["name"] == "rho")
    assert rho == {
        "name": "rho",
        "value": 0.01,
        "default": 0.01,
        "lo": 0.0005,
        "hi": 0.2,
        "meaning": SPECS["rho"].meaning,
        "int": False,
    }
    cands = next(t for t in body["tunables"] if t["name"] == "candidates")
    assert cands["int"] is True and cands["lo"] == 2 and cands["hi"] == 32
    assert isinstance(cands["lo"], int)


async def test_payload_after_ticks(app: Any, client: httpx.AsyncClient, clean_db: str) -> None:
    corpus(clean_db)
    await running(app)
    await run_ticks(app, 6)
    body = (await client.get(URL)).json()
    assert body["running"] is True and body["tick"] == 5
    assert body["graph_dirty"] is False and body["graph_version"] == 1
    assert [s["tick"] for s in body["series"]] == list(range(6))
    s = body["series"][0]
    assert set(s) == {
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
    assert s["live"] == 12 and s["simulated"] is False
    assert datetime.fromisoformat(s["scheduled_at"]) == T0
    assert body["events"], "the fast tunables should have formed a colony"
    ev = body["events"][0]
    assert set(ev) == {
        "tick",
        "scheduled_at",
        "kind",
        "colony_id",
        "layer",
        "slug",
        "other_colony_id",
        "simulated",
    }
    # Newest first.
    assert [e["tick"] for e in body["events"]] == sorted(
        (e["tick"] for e in body["events"]), reverse=True
    )
    cols = body["colonies"]["forming"] + body["colonies"]["living"]
    assert cols
    c = cols[0]
    assert set(c) == {
        "id",
        "layer",
        "state",
        "serial",
        "title",
        "size",
        "born_tick",
        "fruited_tick",
        "members",
    }
    assert c["size"] == len(c["members"]) >= 3
    assert all(set(m) == {"slug", "founder"} for m in c["members"])


# -- start and pause --------------------------------------------------------


async def test_start_and_pause(client: httpx.AsyncClient) -> None:
    r = await post(client, "start")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True and body["behind"] == 0
    due = datetime.fromisoformat(body["next_due"])
    now = datetime.now(UTC)
    assert now < due <= now + TICK + timedelta(seconds=5)
    # A second start does not move the clock.
    again = (await post(client, "start")).json()
    assert again["next_due"] == body["next_due"]
    body = (await post(client, "pause")).json()
    assert body["running"] is False
    assert (await client.get(URL)).json()["running"] is False


async def test_regrow_marks_dirty(app: Any, client: httpx.AsyncClient, clean_db: str) -> None:
    corpus(clean_db)
    await running(app)
    await run_ticks(app, 1)
    assert (await client.get(URL)).json()["graph_dirty"] is False
    body = (await post(client, "regrow")).json()
    assert body["graph_dirty"] is True


# -- tunables ---------------------------------------------------------------


async def test_tunables_validation(client: httpx.AsyncClient) -> None:
    cases = [
        ({"values": {"nope": 1}}, "unknown tunable nope"),
        ({"values": {"rho": 5}}, "rho must be between 0.0005 and 0.2"),
        ({"values": {"candidates": 4.5}}, "candidates must be a whole number"),
        ({"values": {"rho": True}}, "rho must be a number"),
        ({"values": {"rho": "0.1"}}, "rho must be a number"),
        ({"values": {"colony_leave": 0.5}}, "colony_leave must not exceed colony_enter"),
        ({"values": {}}, "nothing to change"),
        ({"values": {"rho": 0.02}, "note": "x" * 201}, "note too long"),
    ]
    for body, detail in cases:
        r = await post(client, "tunables", body)
        assert r.status_code == 400, body
        assert r.json()["detail"] == detail
    assert (await post(client, "tunables", {"note": "x"})).status_code == 422
    # JSON as Python reads it allows these literals; int() of them is not a TunableError.
    for name in ("admit_ticks", "rho"):
        for lit in ("NaN", "Infinity", "-Infinity"):
            r = await client.post(
                f"{URL}/tunables",
                content=f'{{"values": {{"{name}": {lit}}}}}',
                headers={**XHR, "content-type": "application/json"},
            )
            assert r.status_code == 400, (name, lit)
            assert r.json()["detail"] == f"{name} must be a number"
    assert (await client.get(URL)).json()["history"] == []


async def test_tunables_and_history(client: httpx.AsyncClient) -> None:
    r = await post(client, "tunables", {"values": {"rho": 0.02}, "note": " faster \u2014 a bit "})
    assert r.status_code == 200
    body = r.json()
    assert body["tunables_version"] == 1
    assert next(t for t in body["tunables"] if t["name"] == "rho")["value"] == 0.02
    body = (await post(client, "tunables", {"values": {"walkers": 2}})).json()
    assert body["tunables_version"] == 2
    values = {t["name"]: t["value"] for t in body["tunables"]}
    # Each post is applied over the overrides before it.
    assert values["rho"] == 0.02 and values["walkers"] == 2
    # Back to the default: the override is dropped, not stored as a value.
    body = (await post(client, "tunables", {"values": {"rho": 0.01}})).json()
    hist = body["history"]
    assert [h["version"] for h in hist] == [3, 2, 1]
    assert hist[0]["values"] == {"walkers": 2}
    assert hist[1]["values"] == {"rho": 0.02, "walkers": 2}
    assert hist[2]["note"] == "faster -- a bit"
    assert hist[1]["note"] is None
    assert datetime.fromisoformat(hist[0]["created_at"])


async def test_history_caps_at_twenty(client: httpx.AsyncClient) -> None:
    for i in range(22):
        r = await post(client, "tunables", {"values": {"wither_idle": 10 + i}})
        assert r.status_code == 200
    hist = r.json()["history"]
    assert len(hist) == 20 and hist[0]["version"] == 22


# -- reset ------------------------------------------------------------------


async def test_reset_before_public_is_a_full_wipe(
    app: Any, client: httpx.AsyncClient, clean_db: str
) -> None:
    corpus(clean_db)
    await running(app)
    await run_ticks(app, 6)
    body = (await post(client, "reset")).json()
    assert body["tick"] is None and body["series"] == [] and body["events"] == []
    assert body["colonies"] == {"forming": [], "living": []}
    assert body["graph_dirty"] is True
    # Settings survive a wipe; only the network goes.
    assert body["running"] is True and body["tunables_version"] == 1
    for table in ("candidate_edges", "edge_state", "colonies", "colony_members", "mycelium_ticks"):
        assert sql(clean_db, f"select count(*) from {table}") == [(0,)], table


async def test_reset_after_public_keeps_tombstones(
    app: Any, client: httpx.AsyncClient, clean_db: str
) -> None:
    corpus(clean_db)
    await running(app)
    await run_ticks(app, 8)
    living = sql(clean_db, "select id from colonies where state in ('forming', 'alive', 'fading')")
    assert living
    serials = sql(clean_db, "select id, serial from colonies order by id")
    async with app.state.pool.connection() as conn:
        await store.save_state(conn, public_since=T0)
    body = (await post(client, "reset")).json()
    assert body["public"] is True
    assert body["colonies"] == {"forming": [], "living": []}
    assert body["tick"] == 7 and len(body["series"]) == 8
    assert {e["kind"] for e in body["events"] if e["tick"] == 7} >= {"reset"}
    assert sql(clean_db, "select id, serial from colonies order by id") == serials
    assert sql(
        clean_db,
        "select count(*) from colonies where id = any(%s) and cause = 'reset' and died_tick = 7",
        [r[0] for r in living],
    ) == [(len(living),)]
    assert sql(clean_db, "select count(*) from edge_state") == [(0,)]


# -- genesis ----------------------------------------------------------------


async def test_genesis_enqueue_and_refusals(app: Any, client: httpx.AsyncClient) -> None:
    r = await post(client, "genesis")
    assert r.status_code == 200
    assert r.json()["genesis"] == {"status": "queued", "error": None}
    # A second press collapses into the queued job.
    assert (await post(client, "genesis")).status_code == 200
    jobs = sql(get_settings().database_url, "select type, dedupe_key, status::text from jobs")
    assert jobs == [(genesis.JOB, genesis.JOB, "queued")]

    async with app.state.pool.connection() as conn:
        await store.save_state(conn, running=True)
    r = await post(client, "genesis")
    assert r.status_code == 409 and r.json()["detail"] == "running"

    async with app.state.pool.connection() as conn:
        await store.save_state(conn, running=False, public_since=T0)
    r = await post(client, "genesis")
    assert r.status_code == 409 and r.json()["detail"] == "public"
    assert len(sql(get_settings().database_url, "select id from jobs")) == 1


async def test_genesis_status_reports_failure(client: httpx.AsyncClient, clean_db: str) -> None:
    sql(
        clean_db,
        "insert into jobs (type, status, last_error) values (%s, 'failed', %s)",
        genesis.JOB,
        "HardError('fewer than 30 pictures')",
    )
    body = (await client.get(URL)).json()
    assert body["genesis"] == {"status": "failed", "error": "HardError('fewer than 30 pictures')"}
    sql(clean_db, "insert into jobs (type, status) values (%s, 'running')", genesis.JOB)
    assert (await client.get(URL)).json()["genesis"] == {"status": "running", "error": None}


async def test_genesis_shows_in_the_payload(
    app: Any, client: httpx.AsyncClient, clean_db: str
) -> None:
    corpus(clean_db)
    async with app.state.pool.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
    settings = get_settings().model_copy(update={"centered_min_images": 4})
    async with app.state.pool.connection() as conn:
        done = await genesis.run(conn, now=T0 - timedelta(days=30) + 30 * TICK, settings=settings)
    body = (await client.get(URL)).json()
    assert body["simulated_through"] == done.last_tick == body["tick"]
    assert body["running"] is False and body["behind"] == 0
    assert body["series"] and all(s["simulated"] for s in body["series"])
    assert all(e["simulated"] for e in body["events"])
