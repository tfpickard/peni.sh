import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import numpy as np
import pytest

from penish import runtime
from penish.api.embed_client import EmbedUnavailable, embed_query, embed_query_full
from penish.config import Settings
from penish.worker import embed_server
from penish.worker.describe import describe
from penish.worker.grow import grow_into
from penish.worker.haiku import haiku_from
from penish.worker.heartbeat import beat
from penish.worker.metrics import refresh_corpus_stats

SETTINGS = Settings(anthropic_api_key="test")
TAGS = ["cat", "kitchen", "photo", "warm-light", "tiled-floor", "orange", "indoor"]


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


class FakeMessages:
    def __init__(self, replies: list[str], usage: tuple[int, int] = (120, 30)) -> None:
        self.replies = list(replies)
        self.usage = usage
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.replies.pop(0))],
            usage=SimpleNamespace(input_tokens=self.usage[0], output_tokens=self.usage[1]),
        )


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self.messages = FakeMessages(list(replies))


async def q(p: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    async with p.connection() as conn:
        cur = await conn.execute(sql, args)
        return cast(list[dict[str, Any]], await cur.fetchall())


# -- migration ------------------------------------------------------------


async def test_migration_tables_exist(p: Any) -> None:
    rows = await q(
        p,
        "select table_name from information_schema.tables where table_schema = 'public'"
        " and table_name = any(%s)",
        [
            "settings",
            "votes",
            "comments",
            "api_usage",
            "heartbeats",
            "corpus_stats_history",
        ],
    )
    assert len(rows) == 6
    cols = await q(
        p,
        "select column_name, is_nullable from information_schema.columns"
        " where table_name = 'images' and column_name in ('crop', 'pub_sha')",
    )
    assert {c["column_name"]: c["is_nullable"] for c in cols} == {"crop": "YES", "pub_sha": "NO"}


# -- runtime --------------------------------------------------------------


async def test_get_put_reset(p: Any) -> None:
    assert await runtime.get(p, "describe.model") == runtime.default("describe.model")
    assert await runtime.get(p, "describe.effort") is None
    assert "{examples}" in await runtime.get(p, "haiku.prompt")

    await runtime.put(p, "describe.model", "  claude-other  ")
    await runtime.put(p, "describe.effort", "high")
    await runtime.put(p, "grow.prompt", "grow {n} like {examples}")
    assert await runtime.get(p, "describe.model") == "claude-other"
    assert await runtime.get(p, "describe.effort") == "high"
    assert await runtime.get(p, "grow.prompt") == "grow {n} like {examples}"
    await runtime.put(p, "describe.model", "claude-third")
    assert await runtime.get(p, "describe.model") == "claude-third"

    await runtime.reset(p, "describe.model")
    assert await runtime.get(p, "describe.model") == runtime.default("describe.model")
    await runtime.put(p, "describe.effort", None)
    assert await runtime.get(p, "describe.effort") is None


async def test_put_validates(p: Any) -> None:
    with pytest.raises(ValueError):
        await runtime.put(p, "describe.effort", "extreme")
    with pytest.raises(ValueError):
        await runtime.put(p, "haiku.prompt", "   ")
    with pytest.raises(KeyError):
        await runtime.put(p, "nope", "x")
    with pytest.raises(KeyError):
        await runtime.get(p, "nope")


# -- describe / haiku -----------------------------------------------------


async def test_describe_reads_runtime_and_records_usage(p: Any) -> None:
    await runtime.put(p, "describe.model", "claude-override")
    await runtime.put(p, "describe.effort", "low")
    await runtime.put(p, "describe.prompt", "describe it flatly")
    client = FakeClient("nope", json.dumps({"caption": "a cat"}))
    out = await describe(
        b"x", "image/png", want={"caption"}, settings=SETTINGS, pool=p, client=client
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-override" and out.model == "claude-override"
    assert call["system"] == "describe it flatly"
    assert call["output_config"]["effort"] == "low"
    # Both calls were paid for, including the one that failed validation.
    rows = await q(p, "select purpose, model, input_tokens, output_tokens from api_usage")
    assert (
        rows
        == [
            {
                "purpose": "describe",
                "model": "claude-override",
                "input_tokens": 120,
                "output_tokens": 30,
            }
        ]
        * 2
    )


async def test_describe_omits_effort_by_default() -> None:
    client = FakeClient(json.dumps({"caption": "a cat"}))
    await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)
    assert "effort" not in client.messages.calls[0]["output_config"]


async def test_describe_with_stub_getter() -> None:
    values = {"describe.model": "m-stub", "describe.effort": "max", "describe.prompt": "P"}

    async def get(key: str) -> Any:
        return values[key]

    client = FakeClient(json.dumps({"tags": TAGS}))
    out = await describe(
        b"x", "image/png", want={"tags"}, settings=SETTINGS, get=get, client=client
    )
    call = client.messages.calls[0]
    assert (call["model"], call["system"], call["output_config"]["effort"]) == (
        "m-stub",
        "P",
        "max",
    )
    assert out.model == "m-stub"


async def test_haiku_reads_runtime_examples_from_db_and_records_usage(p: Any) -> None:
    async with p.connection() as conn:
        await conn.execute("update haikus set active = false")
        await conn.execute(
            "insert into haikus (l1, l2, l3, source) values"
            " ('owner one', 'owner two', 'owner three', 'owner')"
        )
    try:
        await runtime.put(p, "haiku.model", "claude-h")
        await runtime.put(p, "haiku.prompt", "voice:\n{examples}\nend")
        client = FakeClient(json.dumps({"lines": ["a", "b", "c"]}))
        assert await haiku_from("desc", settings=SETTINGS, pool=p, client=client) == ("a", "b", "c")
        call = client.messages.calls[0]
        assert call["model"] == "claude-h"
        # Only the active deck is sampled, so the inactive seeds stay out.
        assert call["system"] == "voice:\nowner one\nowner two\nowner three\nend"
        rows = await q(p, "select purpose, model from api_usage")
        assert rows == [{"purpose": "haiku", "model": "claude-h"}]
    finally:
        async with p.connection() as conn:
            await conn.execute("update haikus set active = true")


async def test_haiku_samples_six_active(p: Any) -> None:
    client = FakeClient(json.dumps({"lines": ["a", "b", "c"]}))
    await haiku_from("desc", settings=SETTINGS, pool=p, client=client)
    body = client.messages.calls[0]["system"].split("Examples of the voice:\n\n")[1]
    examples = body.split("\n\nRespond")[0].split("\n\n")
    assert len(examples) == 6 and all(len(e.split("\n")) == 3 for e in examples)


async def test_grow_into_uses_runtime_and_records_usage(p: Any) -> None:
    await runtime.put(p, "grow.model", "claude-g")
    await runtime.put(p, "grow.prompt", "make {n}:\n{examples}")
    h = ["a gill on the wrist", "the dentist admires it", "twice a year, fine"]
    client = FakeClient(json.dumps({"haikus": [h]}))
    stats = await grow_into(p, 1, settings=SETTINGS, client=client)
    assert stats.accepted == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-g" and call["system"].startswith("make 1:\n")
    assert (await q(p, "select source::text as s from haikus where l1 = %s", h[0])) == [
        {"s": "model"}
    ]
    assert (await q(p, "select purpose from api_usage")) == [{"purpose": "grow"}]


# -- heartbeat and history ------------------------------------------------


async def test_heartbeat_upserts(p: Any) -> None:
    async with p.connection() as conn:
        await conn.execute("insert into jobs (type) values ('haiku'), ('haiku')")
    await beat(p, version="abc123")
    first = await q(p, "select name, at, info from heartbeats")
    assert len(first) == 1 and first[0]["info"] == {"version": "abc123", "queue": 2}
    await beat(p, version="def456")
    second = await q(p, "select name, at, info from heartbeats")
    assert len(second) == 1 and second[0]["info"]["version"] == "def456"
    assert second[0]["at"] >= first[0]["at"]


async def test_corpus_stats_history_appends(p: Any) -> None:
    await refresh_corpus_stats(p, kind="description", model="m")
    await refresh_corpus_stats(p, kind="description", model="m")
    rows = await q(p, "select kind, model, n, temperature from corpus_stats_history")
    assert rows == [{"kind": "description", "model": "m", "n": 0, "temperature": None}] * 2


# -- embed server ---------------------------------------------------------


class FakeQueryEmbedder:
    model_name = "test/embed"
    dim = 4

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def embed_query(self, text: str) -> np.ndarray:
        self.seen.append(text)
        return np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)


