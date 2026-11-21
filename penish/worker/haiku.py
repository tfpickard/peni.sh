import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

import jsonschema

from penish import runtime
from penish.config import Settings
from penish.runtime import Getter, Pool
from penish.text import no_em_dash
from penish.worker import providers
from penish.worker.describe import (
    DescribeError,
    LazyAnthropic,
    call_text,
    invalid,
    output_config,
    parse_json,
    rejected,
)

log = logging.getLogger(__name__)

Haiku = tuple[str, str, str]

MAX_LINE = 60
N_EXAMPLES = 6
_STRINGS = {"type": "array", "items": {"type": "string"}}
# What the model is asked for. `drafts` is scratch that is never stored: it comes first and
# is required so the candidates are written before the pick, whether or not the model thinks.
_SCHEMA = {
    "type": "object",
    "properties": {"drafts": _STRINGS, "lines": _STRINGS},
    "required": ["drafts", "lines"],
    "additionalProperties": False,
}
# The retry after a reasoning_extraction refusal: the drafts in the reply are what that
# classifier objects to, so the model is asked for the survivor alone and drafts in its
# thinking. Only on refusal, because written drafts are the only way to be sure the
# drafting happens at all on a model whose adaptive thinking may skip it.
_LINES_ONLY = {
    "type": "object",
    "properties": {"lines": _STRINGS},
    "required": ["lines"],
    "additionalProperties": False,
}
_NO_DRAFTS = (
    "\n\nThis time the reply holds only `lines`, in the shape"
    ' {"lines": ["...", "...", "..."]}. Draft and strike before you answer, as above,'
    " and leave the drafts out of the reply."
)
REASONING_EXTRACTION = "reasoning_extraction"
# What a reply must hold. Looser than _SCHEMA because grow.py validates bare lines here too.
_REPLY = {"type": "object", "properties": {"lines": _STRINGS}, "required": ["lines"]}


async def load_examples(pool: Pool, n: int = N_EXAMPLES) -> list[Haiku]:
    # From the live deck, not the seed migration: the image ships no migrations folder, and
    # owner edits and deactivations should steer the voice. Random so the prompt does not
    # pull every haiku toward the same six. A gallery's haiku is about its room, not a
    # picture, so it is no example of the deck.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select l1, l2, l3 from haikus where active and colony_id is null"
            " order by random() limit %s",
            (n,),
        )
        rows = await cur.fetchall()
    return [(r["l1"], r["l2"], r["l3"]) for r in rows]


def build_prompt(template: str, examples: list[Haiku]) -> str:
    return template.replace("{examples}", "\n\n".join("\n".join(e) for e in examples))


def _validate(text: str) -> Haiku:
    data = parse_json(text)
    jsonschema.validate(data, _REPLY)
    lines = [no_em_dash(x).strip() for x in data["lines"]]
    if len(lines) != 3 or any(not x or len(x) > MAX_LINE for x in lines):
        raise ValueError("haiku must be three non-empty lines of at most 60 characters")
    return lines[0], lines[1], lines[2]


class Written(tuple[str, str, str]):
    """A haiku and the model that wrote it, provider prefix and all. A tuple, as Reply is a
    str, so every caller that unpacks three lines and every test writer that returns a
    plain tuple keeps working; a caller reads `getattr(h, "model", None)`."""

    model: str

    def __new__(cls, lines: Haiku, model: str) -> "Written":
        out = super().__new__(cls, lines)
        out.model = model
        return out


ClientFor = Callable[[str], Any]


