import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from penish.config import Settings
from penish.text import no_em_dash, slugify
from penish.worker.describe import DescribeError, describe
from penish.worker.haiku import build_prompt, haiku_from, seed_examples

SETTINGS = Settings(anthropic_api_key="test")
TAGS = ["cat", "kitchen", "photo", "warm-light", "tiled-floor", "orange", "indoor"]


class FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self.messages = FakeMessages(list(replies))


async def test_only_wanted_keys_requested() -> None:
    client = FakeClient(json.dumps({"caption": "cat on a stove"}))
    out = await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)
    call = client.messages.calls[0]
    schema = call["output_config"]["format"]["schema"]
    assert list(schema["properties"]) == ["caption"]
    assert call["messages"][0]["content"][1]["text"].endswith("caption.")
    assert call["model"] == SETTINGS.describe_model
    assert out.caption == "cat on a stove"
    assert out.description is None and out.tags is None


async def test_em_dash_converted() -> None:
    client = FakeClient(json.dumps({"description": "Subject: a cat \u2014 sitting."}))
    out = await describe(b"x", "image/png", want={"description"}, settings=SETTINGS, client=client)
    assert out.description == "Subject: a cat -- sitting."


async def test_code_fenced_json() -> None:
    body = json.dumps({"caption": "a cat"})
    client = FakeClient(f"```json\n{body}\n```")
    out = await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)
    assert out.caption == "a cat"


async def test_tags_normalised() -> None:
    raw = ["Cat", "cat", "Warm Light", "kitchen", "photo", "tiled floor", "orange", "indoor"]
    client = FakeClient(json.dumps({"tags": raw}))
    out = await describe(b"x", "image/png", want={"tags"}, settings=SETTINGS, client=client)
    assert out.tags == ["cat", "warm-light", "kitchen", "photo", "tiled-floor", "orange", "indoor"]


async def test_bad_json_twice_is_retryable() -> None:
    client = FakeClient("nope", "{still not")
    with pytest.raises(DescribeError) as ei:
        await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)
    assert ei.value.retryable
    assert len(client.messages.calls) == 2


async def test_bad_json_then_good_recovers() -> None:
    client = FakeClient("nope", json.dumps({"caption": "a cat"}))
    out = await describe(b"x", "image/png", want={"caption"}, settings=SETTINGS, client=client)
    assert out.caption == "a cat"


async def test_too_few_tags_rejected() -> None:
    client = FakeClient(json.dumps({"tags": ["a", "b"]}), json.dumps({"tags": ["a"]}))
    with pytest.raises(DescribeError):
        await describe(b"x", "image/png", want={"tags"}, settings=SETTINGS, client=client)


async def test_haiku_ok() -> None:
    client = FakeClient(json.dumps({"lines": ["one \u2014 two", "three", "four"]}))
    assert await haiku_from("a cat", settings=SETTINGS, client=client) == (
        "one -- two",
        "three",
        "four",
    )
    assert client.messages.calls[0]["model"] == SETTINGS.haiku_model


@pytest.mark.parametrize(
    "lines", [["a", "b"], ["a", "b", "c", "d"], ["a", "", "c"], ["a", "b", "x" * 61]]
)
async def test_haiku_rejects_bad_shape(lines: list[str]) -> None:
    client = FakeClient(*[json.dumps({"lines": lines})] * 2)
    with pytest.raises(DescribeError) as ei:
        await haiku_from("a cat", settings=SETTINGS, client=client)
    assert ei.value.retryable
    assert len(client.messages.calls) == 2


def test_haiku_prompt_has_six_seed_examples() -> None:
    assert len(seed_examples()) == 6
    prompt = build_prompt()
    assert "{examples}" not in prompt
    assert seed_examples()[0][0] in prompt


def test_no_em_dash() -> None:
    assert no_em_dash("a\u2014b") == "a--b"
    assert no_em_dash("a \u2013 b") == "a -- b"
    assert no_em_dash("a\u2015b") == "a--b"
    assert no_em_dash("5\u20137") == "5\u20137"


def test_slugify() -> None:
    assert slugify("Café au Lait!") == "cafe-au-lait"
    assert slugify("  --a  b--  ") == "a-b"
    assert slugify("") == ""
    assert slugify("word " * 30, max_len=12) == "word-word-wo"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY")
async def test_live_describe() -> None:
    import pyvips

    png = pyvips.Image.black(64, 64).new_from_image([255, 77, 157]).write_to_buffer(".png")
    out = await describe(
        png,
        "image/png",
        want={"caption", "description", "tags"},
        settings=Settings(anthropic_api_key=os.environ["ANTHROPIC_API_KEY"]),
    )
    assert out.caption and out.description and out.tags
    assert 6 <= len(out.tags) <= 14
    assert "\u2014" not in out.description
    for label in ("Subject:", "Setting:", "Style:", "Mood:"):
        assert label in out.description
