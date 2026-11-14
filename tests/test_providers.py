import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
import pyvips

from penish import runtime
from penish.config import Settings, get_settings
from penish.worker import jobs, providers
from penish.worker.describe import DescribeError, call_text, output_config
from penish.worker.heartbeat import beat
from penish.worker.jobs import Job, SoftError
from tests.test_admin_ops import client, sql  # noqa: F401  (client is a fixture)

KEYS = Settings(
    anthropic_api_key="ak", openai_api_key="ok", openrouter_api_key="rk", xai_api_key="xk"
)
SCHEMA = {
    "type": "object",
    "properties": {"caption": {"type": "string"}},
    "required": ["caption"],
    "additionalProperties": False,
}


def webp() -> bytes:
    out: bytes = pyvips.Image.black(8, 8).write_to_buffer(".webp")
    return out


def kwargs(effort: str | None = None, *, image: bytes | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if image is not None:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/webp",
                    "data": base64.b64encode(image).decode(),
                },
            }
        )
    content.append({"type": "text", "text": "Return only these keys: caption."})
    return {
        "max_tokens": 8000,
        "system": "be literal",
        "output_config": output_config(SCHEMA, effort),
        "messages": [{"role": "user", "content": content}],
    }


class Wire:
    """A MockTransport that records requests and answers with a canned reply."""

    def __init__(self, status: int = 200, body: Any = None, exc: Exception | None = None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, json=self.body)

    @property
    def sent(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.requests[-1].content)
        return out


RESPONSES_OK = {
    "output": [
        {"type": "reasoning", "summary": []},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": '{"caption": "a stove"}'}],
        },
    ],
    "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
}
CHAT_OK = {
    "choices": [{"message": {"role": "assistant", "content": '{"caption": "a stove"}'}}],
    "usage": {"prompt_tokens": 90, "completion_tokens": 12, "total_tokens": 102},
}


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    def make(**kw: Any) -> Wire:
        w = Wire(**kw)
        monkeypatch.setattr(providers, "transport", httpx.MockTransport(w))
        return w

    monkeypatch.setattr(providers, "get_settings", lambda: KEYS)
    return make


async def call(model: str, kw: dict[str, Any], via: Any = None) -> tuple[str, list[Any]]:
    seen: list[Any] = []

    async def record(m: str, usage: Any) -> None:
        seen.append((m, usage.input_tokens, usage.output_tokens))

    text = await call_text(via, model=model, create_kwargs=kw, record=record)
    return text, seen


# -- ids ------------------------------------------------------------------


def test_split_model() -> None:
    assert runtime.split_model("claude-sonnet-5-5") == (None, "claude-sonnet-5-5")
    assert runtime.split_model("openai:gpt-6-astra") == ("openai", "gpt-6-astra")
    # OpenRouter variants carry a colon of their own.
    assert runtime.split_model("openrouter:x-ai/grok-4.7:batch") == (
        "openrouter",
        "x-ai/grok-4.7:batch",
    )
    with pytest.raises(ValueError, match="unknown provider"):
        runtime.split_model("mistral:large")
    with pytest.raises(ValueError, match="no model id"):
        runtime.split_model("xai: ")
    assert runtime.split_model("openai: gpt-6-astra") == ("openai", "gpt-6-astra")


def test_validate_keys() -> None:
    assert runtime.validate("extract.model", " openai:gpt-6-luna ") == "openai:gpt-6-luna"
    # Whitespace after the prefix is stripped too, or every call would send " gpt-6-luna".
    assert runtime.validate("describe.model", "openai: gpt-6-luna") == "openai:gpt-6-luna"
    with pytest.raises(ValueError):
        runtime.validate("name.model", "bogus:thing")
    assert runtime.validate("extract.effort", "max") == "max"
    assert runtime.validate("extract.effort", None) is None
    with pytest.raises(ValueError):
        runtime.validate("extract.effort", "extreme")
    assert runtime.validate("mycelium.daily_calls", 0) == 0
    for bad in (-1, 100_001, True, "12", 1.5):
        with pytest.raises(ValueError):
            runtime.validate("mycelium.daily_calls", bad)
    assert runtime.default("mycelium.daily_calls") == 400
    assert runtime.default("extract.model") == runtime.default("describe.model")
    assert runtime.default("name.model") == runtime.default("describe.model")
    assert runtime.default("extract.effort") is None
    assert "subject" in runtime.default("extract.prompt")
    assert "\u2014" not in runtime.default("extract.prompt") + runtime.default("name.prompt")
    assert {"extract", "name"} <= set(runtime.PURPOSES)


