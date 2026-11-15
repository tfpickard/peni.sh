import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from penish.config import Settings
from penish.runtime import default
from penish.text import no_em_dash, slugify
from penish.worker.describe import DescribeError, describe
from penish.worker.haiku import build_prompt, haiku_from

SETTINGS = Settings(anthropic_api_key="test")
TAGS = ["cat", "kitchen", "photo", "warm-light", "tiled-floor", "orange", "indoor"]
ANALYSIS = (
    "Subject: a cat sits on a stove.\n\nSetting: a kitchen.\n\n"
    "Style: photograph, warm light.\n\nMood: The cat is on the stove."
)
DESCRIPTION = "The stove has four steel knobs, the second one turned a quarter. A cat is on it."


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
    assert list(schema["properties"]) == ["concern", "caption"]
    assert call["messages"][0]["content"][1]["text"].endswith("caption.")
    assert call["model"] == SETTINGS.describe_model
    assert out.caption == "cat on a stove"
    assert out.description is None and out.tags is None


async def test_em_dash_converted() -> None:
    reply = {"description": "A knob \u2014 steel.", "analysis": ANALYSIS.replace(".", " \u2014 x.")}
    client = FakeClient(json.dumps(reply))
    out = await describe(b"x", "image/png", want={"description"}, settings=SETTINGS, client=client)
    assert out.description == "A knob -- steel."
    assert out.analysis is not None and "\u2014" not in out.analysis


async def test_description_brings_analysis() -> None:
    client = FakeClient(json.dumps({"description": DESCRIPTION, "analysis": ANALYSIS}))
    out = await describe(b"x", "image/png", want={"description"}, settings=SETTINGS, client=client)
    call = client.messages.calls[0]
    schema = call["output_config"]["format"]["schema"]
    assert (
        set(schema["properties"])
        == set(schema["required"])
        == {"description", "analysis", "concern"}
    )
    # The faithful analysis is written before the misreading it informs.
    assert list(schema["properties"]) == ["analysis", "concern", "description"]
    assert call["messages"][0]["content"][1]["text"].endswith("description, analysis.")
    assert (out.description, out.analysis) == (DESCRIPTION, ANALYSIS)


async def test_analysis_alone() -> None:
    client = FakeClient(json.dumps({"analysis": ANALYSIS}))
    out = await describe(b"x", "image/png", want={"analysis"}, settings=SETTINGS, client=client)
    schema = client.messages.calls[0]["output_config"]["format"]["schema"]
    assert list(schema["properties"]) == ["analysis"]
    assert out.analysis == ANALYSIS and out.description is None


@pytest.mark.parametrize(
    "reply",
    [
        # The analysis lost a label, or has them out of order.
        {"description": DESCRIPTION, "analysis": "Subject: a cat.\n\nMood: flat."},
        {"description": DESCRIPTION, "analysis": ANALYSIS.replace("Setting:", "Place:")},
        {
            "description": DESCRIPTION,
            "analysis": "Setting: a.\n\nSubject: b.\n\nStyle: c.\n\nMood: d.",
        },
        # The description copied the analysis structure, or gave itself away.
        {"description": ANALYSIS, "analysis": ANALYSIS},
        {"description": "The knob is, curiously, steel.", "analysis": ANALYSIS},
    ],
)
async def test_bad_description_or_analysis_retried(reply: dict[str, str]) -> None:
    client = FakeClient(*[json.dumps(reply)] * 2)
    with pytest.raises(DescribeError) as ei:
        await describe(b"x", "image/png", want={"description"}, settings=SETTINGS, client=client)
    assert ei.value.retryable and len(client.messages.calls) == 2


def test_prompt_rules() -> None:
    prompt = default("describe.prompt")
    for key in ("caption:", "description:", "analysis:", "tags:"):
        assert key in prompt
    for label in ("Subject:", "Setting:", "Style:", "Mood:"):
        assert label in prompt
    assert "\u2014" not in prompt


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


async def test_haiku_drafts_are_asked_for_first_and_dropped() -> None:
    reply = {"drafts": ["a / b / c", "d / e / f"], "lines": ["d", "e", "f"]}
    client = FakeClient(json.dumps(reply))
    assert await haiku_from("a cat", settings=SETTINGS, client=client) == ("d", "e", "f")
    schema = client.messages.calls[0]["output_config"]["format"]["schema"]
    # Order matters: the scratch has to be written before the pick it informs.
    assert list(schema["properties"]) == ["drafts", "lines"]
    assert schema["required"] == ["drafts", "lines"]


def test_haiku_prompt_fills_examples() -> None:
    prompt = build_prompt("before\n{examples}\nafter", [("a", "b", "c"), ("d", "e", "f")])
    assert prompt == "before\na\nb\nc\n\nd\ne\nf\nafter"


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
    assert out.caption and out.description and out.analysis and out.tags
    assert 6 <= len(out.tags) <= 14
    assert "\u2014" not in out.description and "\u2014" not in out.analysis
    for label in ("Subject:", "Setting:", "Style:", "Mood:"):
        assert label in out.analysis and label not in out.description
    print(f"\ncaption: {out.caption}\ndescription: {out.description}\nanalysis: {out.analysis}")
