from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from penish import logs
from penish.api import admin_logs
from penish.config import get_settings
from tests.test_admin import PW, XHR, _build, _login

URL = "/api/admin/logs"


@pytest.fixture
async def client(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    app = _build(clean_db, tmp_path, monkeypatch, PW)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            await _login(c)
            yield c
    get_settings.cache_clear()


def add(
    dsn: str,
    message: str,
    *,
    level: int = logging.INFO,
    source: str = "worker",
    logger: str = "t.jobs",
    exc: str | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "insert into app_log (source, level, logger, message, exc, context)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (source, level, logger, message, exc, Jsonb(context) if context else None),
        ).fetchone()
    assert row
    return int(row[0])


async def ids(c: httpx.AsyncClient, **params: Any) -> list[int]:
    # Every listing here filters to the test's own loggers: the api's own handler may
    # write lines of its own while the app is up.
    params.setdefault("logger", "t.")
    r = await c.get(URL, params=params)
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


async def test_off_and_login(
    clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build(clean_db, tmp_path, monkeypatch, None)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            assert (await c.get(URL)).status_code == 404
            assert (await c.get(f"{URL}/1")).status_code == 404
    get_settings.cache_clear()
    (tmp_path / "on").mkdir()
    app = _build(clean_db, tmp_path / "on", monkeypatch, PW)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            r = await c.get(URL)
            assert r.status_code == 401
            assert r.headers["cache-control"] == "no-store"
            assert (await c.get(f"{URL}/1")).status_code == 401
    get_settings.cache_clear()


async def test_empty_and_defaults(client: httpx.AsyncClient) -> None:
    r = await client.get(URL, params={"logger": "t."})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "items": [],
        "next": None,
        "dropped": 0,
        "level": "info",
        "keep_days": 14,
        "levels": ["debug", "info", "warning", "error"],
    }


async def test_filters(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, "debug line", level=logging.DEBUG, context={"job_id": 5})
    b = add(clean_db, "warned", level=logging.WARNING, source="api", logger="t.api.x")
    c = add(
        clean_db,
        "it broke",
        level=logging.ERROR,
        exc="Traceback\nValueError: Unterminated String",
        context={"job_id": 5, "image_id": 9, "purpose": "haiku"},
    )
    d = add(clean_db, "100% done", logger="t_other", context={"job_id": 50, "image_id": 90})
    assert await ids(client) == [c, b, a]
    assert await ids(client, logger="t") == [d, c, b, a]
    # A LIKE wildcard in the prefix is a literal.
    assert await ids(client, logger="t_") == [d]
    assert await ids(client, level="warning") == [c, b]
    assert await ids(client, level="error") == [c]
    assert await ids(client, level="critical") == []
    assert await ids(client, source="api") == [b]
    assert await ids(client, source="worker") == [c, a]
    assert await ids(client, q="BROKE") == [c]
    # The traceback is searched too.
    assert await ids(client, q="unterminated string") == [c]
    assert await ids(client, q="%", logger="t") == [d]
    assert await ids(client, job=5) == [c, a]
    assert await ids(client, image=9) == [c]
    assert await ids(client, job=5, level="error", image=9) == [c]
    for bad in ({"level": "verbose"}, {"source": "cron"}, {"job": "x"}, {"limit": 501}):
        assert (await client.get(URL, params=bad)).status_code == 422


async def test_payload_and_detail(client: httpx.AsyncClient, clean_db: str) -> None:
    lid = add(
        clean_db,
        "invalid haiku output\nraw: {",
        level=logging.WARNING,
        exc="Traceback (most recent call last):\n  boom",
        context={"job_id": 3, "stop_reason": "refusal"},
    )
    add(clean_db, "custom level", level=25)
    r = await client.get(URL, params={"logger": "t."})
    first, second = r.json()["items"]
    assert first["level"] == "level 25"
    assert second == {
        "id": lid,
        "at": second["at"],
        "source": "worker",
        "level": "warning",
        "logger": "t.jobs",
        "message": "invalid haiku output\nraw: {",
        "has_exc": True,
        "context": {"job_id": 3, "stop_reason": "refusal"},
    }
    r = await client.get(f"{URL}/{lid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["exc"] == "Traceback (most recent call last):\n  boom"
    assert detail["message"] == "invalid haiku output\nraw: {"
    assert detail["context"]["stop_reason"] == "refusal"
    assert (await client.get(f"{URL}/{lid + 1000}")).status_code == 404


async def test_paging(client: httpx.AsyncClient, clean_db: str) -> None:
    made = [add(clean_db, f"line {i}") for i in range(5)]
    r = (await client.get(URL, params={"logger": "t.", "limit": 2})).json()
    assert [i["id"] for i in r["items"]] == made[:-3:-1]
    assert r["next"] == made[3]
    r = (await client.get(URL, params={"logger": "t.", "limit": 2, "before": r["next"]})).json()
    assert [i["id"] for i in r["items"]] == [made[2], made[1]]
    r = (await client.get(URL, params={"logger": "t.", "limit": 2, "before": r["next"]})).json()
    assert [i["id"] for i in r["items"]] == [made[0]]
    assert r["next"] is None


async def test_dropped_and_settings(client: httpx.AsyncClient, clean_db: str) -> None:
    add(clean_db, "dropped 3 log lines", logger=logs.DROP_LOGGER, context={"dropped": 3})
    add(
        clean_db,
        "dropped 4 log lines",
        source="api",
        logger=logs.DROP_LOGGER,
        context={"dropped": 4},
    )
    r = await client.put(
        "/api/admin/models", json={"key": "log.level", "value": "debug"}, headers=XHR
    )
    assert r.status_code == 200 and r.json() == {"key": "log.level", "value": "debug"}
    r = await client.put(
        "/api/admin/models", json={"key": "log.keep_days", "value": 30}, headers=XHR
    )
    assert r.status_code == 200
    for bad in ({"key": "log.level", "value": "loud"}, {"key": "log.keep_days", "value": 0}):
        assert (await client.put("/api/admin/models", json=bad, headers=XHR)).status_code == 422
    body = (await client.get(URL, params={"logger": "t."})).json()
    assert body["dropped"] == 7
    assert body["level"] == "debug" and body["keep_days"] == 30
    models = (await client.get("/api/admin/models")).json()
    assert models["current"]["log.level"] == "debug"
    assert models["default"]["log.keep_days"] == 14
    assert (await client.delete("/api/admin/models/log.level")).status_code == 200
    assert (await client.get(URL)).json()["level"] == "info"


async def test_api_lines_reach_the_table(client: httpx.AsyncClient, clean_db: str) -> None:
    assert logs.installed() is not None
    logging.getLogger("penish.api.test").warning("from the api")

    def found() -> list[Any]:
        with psycopg.connect(clean_db, autocommit=True) as conn:
            return conn.execute(
                "select source from app_log where message = 'from the api'"
            ).fetchall()

    for _ in range(200):
        if found():
            break
        await asyncio.sleep(0.02)
    assert found() == [("api",)]


async def test_after_returns_only_newer(client: httpx.AsyncClient, clean_db: str) -> None:
    made = [add(clean_db, f"line {i}") for i in range(4)]
    assert await ids(client, after=made[1]) == [made[3], made[2]]
    assert await ids(client, after=made[3]) == []
    # A full page of newer lines says there may be a gap below it.
    r = (await client.get(URL, params={"logger": "t.", "after": made[0], "limit": 2})).json()
    assert [i["id"] for i in r["items"]] == [made[3], made[2]]
    assert r["next"] == made[2]


async def test_drop_sum_uses_the_partial_index(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("set enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute(f"explain {admin_logs._DROPS_SQL}"))
    assert "app_log_drops_idx" in plan


async def test_dropped_includes_this_process_unreported(
    client: httpx.AsyncClient, clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(clean_db, "dropped 2 log lines", logger=logs.DROP_LOGGER, context={"dropped": 2})
    # Lines the api's own handler lost and has not yet written a report row for.
    monkeypatch.setattr(logs, "installed", lambda: SimpleNamespace(dropped=5))
    assert (await client.get(URL, params={"logger": "t."})).json()["dropped"] == 7