async def test_embed_server_and_client() -> None:
    fake = FakeQueryEmbedder()
    transport = httpx.ASGITransport(app=embed_server.make_app(fake))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker:8001") as c:
        r = await c.post("/embed", json={"text": "  moss  "})
        assert r.json() == {"vector": [0.5] * 4, "model": "test/embed", "dim": 4}
        assert fake.seen == ["moss"]
        assert (await c.get("/healthz")).json() == {"ok": True}
        for bad in ({"text": ""}, {"text": 3}, {}, {"text": "x" * 2001}):
            assert (await c.post("/embed", json=bad)).status_code == 400
        assert (await c.post("/embed", content=b"{nope")).status_code == 400

        assert await embed_query("moss", client=c) == [0.5] * 4
        full = await embed_query_full("moss", client=c)
        assert full["model"] == "test/embed" and full["dim"] == 4
        with pytest.raises(EmbedUnavailable):
            await embed_query("", client=c)


async def test_embed_client_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from penish.config import get_settings

    monkeypatch.setenv("EMBED_SERVER_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    try:
        with pytest.raises(EmbedUnavailable):
            await embed_query("moss")
    finally:
        get_settings.cache_clear()


async def test_embed_server_stops_on_event() -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        embed_server.serve(FakeQueryEmbedder(), stop, host="127.0.0.1", port=0)
    )
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_xhigh_effort_and_grow_model(p: Any) -> None:
    await runtime.put(p, "describe.effort", "xhigh")
    assert await runtime.get(p, "describe.effort") == "xhigh"
    assert await runtime.get(p, "grow.model") == runtime.default("describe.model")
    await runtime.put(p, "grow.model", " claude-grow ")
    assert await runtime.get(p, "grow.model") == "claude-grow"
    # Splitting grow off leaves describe alone.
    assert await runtime.get(p, "describe.model") == runtime.default("describe.model")
    await runtime.reset(p, "grow.model")
    assert await runtime.get(p, "grow.model") == runtime.default("describe.model")
    with pytest.raises(ValueError):
        runtime.validate("grow.model", "  ")
