"""Model providers beside Anthropic, behind the Anthropic messages shape.

Every caller builds Anthropic `messages.create` kwargs and reads `.content` text blocks and
`.usage`. A model id with a `provider:` prefix (runtime.split_model) gets a ProviderClient
here instead, which speaks the same shape over plain httpx, so the callers, the usage
accounting and the tests that pin the Anthropic request shape do not change.

Wire formats, checked against each provider's docs on 2026-11-14 (docs/mycelium.md has
the list): OpenAI and xAI take the Responses API (`/v1/responses`; xAI calls its chat
completions endpoint legacy), OpenRouter takes chat completions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx

from penish import runtime
from penish.config import Settings, get_settings
from penish.runtime import Pool
from penish.worker.jobs import SoftError

log = logging.getLogger(__name__)

# Reasoning models with an image can think for minutes before the first byte.
TIMEOUT = httpx.Timeout(600.0, connect=15.0)
# Provider error text goes into jobs.last_error; a whole HTML error page would not help.
ERROR_CAP = 300
# 408 is the provider saying the request timed out, which is a timeout like any other.
_RETRY_STATUS = (408, 429)


@dataclass(frozen=True)
class Spec:
    name: str
    url: str
    wire: Literal["responses", "chat"]
    key: str
    # Media types sent as they are; anything else is re-encoded as jpeg first.
    images: frozenset[str]


SPECS: dict[str, Spec] = {
    "openai": Spec(
        "openai",
        "https://api.openai.com/v1/responses",
        "responses",
        "openai_api_key",
        frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    ),
    # xAI documents jpg and png only, and the describer sends webp.
    "xai": Spec(
        "xai",
        "https://api.x.ai/v1/responses",
        "responses",
        "xai_api_key",
        frozenset({"image/png", "image/jpeg"}),
    ),
    # OpenRouter itself accepts webp, but a request can be routed to an upstream that
    # does not (its docs name one), and a jpeg is accepted everywhere.
    "openrouter": Spec(
        "openrouter",
        "https://openrouter.ai/api/v1/chat/completions",
        "chat",
        "openrouter_api_key",
        frozenset({"image/png", "image/jpeg"}),
    ),
}
assert set(SPECS) == set(runtime.PROVIDERS)

# Tests swap in an httpx.MockTransport here; None is the real network.
transport: httpx.AsyncBaseTransport | None = None


class ProviderError(Exception):
    """describe.call_text turns this into a DescribeError with the same retryability."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class Response:
    content: list[TextBlock] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def keyed(settings: Settings | None = None) -> list[str]:
    """Names of the providers with a key set, Anthropic first. Never the keys themselves."""
    s = settings or get_settings()
    out = ["anthropic"] if s.anthropic_api_key else []
    return out + [p for p in runtime.PROVIDERS if getattr(s, SPECS[p].key)]


def prefixed(model: str) -> bool:
    """True when the id names a non-Anthropic provider. ValueError for an unknown prefix."""
    return runtime.split_model(model)[0] is not None


# -- effort ---------------------------------------------------------------

_OPENAI_REASONING = re.compile(r"^(gpt-(\d+)|o\d)")
# xAI lists grok-4.5, 4.6 and 4.7 as taking reasoning effort, xhigh from 4.6 on. Matched
# by name: grok-4.20 sorts above 4.7 as a number but is a different line.
_XAI_EFFORT = {"grok-4.5": "high", "grok-4.6": "xhigh", "grok-4.7": "xhigh"}
_XAI_NEWER = re.compile(r"^grok-([5-9]|\d{2,})(\.|-|$)")
_ORDER = ("low", "medium", "high", "xhigh", "max")


def _clamp(effort: str, top: str) -> str:
    return effort if _ORDER.index(effort) <= _ORDER.index(top) else top