def test_keyed() -> None:
    assert providers.keyed(KEYS) == ["anthropic", "openai", "openrouter", "xai"]
    only = Settings(
        anthropic_api_key=None, openai_api_key=None, openrouter_api_key=None, xai_api_key="x"
    )
    assert providers.keyed(only) == ["xai"]


def test_effort_mapping() -> None:
    m = providers.map_effort
    assert m("openai", "gpt-6-astra", "max") == "max"
    assert m("openai", "gpt-5.2", "max") == "high"
    assert m("openai", "o4-mini", "xhigh") == "high"
    assert m("openai", "gpt-4.1", "high") is None
    assert m("xai", "grok-4.7", "max") == "xhigh"
    assert m("xai", "grok-4.5", "xhigh") == "high"
    assert m("xai", "grok-4.20-0309-reasoning", "high") is None
    assert m("openrouter", "anything/at-all", "low") == "low"
    assert m("openai", "gpt-6-astra", None) is None


# -- request shapes -------------------------------------------------------


async def test_openai_request(wire: Any) -> None:
    w = wire(body=RESPONSES_OK)
    img = webp()
    text, seen = await call("openai:gpt-6-astra", kwargs("max", image=img))
    req = w.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == "Bearer ok"
    body = w.sent
    assert body["model"] == "gpt-6-astra"
    assert body["instructions"] == "be literal"
    assert body["store"] is False
    assert body["max_output_tokens"] == 8000
    assert body["reasoning"] == {"effort": "max"}
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "output",
        "schema": SCHEMA,
        "strict": True,
    }
    parts = body["input"][0]["content"]
    # OpenAI reads webp, so the describer's bytes go as they are.
    assert parts[0] == {
        "type": "input_image",
        "image_url": "data:image/webp;base64," + base64.b64encode(img).decode(),
    }
    assert parts[1] == {"type": "input_text", "text": "Return only these keys: caption."}
    assert text == '{"caption": "a stove"}'
    assert seen == [("openai:gpt-6-astra", 120, 30)]


async def test_openai_no_effort_on_non_reasoning(wire: Any) -> None:
    w = wire(body=RESPONSES_OK)
    await call("openai:gpt-4.1", kwargs("high"))
    assert "reasoning" not in w.sent


async def test_xai_request_converts_webp(wire: Any) -> None:
    w = wire(body=RESPONSES_OK)
    await call("xai:grok-4.7", kwargs("max", image=webp()))
    req = w.requests[0]
    assert str(req.url) == "https://api.x.ai/v1/responses"
    assert req.headers["authorization"] == "Bearer xk"
    body = w.sent
    assert body["reasoning"] == {"effort": "xhigh"}
    url = body["input"][0]["content"][0]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:2] == b"\xff\xd8"


async def test_openrouter_request(wire: Any) -> None:
    w = wire(body=CHAT_OK)
    text, seen = await call("openrouter:google/gemini-4-carbon", kwargs("medium", image=webp()))
    req = w.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer rk"
    # The optional attribution headers would name the site.
    assert "http-referer" not in req.headers and "x-title" not in req.headers
    body = w.sent
    assert body["model"] == "google/gemini-4-carbon"
    assert body["messages"][0] == {"role": "system", "content": "be literal"}
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[1] == {"type": "text", "text": "Return only these keys: caption."}
    assert body["max_tokens"] == 8000
    assert body["reasoning"] == {"effort": "medium"}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "output", "strict": True, "schema": SCHEMA},
    }
    assert text == '{"caption": "a stove"}'
    assert seen == [("openrouter:google/gemini-4-carbon", 90, 12)]


async def test_system_blocks_and_plain_content(wire: Any) -> None:
    w = wire(body=CHAT_OK)
    kw = {
        "max_tokens": 100,
        "system": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        "output_config": output_config(SCHEMA, None),
        "messages": [{"role": "user", "content": "Write 3 haikus."}],
    }
    await call("openrouter:qwen/qwen3.8-max-prime", kw)
    body = w.sent
    assert body["messages"] == [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "Write 3 haikus."},
    ]
    assert "reasoning" not in body


