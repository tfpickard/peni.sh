"""What a model call leaves behind when it goes wrong: stop reasons, refusals, the raw text
in WARNING records and error messages, and DEBUG records with no keys or image bytes."""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from penish import logs, runtime
from penish.config import Settings
from penish.worker import jobs, providers
from penish.worker.admin_jobs import _soft_or_hard
from penish.worker.categorize import classify
from penish.worker.describe import DescribeError, Reply, call_text, describe, output_config
from penish.worker.facet_jobs import extract
from penish.worker.grow import grow
from penish.worker.haiku import haiku_from
from penish.worker.jobs import HardError, Job
from penish.worker.name_jobs import Inputs, name_gallery

MODEL = "claude-haiku-5-5"
SETTINGS = Settings(anthropic_api_key="test")
SECRET = "sk-test-do-not-log"
KEYS = Settings(
    anthropic_api_key=SECRET, openai_api_key=SECRET, openrouter_api_key=SECRET, xai_api_key=SECRET
)
HAIKU = ["moss on the kettle", "it boils water anyway", "tea at four as planned"]
LOGGER = "penish.worker"


def reply(text: str, stop_reason: str | None = "end_turn", **extra: Any) -> Any:
    out = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] if text else [],
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
        **extra,
    )
    if stop_reason is not None:
        out.stop_reason = stop_reason
    return out


class FakeMessages:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.replies.pop(0)


class FakeClient:
    def __init__(self, *replies: Any) -> None:
        self.messages = FakeMessages(list(replies))


async def get(key: str) -> Any:
    if key.endswith(".model"):
        return MODEL
    return runtime.default(key)


def kwargs(image: bytes | None = None) -> dict[str, Any]:
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
    content.append({"type": "text", "text": "Return the JSON object."})
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    return {
        "max_tokens": 4096,
        "system": "be literal",
        "output_config": output_config(schema, None),
        "messages": [{"role": "user", "content": content}],
    }