def map_effort(provider: str, model_id: str, effort: str | None) -> str | None:
    """The provider's reasoning effort for a runtime effort, or None to leave the knob out.

    A model that takes no effort rejects the field, so it is sent only where the docs say
    the model reads it. OpenRouter normalizes `reasoning.effort` itself and its upstreams
    ignore parameters they do not support, so it always gets the value.
    """
    if not effort:
        return None
    if provider == "openrouter":
        return effort
    if provider == "openai":
        m = _OPENAI_REASONING.match(model_id)
        if not m:
            return None
        if m.group(2) is None:
            return _clamp(effort, "high")  # o-series
        major = int(m.group(2))
        # gpt-4 and earlier do not reason; gpt-6 and later take every level up to max.
        if major < 5:
            return None
        return effort if major >= 6 else _clamp(effort, "high")
    if provider == "xai":
        base = re.sub(r"-(fast|reasoning|latest|\d{4})$", "", model_id)
        if base in _XAI_EFFORT:
            return _clamp(effort, _XAI_EFFORT[base])
        if _XAI_NEWER.match(model_id):
            return _clamp(effort, "xhigh")
        return None
    return None


# -- request --------------------------------------------------------------


def to_jpeg(data: bytes) -> bytes:
    import pyvips  # Imported here: the api never sends images and need not load libvips.

    img = pyvips.Image.new_from_buffer(data, "")
    if img.hasalpha():
        img = img.flatten(background=[255, 255, 255])
    out: bytes = img.write_to_buffer(".jpg", Q=90)
    return out


async def _data_url(spec: Spec, block: dict[str, Any]) -> str:
    source = block.get("source") or {}
    if source.get("type") != "base64":
        raise ProviderError(f"{spec.name}: only base64 images are sent", retryable=False)
    mime = str(source.get("media_type", ""))
    data = str(source.get("data", ""))
    if mime not in spec.images:
        raw = await asyncio.to_thread(to_jpeg, base64.b64decode(data))
        mime, data = "image/jpeg", base64.b64encode(raw).decode()
    return f"data:{mime};base64,{data}"


def _system_text(system: Any) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    # Text blocks, possibly carrying cache_control, which no other provider reads.
    return "\n\n".join(b["text"] for b in system if b.get("type") == "text")


async def _parts(spec: Spec, content: Any) -> list[dict[str, Any]] | str:
    if isinstance(content, str):
        return content
    text_type = "input_text" if spec.wire == "responses" else "text"
    out: list[dict[str, Any]] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            out.append({"type": text_type, "text": block["text"]})
        elif kind == "image":
            url = await _data_url(spec, block)
            if spec.wire == "responses":
                out.append({"type": "input_image", "image_url": url})
            else:
                out.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ProviderError(f"{spec.name}: unsupported block {kind}", retryable=False)
    return out


def _flat(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


_KNOWN_KWARGS = {"max_tokens", "system", "messages", "output_config", "temperature"}


async def build_request(spec: Spec, model_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The provider's JSON body for Anthropic-shaped kwargs."""
    unknown = set(kwargs) - _KNOWN_KWARGS
    if unknown:
        # Better a clear failure than a request that silently loses an option.
        raise ProviderError(
            f"{spec.name}: unsupported option {', '.join(sorted(unknown))}", retryable=False
        )
    cfg = kwargs.get("output_config") or {}
    fmt = cfg.get("format")
    schema = fmt.get("schema") if fmt and fmt.get("type") == "json_schema" else None
    effort = map_effort(spec.name, model_id, cfg.get("effort"))
    system = _system_text(kwargs.get("system"))
    body: dict[str, Any] = {"model": model_id}

    if spec.wire == "responses":
        items: list[dict[str, Any]] = []
        for m in kwargs.get("messages", []):
            if m["role"] == "assistant":
                # Output parts are typed differently; an earlier turn only needs its words.
                items.append({"role": "assistant", "content": _flat(m["content"])})
            else:
                items.append({"role": m["role"], "content": await _parts(spec, m["content"])})
        body["input"] = items
        if system:
            body["instructions"] = system
        # Nothing here is ever fetched back, and xAI advises against storing image requests.
        body["store"] = False
        if "max_tokens" in kwargs:
            body["max_output_tokens"] = kwargs["max_tokens"]
        if schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": schema,
                    "strict": True,
                }
            }
        if effort:
            body["reasoning"] = {"effort": effort}
    else:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in kwargs.get("messages", []):
            content = _flat(m["content"]) if m["role"] == "assistant" else m["content"]
            msgs.append({"role": m["role"], "content": await _parts(spec, content)})
        body["messages"] = msgs
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            }
        if effort:
            body["reasoning"] = {"effort": effort}
    if "temperature" in kwargs:
        body["temperature"] = kwargs["temperature"]
    return body


