import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import anthropic
import jsonschema

from penish import runtime
from penish.config import Settings
from penish.runtime import Getter, Pool, UsageRecorder
from penish.text import no_em_dash

log = logging.getLogger(__name__)

_FIELDS = ("caption", "description", "analysis", "tags")
ANALYSIS_LABELS = ("Subject", "Setting", "Style", "Mood")
_LABEL = re.compile(r"^\s*(Subject|Setting|Style|Mood)\s*:", re.M)
# The description is meant to read as plain and sincere; these words are how a model
# signals that it knows it is looking at the wrong thing.
_TELLS = re.compile(
    r"\b(oddly|curiously|notably|interestingly|strangely|surprisingly|ironically|meanwhile)\b",
    re.I,
)
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
    analysis: str | None = None


def build_schema(want: set[str]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if "caption" in want:
        props["caption"] = {"type": "string"}
    if "description" in want:
        props["description"] = {"type": "string"}
    if "analysis" in want:
        props["analysis"] = {"type": "string"}
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


def expand(want: set[str]) -> set[str]:
    """The description looks away from the subject, so it never travels without the analysis
    that the embedding is made from."""
    want = set(want)
    if "description" in want:
        want.add("analysis")
    return want


def check_analysis(text: str) -> None:
    labels = tuple(m.group(1) for m in _LABEL.finditer(text))
    if labels != ANALYSIS_LABELS:
        raise ValueError(f"analysis labels {labels}, want {ANALYSIS_LABELS}")


def check_public(field: str, text: str) -> None:
    if _LABEL.search(text):
        raise ValueError(f"{field} has analysis labels")
    if m := _TELLS.search(text):
        raise ValueError(f"{field} uses {m.group(0)!r}")


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
            if key == "analysis":
                check_analysis(value)
            else:
                check_public(key, value)
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


async def call_text(
    client: Any,
    *,
    model: str,
    create_kwargs: dict[str, Any],
    record: UsageRecorder | None = None,
) -> str:
    try:
        resp = await client.messages.create(model=model, **create_kwargs)
    except anthropic.APIError as exc:
        raise map_api_error(exc) from exc
    if record is not None:
        try:
            await record(model, getattr(resp, "usage", None))
        except Exception:
            # Accounting must not throw away a response that was already paid for.
            log.exception("could not record api usage for %s", model)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def output_config(schema: dict[str, Any], effort: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    # Only when set: models without effort support reject the key outright.
    if effort:
        cfg["effort"] = effort
    return cfg


async def describe(
    image_bytes: bytes,
    mime: str,
    *,
    want: set[str],
    settings: Settings,
    pool: Pool | None = None,
    get: Getter | None = None,
    client: Any = None,
) -> Described:
    """`get` overrides the runtime lookup (tests); otherwise `pool` supplies it, and usage
    is recorded whenever a pool is given."""
    want = expand(want)
    if not want or not want <= set(_FIELDS):
        raise ValueError(f"want must be a non-empty subset of {_FIELDS}")
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    get = get or runtime.getter(pool, settings)
    # Read per call so an admin change applies to the next image without a restart.
    model = await get("describe.model")
    effort = await get("describe.effort")
    prompt = await get("describe.prompt")
    record = runtime.usage_recorder(pool, "describe")

    keys = [k for k in _FIELDS if k in want]
    # temperature is not accepted for claude-sonnet-5-5 (the SDK no longer exposes
    # it), so determinism comes from the fixed analysis structure instead.
    kwargs: dict[str, Any] = {
        "max_tokens": 2500,
        "system": prompt,
        "output_config": output_config(build_schema(want), effort),
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
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            out = _validate(text, want)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            continue
        return Described(
            caption=out.get("caption"),
            description=out.get("description"),
            tags=out.get("tags"),
            model=model,
            analysis=out.get("analysis"),
        )
    raise DescribeError(f"invalid describer output: {last}", retryable=True)