async def test_unknown_option_refused(wire: Any) -> None:
    wire(body=CHAT_OK)
    kw = kwargs() | {"thinking": {"type": "enabled"}}
    with pytest.raises(DescribeError) as e:
        await call("openrouter:a/b", kw)
    assert not e.value.retryable


# -- responses ------------------------------------------------------------


async def test_refusal_and_empty_mean_no_text(wire: Any) -> None:
    wire(
        body={
            "output": [
                {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )
    text, seen = await call("openai:gpt-6-luna", kwargs())
    assert text == "" and seen == [("openai:gpt-6-luna", 5, 1)]


async def test_chat_missing_usage_counts_zero(wire: Any) -> None:
    wire(body={"choices": [{"message": {"content": None}}]})
    text, seen = await call("openrouter:a/b", kwargs())
    assert text == "" and seen == [("openrouter:a/b", 0, 0)]


# -- errors ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (503, True), (400, False), (401, False)],
)
async def test_status_errors(wire: Any, status: int, retryable: bool) -> None:
    wire(status=status, body={"error": {"message": "the provider says no", "type": "x"}})
    with pytest.raises(DescribeError) as e:
        await call("openai:gpt-6-astra", kwargs(image=webp()))
    msg = str(e.value)
    assert e.value.retryable is retryable
    assert msg == f"openai {status}: the provider says no"
    # Neither the key nor the request body goes into a job's last_error.
    assert "ok" not in msg.replace("openai", "") and "be literal" not in msg


async def test_xai_string_error(wire: Any) -> None:
    wire(status=400, body={"code": "bad", "error": "Model not found: grok-9"})
    with pytest.raises(DescribeError) as e:
        await call("xai:grok-9", kwargs())
    assert str(e.value) == "xai 400: Model not found: grok-9" and not e.value.retryable


async def test_openrouter_error_in_200(wire: Any) -> None:
    wire(body={"error": {"code": 502, "message": "upstream down"}})
    with pytest.raises(DescribeError) as e:
        await call("openrouter:a/b", kwargs())
    assert e.value.retryable and "upstream down" in str(e.value)


async def test_openrouter_timeout_in_200(wire: Any) -> None:
    wire(body={"error": {"code": 408, "message": "timed out"}})
    with pytest.raises(DescribeError) as e:
        await call("openrouter:a/b", kwargs())
    assert e.value.retryable and str(e.value) == "openrouter 408: timed out"


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("x")],
)
async def test_transport_errors_retry(wire: Any, exc: Exception) -> None:
    wire(exc=exc)
    with pytest.raises(DescribeError) as e:
        await call("xai:grok-4.7", kwargs())
    assert e.value.retryable


async def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    w = Wire(body=RESPONSES_OK)
    monkeypatch.setattr(providers, "transport", httpx.MockTransport(w))
    monkeypatch.setattr(providers, "get_settings", lambda: Settings(openai_api_key=None))
    with pytest.raises(DescribeError) as e:
        await call("openai:gpt-6-astra", kwargs())
    assert str(e.value) == "no key for openai" and not e.value.retryable
    assert w.requests == []


async def test_unknown_prefix() -> None:
    with pytest.raises(DescribeError) as e:
        await call("mistral:large", kwargs(), via=object())
    assert not e.value.retryable


# -- the seam -------------------------------------------------------------


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        )


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


async def test_bare_id_uses_given_client(wire: Any) -> None:
    w = wire(body=RESPONSES_OK)
    fake = FakeClient()
    kw = kwargs("high")
    text, _ = await call("claude-sonnet-5-5", kw, via=fake)
    assert text == "hi" and w.requests == []
    # The Anthropic request goes out exactly as built.
    assert fake.messages.calls == [{"model": "claude-sonnet-5-5", **kw}]


async def test_prefixed_id_replaces_anthropic_client(wire: Any) -> None:
    w = wire(body=RESPONSES_OK)
    real = anthropic.AsyncAnthropic(api_key="unused")
    text, _ = await call("openai:gpt-6-astra", kwargs(), via=real)
    assert text == '{"caption": "a stove"}' and len(w.requests) == 1


async def test_prefixed_id_keeps_a_test_fake(wire: Any) -> None:
    # Tests inject fakes to stay off the network; a provider id must not undo that.
    w = wire(body=RESPONSES_OK)
    fake = FakeClient()
    text, _ = await call("openai:gpt-6-astra", kwargs(), via=fake)
    assert text == "hi" and w.requests == []


# -- daily cap ------------------------------------------------------------

