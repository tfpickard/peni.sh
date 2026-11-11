import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

from penish.config import Settings
from penish.text import no_em_dash

PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "describe.md").read_text()

_FIELDS = ("caption", "description", "tags")
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?```\s*$", re.S)
_TAG_BAD = re.compile(r"[^a-z0-9]+")
MIN_TAGS, MAX_TAGS = 6, 14


class DescribeError(Exception):
    """`retryable` tells the job loop whether to back off (soft) or fail the job (hard)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Described:
    caption: str | None
    description: str | None
    tags: list[str] | None
    model: str


def build_schema(want: set[str]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if "caption" in want:
        props["caption"] = {"type": "string"}
    if "description" in want:
        props["description"] = {"type": "string"}
    if "tags" in want:
        props["tags"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def parse_json(text: str) -> Any:
    # Structured outputs should return bare JSON, but a fenced block is cheap to tolerate.
    m = _FENCE.match(text)
    return json.loads(m.group(1) if m else text)


def clean_tags(raw: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for t in raw:
        tag = _TAG_BAD.sub("-", no_em_dash(t).lower()).strip("-")
        if tag:
            seen.setdefault(tag)
    tags = list(seen)
    if not MIN_TAGS <= len(tags) <= MAX_TAGS:
        # Over-long lists are trimmed; short ones mean the model under-delivered.
        if len(tags) > MAX_TAGS:
            return tags[:MAX_TAGS]
        raise ValueError(f"expected {MIN_TAGS} to {MAX_TAGS} tags, got {len(tags)}")
    return tags


def _validate(text: str, want: set[str]) -> dict[str, Any]:
    data = parse_json(text)
    jsonschema.validate(data, build_schema(want))
    out: dict[str, Any] = {}
    for key in want:
        if key == "tags":
            out[key] = clean_tags(data[key])
        else:
            value = no_em_dash(data[key]).strip()
            if not value:
                raise ValueError(f"empty {key}")
            out[key] = value
    return out


def map_api_error(exc: anthropic.APIError) -> DescribeError:
    # Rate limits, overload, 5xx, connection drops and timeouts clear up on their own.
    if isinstance(
        exc,
        anthropic.RateLimitError
        | anthropic.OverloadedError
        | anthropic.APITimeoutError
        | anthropic.APIConnectionError,
    ):
        return DescribeError(str(exc), retryable=True)
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return DescribeError(str(exc), retryable=True)
    return DescribeError(str(exc), retryable=False)


async def call_text(client: Any, *, model: str, create_kwargs: dict[str, Any]) -> str:
    try:
        resp = await client.messages.create(model=model, **create_kwargs)
    except anthropic.APIError as exc:
        raise map_api_error(exc) from exc
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


async def describe(
    image_bytes: bytes,
    mime: str,
    *,
    want: set[str],
    settings: Settings,
    client: Any = None,
) -> Described:
    want = set(want)
    if not want or not want <= set(_FIELDS):
        raise ValueError(f"want must be a non-empty subset of {_FIELDS}")
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    keys = [k for k in _FIELDS if k in want]
    # temperature is not accepted for claude-sonnet-5-5 (the SDK no longer exposes
    # it), so determinism comes from the fixed description structure instead.
    kwargs: dict[str, Any] = {
        "max_tokens": 1500,
        "system": PROMPT,
        "output_config": {"format": {"type": "json_schema", "schema": build_schema(want)}},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": f"Return only these keys: {', '.join(keys)}."},
                ],
            }
        ],
    }

    last: Exception | None = None
    for _ in range(2):
        text = await call_text(client, model=settings.describe_model, create_kwargs=kwargs)
        try:
            out = _validate(text, want)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            continue
        return Described(
            caption=out.get("caption"),
            description=out.get("description"),
            tags=out.get("tags"),
            model=settings.describe_model,
        )
    raise DescribeError(f"invalid describer output: {last}", retryable=True)
