"""Refusals: the server-side fallback every Anthropic call asks for, and the haiku writer's
retry without drafts after a reasoning_extraction refusal."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from penish.worker import haiku as haiku_mod
from penish.worker.describe import FALLBACK_BETA, DescribeError, LazyAnthropic, call_text
from penish.worker.haiku import haiku_from
from tests.test_model_diagnostics import (
    HAIKU,
    SETTINGS,
    FakeClient,
    FakeMessages,
    get,
    kwargs,
    reply,
)

LINES = json.dumps({"lines": HAIKU})
DRAFTED = json.dumps({"drafts": ["a / b / c"], "lines": HAIKU})


def refused(category: str | None) -> Any:
    return reply("", "refusal", stop_details=SimpleNamespace(category=category, explanation=None))


def anthropic_client(*replies: Any) -> tuple[LazyAnthropic, FakeMessages]:
    """A real client type, so call_text takes its Anthropic path, over a fake transport."""
    fake = FakeMessages(list(replies))
    client = LazyAnthropic("test")
    client._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))  # type: ignore[assignment]
    return client, fake


async def test_anthropic_calls_ask_for_the_default_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="penish.worker")
    client, fake = anthropic_client(reply(LINES, model="claude-opus-4-8"))
    recorded: list[str] = []

    async def record(model: str, usage: Any) -> None:
        recorded.append(model)

    out = await call_text(
        client, model="claude-opus-5-5", create_kwargs=kwargs(), record=record, purpose="haiku"
    )
    assert out == LINES
    [call] = fake.calls
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["model"] == "claude-opus-5-5"
    # Usage is booked to the model that answered, and the switch is logged.
    assert recorded == ["claude-opus-4-8"]
    assert any("claude-opus-4-8 answered" in r.getMessage() for r in caplog.records)


async def test_other_clients_take_the_plain_call() -> None:
    client = FakeClient(reply(LINES))
    await call_text(client, model="claude-opus-5-5", create_kwargs=kwargs(), purpose="haiku")
    [call] = client.messages.calls
    assert "fallbacks" not in call and "betas" not in call


async def test_refusal_carries_its_category() -> None:
    client = FakeClient(refused("reasoning_extraction"))
    with pytest.raises(DescribeError) as e:
        await call_text(client, model="m", create_kwargs=kwargs(), purpose="haiku")
    assert e.value.category == "reasoning_extraction" and not e.value.retryable
    assert str(e.value) == "model refused (m, reasoning_extraction)"


async def test_haiku_retries_without_drafts_after_reasoning_extraction() -> None:
    client = FakeClient(refused("reasoning_extraction"), reply(LINES))
    got = await haiku_from("A cup.", settings=SETTINGS, get=get, client=client, examples=[])
    assert list(got) == HAIKU
    first, second = client.messages.calls
    assert "drafts" in first["output_config"]["format"]["schema"]["properties"]
    assert list(second["output_config"]["format"]["schema"]["properties"]) == ["lines"]
    assert second["system"] == first["system"] + haiku_mod._NO_DRAFTS
    assert second["messages"] == first["messages"]


async def test_haiku_keeps_drafts_when_they_are_not_the_problem() -> None:
    client = FakeClient(reply(DRAFTED))
    assert list(await haiku_from("A cup.", settings=SETTINGS, get=get, client=client)) == HAIKU
    assert len(client.messages.calls) == 1

    # Another category is not about the drafts: nothing to rephrase, so it fails at once.
    client = FakeClient(refused("general_harms"), reply(LINES))
    with pytest.raises(DescribeError) as e:
        await haiku_from("A cup.", settings=SETTINGS, get=get, client=client, examples=[])
    assert e.value.category == "general_harms" and len(client.messages.calls) == 1

    # Refused again without drafts: that is the answer.
    client = FakeClient(refused("reasoning_extraction"), refused("reasoning_extraction"))
    with pytest.raises(DescribeError):
        await haiku_from("A cup.", settings=SETTINGS, get=get, client=client, examples=[])
    assert len(client.messages.calls) == 2
