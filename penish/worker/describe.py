import base64
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
import jsonschema

from penish import logs, runtime
from penish.config import Settings
from penish.runtime import Getter, Pool, UsageRecorder
from penish.text import no_em_dash
from penish.worker import providers

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
# Raw model text kept in a log record, and in an error message (jobs.last_error, the system
# page), where a whole reply would bury the reason.
RAW_LOG = 8000
RAW_ERR = 200
# Stop reasons that mean the reply was cut off, so unparseable JSON is a budget problem.
OUT_OF_TOKENS = frozenset({"max_tokens", "model_context_window_exceeded"})


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


def build_schema(want: set[str], *, scratch: bool = True) -> dict[str, Any]:
    # Order is generation order. The faithful analysis comes first, then `angle` pins the
    # main subject and what only a local knows about it before the public text, which
    # otherwise drifts to a prop in the corner or back into an inventory of the scene.
    # `angle` is scratch and never stored.
    props: dict[str, Any] = {}
    if "analysis" in want:
        props["analysis"] = {"type": "string"}
    if scratch and want & {"caption", "description"}:
        props["angle"] = {"type": "string"}
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
    if isinstance(data, dict):
        data.pop("angle", None)
    jsonschema.validate(data, build_schema(want, scratch=False))
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


class LazyAnthropic:
    """The default client for every caller. With no explicit key the SDK resolves
    credentials from env and disk at construction and can raise there, so building it
    eagerly broke a worker keyed only for other providers before call_text could route a
    prefixed id away. It is built on the first bare-id call instead, and reused after."""

    def __init__(self, api_key: str | None) -> None:
        self._key = api_key
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def messages(self) -> Any:
        if self._client is None:
            # Same flat refusal a provider without a key gives; retrying cannot add a key.
            if not self._key:
                raise DescribeError("no key for anthropic", retryable=False)
            self._client = anthropic.AsyncAnthropic(api_key=self._key)
        return self._client.messages


class Reply(str):
    """call_text's text, carrying what a caller needs to say why it would not validate.

    A str so every caller and test that treats the reply as text keeps working."""

    stop_reason: str
    model: str
    purpose: str | None
    max_tokens: int | None

    def __new__(
        cls,
        text: str,
        *,
        stop_reason: str = "end_turn",
        model: str = "",
        purpose: str | None = None,
        max_tokens: int | None = None,
    ) -> "Reply":
        out = super().__new__(cls, text)
        out.stop_reason = stop_reason
        out.model = model
        out.purpose = purpose
        out.max_tokens = max_tokens
        return out


def _image_note(block: dict[str, Any]) -> str:
    source = block.get("source") or {}
    data = source.get("data")
    # Decoded size from the base64 length, so the bytes themselves never reach a log.
    size = len(data) * 3 // 4 - str(data).count("=", -2) if isinstance(data, str) else 0
    return f"<image {source.get('media_type', '?')} {size} bytes>"


def _loggable(content: Any) -> Any:
    if isinstance(content, str):
        return content
    out: list[Any] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "image":
            out.append(_image_note(block))
        elif isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
        else:
            out.append(
                f"<{block.get('type') if isinstance(block, dict) else type(block).__name__}>"
            )
    return out


def loggable_request(create_kwargs: dict[str, Any]) -> dict[str, Any]:
    """The request as it may be logged: images become a note, everything else is the
    owner's own prompts and options. There are no keys in create_kwargs; the clients hold
    those."""
    out: dict[str, Any] = {}
    for key, value in create_kwargs.items():
        if key == "messages":
            out[key] = [
                {"role": m.get("role"), "content": _loggable(m.get("content"))} for m in value
            ]
        elif key == "system" and not isinstance(value, str):
            out[key] = _loggable(value)
        else:
            out[key] = value
    return out


def _usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        k: int(getattr(usage, k, 0) or 0)
        for k in ("input_tokens", "output_tokens")
        if getattr(usage, k, None) is not None
    }


