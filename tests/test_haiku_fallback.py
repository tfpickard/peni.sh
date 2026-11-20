"""haiku.fallback: another provider writes the haiku when haiku.model refuses, the model
that wrote each haiku is stored, and the worker-start sweep that queues missing haikus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from penish import runtime
from penish.config import Settings
from penish.worker import providers
from penish.worker.admin_jobs import handle_regen
from penish.worker.describe import DescribeError
from penish.worker.haiku import Written, haiku_from
from penish.worker.ingest import enqueue_unwritten, handle_haiku
from penish.worker.jobs import HardError, Job, SoftError
from tests.test_admin import XHR
from tests.test_admin_jobs import Redescriber, live
from tests.test_admin_ops import client  # noqa: F401  (client is a fixture)
from tests.test_ingest import DIM, FakeEmbedder, q
from tests.test_model_diagnostics import HAIKU, FakeClient, reply
from tests.test_refusals import refused

PRIMARY = "claude-opus-5-5"
KEYED = Settings(anthropic_api_key="ak", openai_api_key="ok", openrouter_api_key="rk")
LINES = json.dumps({"drafts": ["x / y / z"], "lines": HAIKU})


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


def getter(fallback: list[str], model: str = PRIMARY) -> Any:
    async def get(key: str) -> Any:
        if key == "haiku.model":
            return model
        if key == runtime.FALLBACK_KEY:
            return fallback
        return runtime.default(key)

    return get


def clients(**by_model: FakeClient) -> Any:
    def go(model: str) -> FakeClient:
        return by_model[model]

    return go


async def write(fallback: list[str], primary: FakeClient, **by_model: FakeClient) -> Written:
    return await haiku_from(
        "A cup.",
        settings=KEYED,
        get=getter(fallback),
        client=primary,
        client_for=clients(**by_model),
        examples=[],
    )


# -- the fallback ------------------------------------------------------------


async def test_refusal_goes_to_the_first_fallback() -> None:
    primary = FakeClient(refused("general_harms"))
    other = FakeClient(reply(LINES))
    got = await write(["openai:a", "openrouter:b"], primary, **{"openai:a": other})
    assert list(got) == HAIKU and got.model == "openai:a"
    [call] = other.messages.calls
    # Same request as the configured model got, under the fallback's own id.
    assert call["model"] == "openai:a"
    assert {k: v for k, v in call.items() if k != "model"} == {
        k: v for k, v in primary.messages.calls[0].items() if k != "model"
    }


async def test_unkeyed_provider_and_the_configured_model_are_skipped() -> None:
    primary = FakeClient(refused(None))
    second = FakeClient(reply(LINES))
    # xai has no key in KEYED; the configured model named again is not asked twice.
    got = await haiku_from(
        "A cup.",
        settings=KEYED,
        get=getter(["xai:a", "openai:p", "openrouter:b"], model="openai:p"),
        client=primary,
        client_for=clients(**{"openrouter:b": second}),
        examples=[],
    )
    assert got.model == "openrouter:b" and len(primary.messages.calls) == 1


async def test_every_model_refusing_names_them_all() -> None:
    primary = FakeClient(refused("general_harms"))
    a = FakeClient(refused(None))
    b = FakeClient(refused(None))
    with pytest.raises(DescribeError) as e:
        await write(["openai:a", "openrouter:b"], primary, **{"openai:a": a, "openrouter:b": b})
    assert not e.value.retryable and e.value.refused
    assert str(e.value) == (
        f"model refused ({PRIMARY}, general_harms); model refused (openai:a);"
        " model refused (openrouter:b)"
    )


async def test_a_fallback_that_cannot_serve_the_request_hands_on() -> None:
    primary = FakeClient(refused(None))

    class Gone:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                raise providers.ProviderError("openai 404: no such model", retryable=False)

    b = FakeClient(reply(LINES))
    got = await haiku_from(
        "A cup.",
        settings=KEYED,
        get=getter(["openai:a", "openrouter:b"]),
        client=primary,
        client_for=lambda m: Gone() if m == "openai:a" else b,
        examples=[],
    )
    assert got.model == "openrouter:b"


async def test_transient_fallback_error_stays_retryable() -> None:
    primary = FakeClient(refused(None))

    class Busy:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                raise providers.ProviderError("openai 429: slow down", retryable=True)

    later = FakeClient(reply(LINES))
    with pytest.raises(DescribeError) as e:
        await haiku_from(
            "A cup.",
            settings=KEYED,
            get=getter(["openai:a", "openrouter:b"]),
            client=primary,
            client_for=lambda m: Busy() if m == "openai:a" else later,
            examples=[],
        )
    assert e.value.retryable
    # Not moved on: the queue backs off and the next attempt starts over.
    assert later.messages.calls == []


async def test_empty_fallback_is_todays_refusal() -> None:
    primary = FakeClient(refused("general_harms"))

    def never(model: str) -> Any:
        raise AssertionError(model)

    with pytest.raises(DescribeError) as e:
        await haiku_from(
            "A cup.",
            settings=KEYED,
            get=getter([]),
            client=primary,
            client_for=never,
            examples=[],
        )
    assert str(e.value) == f"model refused ({PRIMARY}, general_harms)"
    assert not e.value.retryable and len(primary.messages.calls) == 1


async def test_reasoning_extraction_retry_still_comes_first() -> None:
    primary = FakeClient(refused("reasoning_extraction"), reply(json.dumps({"lines": HAIKU})))
    got = await write(["openai:a"], primary)
    assert got.model == PRIMARY and len(primary.messages.calls) == 2


async def test_non_refusal_hard_error_does_not_fall_back() -> None:
    class Bad:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                raise providers.ProviderError("openai 400: bad request", retryable=False)

    with pytest.raises(DescribeError, match="400"):
        await haiku_from(
            "A cup.",
            settings=KEYED,
            get=getter(["openrouter:b"], model="openai:p"),
            client=Bad(),
            client_for=lambda m: pytest.fail(m),
            examples=[],
        )


# -- over the wire -----------------------------------------------------------


class Wire:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append({"url": str(request.url), **json.loads(request.content)})
        return httpx.Response(200, json=self.answers.pop(0))


def chat(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


def responses(text: str) -> dict[str, Any]:
    msg = {"type": "output_text", "text": text}
    return {
        "output": [{"type": "message", "role": "assistant", "content": [msg]}],
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }


OPENAI_REFUSES = {
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "no"}],
        }
    ],
    "usage": {"input_tokens": 5, "output_tokens": 1},
}


async def test_fallback_through_provider_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(OPENAI_REFUSES, chat(LINES))
    monkeypatch.setattr(providers, "transport", httpx.MockTransport(wire))
    got = await haiku_from(
        "A cup.",
        settings=KEYED,
        get=getter(["openai:a", "openrouter:v/b"]),
        client=FakeClient(refused(None)),
        examples=[],
    )
    assert got.model == "openrouter:v/b" and list(got) == HAIKU
    openai, openrouter = wire.sent
    # The drafts-then-lines schema reaches both wire formats as a strict json schema.
    assert openai["text"]["format"]["type"] == "json_schema"
    assert list(openai["text"]["format"]["schema"]["properties"]) == ["drafts", "lines"]
    assert openrouter["response_format"]["json_schema"]["strict"] is True
    assert list(openrouter["response_format"]["json_schema"]["schema"]["properties"]) == [
        "drafts",
        "lines",
    ]


async def test_malformed_fallback_answer_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    # An upstream that ignores the schema answers in prose; validation turns it away.
    wire = Wire(chat("a cup, a cup, a cup"), chat(json.dumps({"lines": ["only", "two"]})))
    monkeypatch.setattr(providers, "transport", httpx.MockTransport(wire))
    with pytest.raises(DescribeError) as e:
        await haiku_from(
            "A cup.",
            settings=KEYED,
            get=getter(["openrouter:b"]),
            client=FakeClient(refused(None)),
            examples=[],
        )
    assert e.value.retryable and str(e.value).startswith("invalid haiku output")


async def test_fallback_usage_is_recorded(p: Any) -> None:
    await runtime.put(p, "haiku.model", PRIMARY)
    await runtime.put(p, runtime.FALLBACK_KEY, ["openai:a"])
    got = await haiku_from(
        "A cup.",
        settings=KEYED,
        pool=p,
        client=FakeClient(refused(None)),
        client_for=clients(**{"openai:a": FakeClient(reply(LINES))}),
        examples=[],
    )
    assert got.model == "openai:a"
    rows = await q(p, "select purpose, model from api_usage order by id")
    assert rows == [
        {"purpose": "haiku", "model": PRIMARY},
        {"purpose": "haiku", "model": "openai:a"},
    ]


# -- the setting -------------------------------------------------------------


def test_fallback_validation() -> None:
    assert runtime.default(runtime.FALLBACK_KEY) == []
    assert runtime.validate(
        runtime.FALLBACK_KEY, [" openai: a ", "", "openrouter:v/b:x", "openai:a"]
    ) == ["openai:a", "openrouter:v/b:x"]
    with pytest.raises(ValueError, match="claude-x is not another provider"):
        runtime.validate(runtime.FALLBACK_KEY, ["openai:a", "claude-x"])
    with pytest.raises(ValueError, match="unknown provider"):
        runtime.validate(runtime.FALLBACK_KEY, ["acme:a"])
    for bad in ("openai:a", None, [1]):
        with pytest.raises(ValueError):
            runtime.validate(runtime.FALLBACK_KEY, bad)


async def test_fallback_setting_over_the_api(client: httpx.AsyncClient) -> None:  # noqa: F811
    body = (await client.get("/api/admin/models")).json()
    assert body["current"][runtime.FALLBACK_KEY] == [] == body["default"][runtime.FALLBACK_KEY]
    r = await client.put(
        "/api/admin/models",
        json={"key": runtime.FALLBACK_KEY, "value": ["claude-x"]},
        headers=XHR,
    )
    assert r.status_code == 422 and r.json()["detail"] == "claude-x is not another provider"
    r = await client.put(
        "/api/admin/models",
        json={"key": runtime.FALLBACK_KEY, "value": ["openrouter:v/b", "openai:a"]},
        headers=XHR,
    )
    assert r.status_code == 200 and r.json()["value"] == ["openrouter:v/b", "openai:a"]
    r = await client.delete(f"/api/admin/models/{runtime.FALLBACK_KEY}")
    assert r.json()["value"] == []


# -- who wrote it -------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


async def by(description: str, *, settings: Settings, pool: Any, nudity: Any = ()) -> Written:
    return Written(("a cup", "it was always full", "nobody poured"), "openrouter:v/b")


def job(type: str, payload: dict[str, Any]) -> Job:
    return Job(id=0, type=type, payload=payload, attempts=1, last_error=None)


async def test_model_is_stored_for_model_haikus_only(
    p: Any,
    settings: Settings,
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    first = await live(p, settings, seed=1)
    second = await live(p, settings, seed=2)
    async with p.connection() as conn:
        await conn.execute("delete from haikus where image_id is not null")
    await handle_haiku(job("haiku", {"image_id": first}), pool=p, settings=settings, writer=by)

    async def again(description: str, **kw: Any) -> Written:
        return Written(("a lid", "it remembers the cup", "the kettle disagrees"), "openai:a")

    await handle_regen(
        job("regen", {"image_id": second, "fields": ["haiku"], "force_owner": False}),
        pool=p,
        settings=settings,
        embedder=FakeEmbedder(),
        describer=Redescriber(),
        writer=again,
    )
    r = await client.post("/api/admin/haikus", json={"lines": ["o", "w", "n"]}, headers=XHR)
    assert r.status_code == 200 and r.json()["model"] is None
    rows = await q(
        p, "select image_id, source, model from haikus where source <> 'seed' order by id"
    )
    assert rows == [
        {"image_id": first, "source": "model", "model": "openrouter:v/b"},
        {"image_id": second, "source": "model", "model": "openai:a"},
        {"image_id": None, "source": "owner", "model": None},
    ]
    listed = (await client.get("/api/admin/haikus?source=model")).json()["items"]
    assert {h["model"] for h in listed} == {"openrouter:v/b", "openai:a"}


async def test_failed_writer_is_hard_naming_every_model(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)

    async def all_refuse(description: str, **kw: Any) -> Written:
        raise DescribeError("model refused (m); model refused (openai:a)", retryable=False)

    async def busy(description: str, **kw: Any) -> Written:
        raise DescribeError("openai 429", retryable=True)

    with pytest.raises(HardError, match=r"haiku: model refused \(m\); model refused \(openai:a\)"):
        await handle_haiku(
            job("haiku", {"image_id": image_id}), pool=p, settings=settings, writer=all_refuse
        )
    with pytest.raises(SoftError):
        await handle_haiku(
            job("haiku", {"image_id": image_id}), pool=p, settings=settings, writer=busy
        )


# -- the sweep ----------------------------------------------------------------


async def test_sweep_queues_live_pictures_with_no_haiku_and_no_job(
    p: Any, settings: Settings
) -> None:
    ids = [await live(p, settings, seed=s) for s in range(1, 6)]
    plain, other, failed, written, queued = ids
    async with p.connection() as conn:
        # Going live queued a haiku job for each; the fake job loop never ran them.
        await conn.execute("delete from jobs where type = 'haiku'")
        await conn.execute("delete from haikus where image_id is not null")
        await conn.execute(
            "insert into jobs (type, payload, dedupe_key, status) values"
            " ('haiku', jsonb_build_object('image_id', %s::bigint), %s, 'failed'),"
            " ('haiku', jsonb_build_object('image_id', %s::bigint), %s, 'queued'),"
            " ('haiku', jsonb_build_object('image_id', %s::bigint), %s, 'done')",
            (failed, f"haiku:{failed}", queued, f"haiku:{queued}", other, f"haiku:{other}"),
        )
        await conn.execute(
            "insert into haikus (l1, l2, l3, source, image_id) values ('x', 'y', 'z', 'model', %s)",
            (written,),
        )
    assert await enqueue_unwritten(p) == 2
    rows = await q(
        p,
        "select payload, dedupe_key from jobs where type = 'haiku' and status = 'queued'"
        " order by id",
    )
    assert rows[-2:] == [
        {"payload": {"image_id": plain}, "dedupe_key": f"haiku:{plain}"},
        {"payload": {"image_id": other}, "dedupe_key": f"haiku:{other}"},
    ]
    # Twice is once.
    assert await enqueue_unwritten(p) == 0