NOW = datetime(2026, 11, 14, 15, 30, tzinfo=UTC)


async def usage(pool: Any, purpose: str, at: datetime) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "insert into api_usage (purpose, model, input_tokens, output_tokens, at)"
            " values (%s, 'm', 1, 1, %s)",
            (purpose, at),
        )


async def test_daily_cap(pool: Any) -> None:
    await runtime.put(pool, "mycelium.daily_calls", 2)
    await usage(pool, "extract", NOW - timedelta(days=1))
    await usage(pool, "describe", NOW - timedelta(hours=1))
    await usage(pool, "haiku", NOW - timedelta(hours=1))
    await usage(pool, "extract", NOW - timedelta(hours=1))
    # Yesterday's call and the uncapped purposes do not count.
    assert await runtime.calls_today(pool, NOW) == 1
    await providers.check_daily_cap(pool, now=NOW)

    await usage(pool, "name", NOW - timedelta(minutes=5))
    with pytest.raises(SoftError) as e:
        await providers.check_daily_cap(pool, now=NOW)
    assert e.value.run_after == datetime(2026, 11, 15, tzinfo=UTC)

    await runtime.put(pool, "mycelium.daily_calls", 0)
    with pytest.raises(SoftError):
        await providers.check_daily_cap(pool, now=NOW + timedelta(days=3))


async def test_deferred_soft_error_keeps_attempts(pool: Any) -> None:
    job_id = await jobs.enqueue(pool, "mycelium_extract", {"image_id": 1})
    assert job_id is not None
    job = await jobs.claim(pool)
    assert job is not None and job.attempts == 1
    when = datetime(2030, 1, 1, tzinfo=UTC)

    async def capped(_: Job) -> None:
        raise SoftError("daily cap 400/400", run_after=when)

    await jobs._run_one(pool, job, capped)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select status::text as s, attempts, run_after, last_error from jobs where id = %s",
            (job_id,),
        )
        row = await cur.fetchone()
    assert row["s"] == "queued" and row["attempts"] == 0
    assert row["run_after"] == when and "daily cap" in row["last_error"]


# -- heartbeat and admin --------------------------------------------------


async def test_heartbeat_reports_providers(pool: Any) -> None:
    await beat(pool, version="v", providers=providers.keyed(KEYS))
    async with pool.connection() as conn:
        cur = await conn.execute("select info from heartbeats")
        row = await cur.fetchone()
    assert row["info"]["providers"] == ["anthropic", "openai", "openrouter", "xai"]
    assert "ak" not in json.dumps(row["info"])


async def test_models_endpoint(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    from tests.test_admin import XHR

    body = (await client.get("/api/admin/models")).json()
    assert body["keyed"] is None and body["calls_today"] == 0
    assert body["providers"] == ["anthropic", "openai", "openrouter", "xai"]
    assert {"extract", "name"} <= set(body["purposes"])
    for k in ("extract.model", "extract.effort", "extract.prompt", "name.model", "name.prompt"):
        assert k in body["current"]
    assert body["current"]["mycelium.daily_calls"] == 400
    assert "openai:gpt-6-astra" in body["known_models"]

    sql(
        clean_db,
        "insert into heartbeats (name, info) values ('worker', %s)",
        json.dumps({"queue": 0, "providers": ["anthropic", "xai"]}),
    )
    sql(clean_db, "insert into api_usage (purpose, model) values ('extract', 'm')")
    body = (await client.get("/api/admin/models")).json()
    assert body["keyed"] == ["anthropic", "xai"] and body["calls_today"] == 1

    r = await client.put(
        "/api/admin/models",
        json={"key": "extract.model", "value": "openrouter:a/b:free"},
        headers=XHR,
    )
    assert r.status_code == 200
    body = (await client.get("/api/admin/models")).json()
    assert "openrouter:a/b:free" in body["known_models"]
    r = await client.put(
        "/api/admin/models", json={"key": "name.model", "value": "nope:x"}, headers=XHR
    )
    assert r.status_code == 422 and "unknown provider" in r.json()["detail"]
    r = await client.put(
        "/api/admin/models", json={"key": "mycelium.daily_calls", "value": 50}, headers=XHR
    )
    assert r.status_code == 200 and r.json()["value"] == 50
    r = await client.put(
        "/api/admin/models", json={"key": "mycelium.daily_calls", "value": -1}, headers=XHR
    )
    assert r.status_code == 422
    get_settings.cache_clear()