async def _write(
    client: Any, model: str, kwargs: dict[str, Any], record: runtime.UsageRecorder | None
) -> Written:
    last: Exception | None = None
    text: str | None = None
    drafting = True
    for attempt in (1, 2):
        try:
            text = await call_text(
                client, model=model, create_kwargs=kwargs, record=record, purpose="haiku"
            )
        except DescribeError as e:
            if not (drafting and e.category == REASONING_EXTRACTION and attempt == 1):
                raise
            log.warning("haiku: %s refused the drafts; asking for the lines alone", model)
            drafting = False
            kwargs = {
                **kwargs,
                "system": kwargs["system"] + _NO_DRAFTS,
                "output_config": output_config(_LINES_ONLY, None),
            }
            continue
        try:
            return Written(_validate(text), getattr(text, "served", None) or model)
        except (ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last = exc
            rejected(text, exc, attempt=attempt, purpose="haiku", model=model)
    raise invalid("invalid haiku output", text, last)


async def _fall_back(
    refusal: DescribeError,
    *,
    model: str,
    get: Getter,
    settings: Settings,
    client_for: ClientFor,
    kwargs: dict[str, Any],
    record: runtime.UsageRecorder | None,
) -> Written:
    """haiku.fallback in order, after `model` refused. A transient error is raised as it is,
    so the queue backs off and the next attempt starts over; moving on would hand the
    picture to a model further down the list for a reason that clears up by itself."""
    keyed = set(providers.keyed(settings))
    failed = [(model, refusal)]
    for entry in await get(runtime.FALLBACK_KEY) or []:
        if entry == model:
            log.info("haiku: fallback %s is haiku.model; skipping", entry)
            continue
        try:
            provider, _ = runtime.split_model(entry)
        except ValueError as e:
            log.warning("haiku: fallback %s skipped: %s", entry, e)
            continue
        if provider is None:
            log.warning("haiku: fallback %s is not another provider; skipping", entry)
            continue
        if provider not in keyed:
            log.warning("haiku: no key for %s; skipping fallback %s", provider, entry)
            continue
        log.warning("haiku: asking fallback %s after %s", entry, failed[-1][0])
        try:
            return await _write(client_for(entry), entry, kwargs, record)
        except DescribeError as e:
            if e.retryable:
                raise
            # A refusal, or a request this entry can never serve (an id the provider does
            # not know): the same on every attempt, so the next entry gets its turn.
            log.warning("haiku: fallback %s did not write it: %s", entry, e)
            failed.append((entry, e))
    if len(failed) == 1:
        raise refusal
    # Every model by name, so the queue page says what was tried.
    raise DescribeError(
        "; ".join(str(e) if m in str(e) else f"{m}: {e}" for m, e in failed),
        retryable=False,
        category=refusal.category,
        refused=all(e.refused for _, e in failed),
    )


async def haiku_from(
    description: str,
    *,
    settings: Settings,
    pool: Pool | None = None,
    get: Getter | None = None,
    client: Any = None,
    client_for: ClientFor | None = None,
    examples: list[Haiku] | None = None,
    nudity: Sequence[str] | None = None,
) -> Written:
    """`get` overrides the runtime lookup (tests); `pool` supplies it, the examples and
    usage recording otherwise. `nudity` is the detector's kinds, which the prompt writes
    for differently. `client` serves haiku.model; `client_for` gives the client for a
    haiku.fallback entry (tests), the provider's own adapter otherwise."""
    if client is None:
        client = LazyAnthropic(settings.anthropic_api_key)
    get = get or runtime.getter(pool, settings)
    model = await get("haiku.model")
    template = await get("haiku.prompt")
    if examples is None:
        examples = await load_examples(pool) if pool is not None else []
    content = description
    if nudity:
        content = f"{description}\n\nNudity in this picture: {', '.join(nudity)}."
    kwargs: dict[str, Any] = {
        # The budget covers thinking too: haiku-5-5 thinks by default, and at 300 it spent
        # every token there and returned no text. The drafts share it as well.
        "max_tokens": 4096,
        "system": build_prompt(template, examples),
        "output_config": output_config(_SCHEMA, None),
        "messages": [{"role": "user", "content": content}],
    }
    record = runtime.usage_recorder(pool, "haiku")
    try:
        return await _write(client, model, kwargs, record)
    except DescribeError as e:
        if not e.refused:
            raise
        return await _fall_back(
            e,
            model=model,
            get=get,
            settings=settings,
            client_for=client_for or (lambda m: providers.client_for(m, settings)),
            kwargs=kwargs,
            record=record,
        )