async def call_text(
    client: Any,
    *,
    model: str,
    create_kwargs: dict[str, Any],
    record: UsageRecorder | None = None,
    purpose: str | None = None,
) -> Reply:
    """One model call. `client` is the caller's Anthropic client or a test fake; a model id
    with a provider prefix swaps a real Anthropic client for that provider's adapter, so
    every caller can be pointed at another provider from admin without knowing it.

    A refusal raises a non-retryable DescribeError: the same prompt gets the same answer,
    so neither the caller's loop nor the job queue should pay for it again."""
    try:
        other = providers.prefixed(model)
    except ValueError as exc:
        raise DescribeError(str(exc), retryable=False) from exc
    if other and (client is None or isinstance(client, LazyAnthropic | anthropic.AsyncAnthropic)):
        client = providers.client_for(model)
    max_tokens = create_kwargs.get("max_tokens")
    with logs.model_context(purpose, model):
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "model request %s %s: %s",
                purpose,
                model,
                json.dumps(loggable_request(create_kwargs), default=str)[: RAW_LOG * 4],
            )
        started = time.monotonic()
        try:
            resp = await client.messages.create(model=model, **create_kwargs)
        except anthropic.APIError as exc:
            log.warning("model call %s %s failed: %s", purpose, model, exc)
            raise map_api_error(exc) from exc
        except providers.ProviderError as exc:
            log.warning("model call %s %s failed: %s", purpose, model, exc)
            raise DescribeError(str(exc), retryable=exc.retryable) from exc
        took = time.monotonic() - started
        if record is not None:
            try:
                await record(model, getattr(resp, "usage", None))
            except Exception:
                # Accounting must not throw away a response that was already paid for.
                log.exception("could not record api usage for %s", model)
        blocks = list(getattr(resp, "content", None) or [])
        text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
        # Test fakes and older adapters carry no stop reason; they mean a normal finish.
        stop = str(getattr(resp, "stop_reason", None) or "end_turn")
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "model reply %s %s: stop_reason %s, blocks %s, usage %s, %.2fs\n%s",
                purpose,
                model,
                stop,
                [getattr(b, "type", None) for b in blocks],
                _usage(getattr(resp, "usage", None)),
                took,
                text[:RAW_LOG],
                extra={"stop_reason": stop},
            )
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None)
            note = getattr(resp, "refusal", None)
            log.warning(
                "model refused %s %s (category %s)%s\npartial text: %s",
                purpose,
                model,
                category,
                f"\nrefusal: {note}" if note else "",
                text[:RAW_LOG],
                extra={"stop_reason": stop},
            )
            raise DescribeError(f"model refused ({model})", retryable=False)
    return Reply(text, stop_reason=stop, model=model, purpose=purpose, max_tokens=max_tokens)


def rejected(reply: str, exc: BaseException, *, attempt: int, purpose: str, model: str) -> None:
    """The WARNING for one reply that failed a caller's validation, with the raw text, so a
    vague parse error can be read against what the model actually sent."""
    stop = getattr(reply, "stop_reason", "end_turn")
    with logs.model_context(purpose, model):
        log.warning(
            "invalid %s output from %s (attempt %d, stop_reason %s): %s\nraw: %s",
            purpose,
            model,
            attempt,
            stop,
            exc,
            str(reply)[:RAW_LOG],
            extra={"stop_reason": stop},
        )


def invalid(prefix: str, reply: str | None, exc: BaseException | None) -> DescribeError:
    """The retryable error after the last rejected reply. It names the stop reason, the token
    budget when that ran out, and the start of the raw text, so jobs.last_error says what
    happened instead of only where the JSON parser gave up."""
    stop = getattr(reply, "stop_reason", "end_turn")
    head = f"{prefix}: "
    if stop in OUT_OF_TOKENS:
        head += f"ran out of tokens at max_tokens {getattr(reply, 'max_tokens', None)}: "
    raw = str(reply or "")[:RAW_ERR]
    return DescribeError(f"{head}{exc} (stop_reason {stop}; raw {raw!r})", retryable=True)


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
    nudity: Sequence[str] | None = None,
) -> Described:
    """`get` overrides the runtime lookup (tests); otherwise `pool` supplies it, and usage
    is recorded whenever a pool is given. `nudity` is the detector's kinds, which the
    prompt writes for differently."""
    want = expand(want)
    if not want or not want <= set(_FIELDS):
        raise ValueError(f"want must be a non-empty subset of {_FIELDS}")
    if client is None:
        client = LazyAnthropic(settings.anthropic_api_key)
    get = get or runtime.getter(pool, settings)
    # Read per call so an admin change applies to the next image without a restart.
    model = await get("describe.model")
    effort = await get("describe.effort")
    prompt = await get("describe.prompt")
    record = runtime.usage_recorder(pool, "describe")

    keys = [k for k in _FIELDS if k in want]
    ask = f"Return only these keys: {', '.join(keys)}."
    if nudity:
        ask = f"Nudity in this picture: {', '.join(nudity)}.\n\n{ask}"
    # temperature is not accepted for claude-sonnet-5-5 (the SDK no longer exposes
    # it), so determinism comes from the fixed analysis structure instead.
    kwargs: dict[str, Any] = {
        # Thinking shares this budget; a higher effort set in admin would otherwise starve
        # the JSON.
        "max_tokens": 8000,
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
                    {"type": "text", "text": ask},
                ],
            }
        ],
    }

    last: Exception | None = None
    text: str | None = None
    for attempt in (1, 2):
        text = await call_text(
            client, model=model, create_kwargs=kwargs, record=record, purpose="describe"
        )
        try:
            out = _validate(text, want)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            rejected(text, exc, attempt=attempt, purpose="describe", model=model)
            continue
        return Described(
            caption=out.get("caption"),
            description=out.get("description"),
            tags=out.get("tags"),
            model=model,
            analysis=out.get("analysis"),
        )
    raise invalid("invalid describer output", text, last)