# -- response -------------------------------------------------------------


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_response(spec: Spec, data: dict[str, Any]) -> Response:
    usage = data.get("usage") or {}
    if spec.wire == "responses":
        texts = [
            part.get("text", "")
            for item in data.get("output") or []
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        ]
        u = Usage(_int(usage.get("input_tokens")), _int(usage.get("output_tokens")))
    else:
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        content = msg.get("content")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
        else:
            texts = [content] if isinstance(content, str) else []
        u = Usage(_int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens")))
    # A refusal or an empty answer comes back as no text, as it does from Anthropic, and
    # the caller's validation retries it.
    return Response(content=[TextBlock(t) for t in texts if t], usage=u)


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:ERROR_CAP]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or ""
    elif err:
        msg = err
    else:
        msg = data.get("message", "") if isinstance(data, dict) else ""
    return str(msg)[:ERROR_CAP]


def _retryable(status: int) -> bool:
    return status in _RETRY_STATUS or status >= 500


# -- client ---------------------------------------------------------------


class _Messages:
    def __init__(self, client: ProviderClient) -> None:
        self._client = client

    async def create(self, *, model: str, **kwargs: Any) -> Response:
        return await self._client.create(model, kwargs)


class ProviderClient:
    """Duck-types `anthropic.AsyncAnthropic` as far as describe.call_text uses it."""

    def __init__(
        self,
        provider: str,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = TIMEOUT,
    ) -> None:
        self.spec = SPECS[provider]
        self._key = api_key
        self._transport = transport
        self._timeout = timeout
        self.messages = _Messages(self)

    async def create(self, model: str, kwargs: dict[str, Any]) -> Response:
        provider, model_id = runtime.split_model(model)
        if provider != self.spec.name:
            raise ProviderError(f"{model} is not a {self.spec.name} model", retryable=False)
        if not self._key:
            raise ProviderError(f"no key for {self.spec.name}", retryable=False)
        body = await build_request(self.spec, model_id, kwargs)
        # No attribution headers for OpenRouter: they are optional and would name the site.
        headers = {"Authorization": f"Bearer {self._key}"}
        name = self.spec.name
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
                resp = await http.post(self.spec.url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{name}: timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            # The exception text is about the connection, never the request body.
            raise ProviderError(f"{name}: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"{name} {resp.status_code}: {_message(resp)}",
                retryable=_retryable(resp.status_code),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"{name}: response is not json", retryable=True) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"{name}: response is not an object", retryable=True)
        # OpenRouter can answer 200 with an upstream error in the body instead of choices.
        err = data.get("error")
        if err and not data.get("choices") and not data.get("output"):
            code = err.get("code") if isinstance(err, dict) else None
            status = code if isinstance(code, int) else 502
            raise ProviderError(f"{name} {status}: {_message(resp)}", retryable=_retryable(status))
        return parse_response(self.spec, data)


def client_for(model: str, settings: Settings | None = None) -> ProviderClient:
    provider, _ = runtime.split_model(model)
    if provider is None:
        raise ValueError(f"{model} is an Anthropic model")
    s = settings or get_settings()
    return ProviderClient(provider, getattr(s, SPECS[provider].key), transport=transport)


# -- daily cap ------------------------------------------------------------


async def check_daily_cap(pool: Pool, *, now: datetime | None = None) -> None:
    """Call before every capped (extract, name) model call. At or over the cap it raises a
    SoftError that queues the job again at the next UTC midnight without using an attempt.

    Two workers can both pass at cap minus one, so the cap can be overrun by the job
    loop's concurrency; it bounds spend, it is not an exact quota.
    """
    cap = int(await runtime.get(pool, "mycelium.daily_calls"))
    used = await runtime.calls_today(pool, now)
    if used >= cap:
        raise SoftError(f"daily cap {used}/{cap}", run_after=runtime.next_midnight(now))