def texts(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


# -- refusal ----------------------------------------------------------------


async def test_refusal_raises_hard_and_logs_partial_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    client = FakeClient(reply('{"drafts": ["half a', "refusal"))
    with pytest.raises(DescribeError) as e:
        await call_text(client, model=MODEL, create_kwargs=kwargs(), purpose="haiku")
    assert str(e.value) == f"model refused ({MODEL})"
    assert not e.value.retryable
    [warning] = texts(caplog, logging.WARNING)
    assert "refused" in warning and MODEL in warning and '{"drafts": ["half a' in warning


async def test_refusal_record_carries_model_context(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[dict[str, Any]] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(logs.current_context())

    grab = Grab(logging.WARNING)
    logging.getLogger("penish.worker.describe").addHandler(grab)
    try:
        with pytest.raises(DescribeError):
            await call_text(
                FakeClient(reply("", "refusal")), model=MODEL, create_kwargs=kwargs(), purpose="x"
            )
    finally:
        logging.getLogger("penish.worker.describe").removeHandler(grab)
    assert seen == [{"purpose": "x", "model": MODEL, "provider": "anthropic"}]


async def test_haiku_refusal_is_not_retried() -> None:
    client = FakeClient(reply("", "refusal"), reply(json.dumps({"drafts": [], "lines": HAIKU})))
    with pytest.raises(DescribeError) as e:
        await haiku_from("a kettle", settings=SETTINGS, get=get, client=client, examples=[])
    assert not e.value.retryable
    assert len(client.messages.calls) == 1


async def test_refusal_fails_the_job_on_its_first_attempt(pool: object) -> None:
    p = cast(Any, pool)
    await jobs.enqueue(p, "haiku", {"image_id": 1})
    job = await jobs.claim(p, ["haiku"])
    assert job is not None and job.attempts == 1

    async def handler(job: Job) -> None:
        client = FakeClient(reply("", "refusal"))
        try:
            await haiku_from("a kettle", settings=SETTINGS, get=get, client=client, examples=[])
        except DescribeError as e:
            raise _soft_or_hard("haiku", e) from e

    await jobs._run_one(p, job, handler)
    async with p.connection() as conn:
        cur = await conn.execute(
            "select status, attempts, last_error from jobs where id = %s", (job.id,)
        )
        row = await cur.fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1
    assert f"model refused ({MODEL})" in row["last_error"]
    assert "HardError" in row["last_error"]


def test_refusal_maps_to_hard_error() -> None:
    err = _soft_or_hard("haiku", DescribeError(f"model refused ({MODEL})", retryable=False))
    assert isinstance(err, HardError)


# -- validation failures ----------------------------------------------------


async def test_haiku_validation_failures_log_raw_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    first = '{"drafts": ["a"], "lines": ["one", "two"]}'
    second = "not json at all"
    client = FakeClient(reply(first), reply(second))
    with pytest.raises(DescribeError) as e:
        await haiku_from("a kettle", settings=SETTINGS, get=get, client=client, examples=[])
    assert e.value.retryable
    warnings = texts(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert "attempt 1" in warnings[0] and first in warnings[0] and "three" in warnings[0]
    assert "attempt 2" in warnings[1] and second in warnings[1] and "Expecting value" in warnings[1]
    for w in warnings:
        assert "haiku" in w and MODEL in w and "stop_reason end_turn" in w
    msg = str(e.value)
    assert msg.startswith("invalid haiku output: Expecting value")
    assert "stop_reason end_turn" in msg and repr(second) in msg


async def test_error_message_keeps_only_the_start_of_the_raw_text() -> None:
    long = "x" * 5000
    with pytest.raises(DescribeError) as e:
        await haiku_from(
            "a kettle",
            settings=SETTINGS,
            get=get,
            client=FakeClient(reply(long), reply(long)),
            examples=[],
        )
    assert repr("x" * 200) in str(e.value) and "x" * 201 not in str(e.value)


async def test_max_tokens_says_so(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    cut = '{"drafts": ["moss on the'
    client = FakeClient(reply(cut, "max_tokens"), reply(cut, "max_tokens"))
    with pytest.raises(DescribeError) as e:
        await haiku_from("a kettle", settings=SETTINGS, get=get, client=client, examples=[])
    msg = str(e.value)
    assert "ran out of tokens at max_tokens 4096" in msg
    assert "Unterminated string" in msg and "stop_reason max_tokens" in msg
    assert all("stop_reason max_tokens" in w for w in texts(caplog, logging.WARNING))


async def test_fake_without_stop_reason_means_end_turn() -> None:
    out = await call_text(
        FakeClient(reply("hi", stop_reason=None)), model=MODEL, create_kwargs=kwargs()
    )
    assert out == "hi" and isinstance(out, Reply) and out.stop_reason == "end_turn"
    assert out.max_tokens == 4096


FACETS = {
    **{f: f"a plain {f}" for f in ("subject", "setting", "style", "mood")},
    "offers": ["heat"],
    "needs": ["water"],
}


async def _describe(client: Any) -> Any:
    return await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)


async def _classify(client: Any) -> Any:
    return await classify(b"x", "image/png", settings=SETTINGS, client=client)


async def _extract(client: Any) -> Any:
    return await extract(b"x", "image/png", settings=SETTINGS, client=client)


async def _name(client: Any) -> Any:
    inputs = Inputs(["a kettle"], ["kettle"], [])
    return await name_gallery(inputs, settings=SETTINGS, client=client, get=get, examples=[])


@pytest.mark.parametrize(
    ("job", "purpose", "prefix"),
    [
        (_describe, "describe", "invalid describer output"),
        (_classify, "categorize", "invalid categories"),
        (_extract, "extract", "invalid extract output"),
        (_name, "name", "invalid name output"),
    ],
    ids=["describe", "categorize", "extract", "name"],
)
async def test_every_caller_logs_each_rejected_reply(
    caplog: pytest.LogCaptureFixture, job: Any, purpose: str, prefix: str
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    client = FakeClient(reply("{nope", "end_turn"), reply('{"also": "wrong"}', "end_turn"))
    with pytest.raises(DescribeError) as e:
        await job(client)
    warnings = texts(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert all(f"invalid {purpose} output" in w for w in warnings)
    assert "{nope" in warnings[0] and '{"also": "wrong"}' in warnings[1]
    assert str(e.value).startswith(prefix + ": ")
    assert "stop_reason end_turn" in str(e.value) and "also" in str(e.value)


async def test_every_caller_stops_on_refusal() -> None:
    for job in (_describe, _classify, _extract, _name):
        client = FakeClient(reply("", "refusal"), reply("", "refusal"))
        with pytest.raises(DescribeError) as e:
            await job(client)
        assert not e.value.retryable and len(client.messages.calls) == 1


async def test_grow_logs_rejected_batches(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    fresh = ["salt in the sugar bowl", "nobody has said a thing", "the tea is still sweet"]
    good = json.dumps({"haikus": [["only", "two"], fresh]})
    client = FakeClient(reply("garbage"), reply(good))
    stats = await grow(
        pool=[(HAIKU[0], HAIKU[1], HAIKU[2])],
        n=1,
        batch=1,
        settings=SETTINGS,
        client=client,
        model=MODEL,
        get=get,
    )
    assert stats.invalid == 2 and stats.accepted == 1
    warnings = texts(caplog, logging.WARNING)
    assert any("invalid grow output" in w and "garbage" in w for w in warnings)
    assert any("invalid grow haiku" in w and '["only", "two"]' in w for w in warnings)


# -- debug records ----------------------------------------------------------


async def test_debug_logs_the_call_without_image_bytes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    image = bytes(range(256)) * 40
    b64 = base64.b64encode(image).decode()
    client = FakeClient(reply('{"caption": "a stove"}'))
    await call_text(client, model=MODEL, create_kwargs=kwargs(image), purpose="describe")
    debug = "\n".join(texts(caplog, logging.DEBUG))
    assert b64[:64] not in debug
    assert f"<image image/webp {len(image)} bytes>" in debug
    assert "be literal" in debug and "Return the JSON object." in debug
    assert "stop_reason end_turn" in debug and '{"caption": "a stove"}' in debug
    assert "'input_tokens': 10" in debug and "['text']" in debug


async def test_debug_off_logs_nothing_at_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    await call_text(FakeClient(reply("hi")), model=MODEL, create_kwargs=kwargs(b"img"))
    assert caplog.records == []


# -- providers --------------------------------------------------------------


class Wire:
    def __init__(self, body: Any) -> None:
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.body)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    def make(body: Any) -> None:
        monkeypatch.setattr(providers, "transport", httpx.MockTransport(Wire(body)))

    monkeypatch.setattr(providers, "get_settings", lambda: KEYS)
    return make


def responses(parts: list[dict[str, Any]], **top: Any) -> dict[str, Any]:
    return {
        "output": [{"type": "message", "role": "assistant", "content": parts}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
        **top,
    }


def chat(content: Any, finish: str | None, **msg: Any) -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content, **msg}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


@pytest.mark.parametrize(
    ("model", "body"),
    [
        ("openai:gpt-6-luna", responses([{"type": "refusal", "refusal": "I won't"}])),
        (
            "xai:grok-4.7",
            responses(
                [{"type": "output_text", "text": "{"}],
                status="incomplete",
                incomplete_details={"reason": "content_filter"},
            ),
        ),
        ("openrouter:a/b", chat(None, "stop", refusal="I won't")),
        ("openrouter:a/b", chat("{", "content_filter")),
    ],
    ids=["openai-refusal-part", "xai-content-filter", "openrouter-refusal", "openrouter-filter"],
)
async def test_provider_refusals(
    wire: Any, caplog: pytest.LogCaptureFixture, model: str, body: dict[str, Any]
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    wire(body)
    with pytest.raises(DescribeError) as e:
        await call_text(None, model=model, create_kwargs=kwargs(), purpose="haiku")
    assert str(e.value) == f"model refused ({model})" and not e.value.retryable
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in everything and "Bearer" not in everything


@pytest.mark.parametrize(
    ("model", "body", "stop"),
    [
        (
            "openai:gpt-6-luna",
            responses(
                [{"type": "output_text", "text": '{"a": "b'}],
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
            "max_tokens",
        ),
        ("openrouter:a/b", chat('{"a": "b', "length"), "max_tokens"),
        ("openrouter:a/b", chat('{"a": "b"}', "stop"), "end_turn"),
        ("openai:gpt-6-luna", responses([{"type": "output_text", "text": "{}"}]), "end_turn"),
    ],
    ids=["openai-max-output", "openrouter-length", "openrouter-stop", "openai-complete"],
)
async def test_provider_stop_reasons(
    wire: Any, model: str, body: dict[str, Any], stop: str
) -> None:
    wire(body)
    out = await call_text(None, model=model, create_kwargs=kwargs())
    assert isinstance(out, Reply) and out.stop_reason == stop


def test_parse_response_keeps_refusal_text() -> None:
    resp = providers.parse_response(
        providers.SPECS["openai"], responses([{"type": "refusal", "refusal": "no thanks"}])
    )
    assert resp.stop_reason == "refusal" and resp.refusal == "no thanks" and resp.content == []
