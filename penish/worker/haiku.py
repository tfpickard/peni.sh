import json
from typing import Any

import anthropic
import jsonschema

from penish import runtime
from penish.config import Settings
from penish.runtime import Getter, Pool
from penish.text import no_em_dash
from penish.worker.describe import DescribeError, call_text, output_config, parse_json

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
# What a reply must hold. Looser than _SCHEMA because grow.py validates bare lines here too.
_REPLY = {"type": "object", "properties": {"lines": _STRINGS}, "required": ["lines"]}


async def load_examples(pool: Pool, n: int = N_EXAMPLES) -> list[Haiku]:
    # From the live deck, not the seed migration: the image ships no migrations folder, and
    # owner edits and deactivations should steer the voice. Random so the prompt does not
    # pull every haiku toward the same six.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select l1, l2, l3 from haikus where active order by random() limit %s", (n,)
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


async def haiku_from(
    description: str,
    *,
    settings: Settings,
    pool: Pool | None = None,
    get: Getter | None = None,
    client: Any = None,
    examples: list[Haiku] | None = None,
) -> Haiku:
    """`get` overrides the runtime lookup (tests); `pool` supplies it, the examples and
    usage recording otherwise."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    get = get or runtime.getter(pool, settings)
    model = await get("haiku.model")
    template = await get("haiku.prompt")
    if examples is None:
        examples = await load_examples(pool) if pool is not None else []
    kwargs: dict[str, Any] = {
        # The budget covers thinking too: haiku-5-5 thinks by default, and at 300 it spent
        # every token there and returned no text. The drafts share it as well.
        "max_tokens": 4096,
        "system": build_prompt(template, examples),
        "output_config": output_config(_SCHEMA, None),
        "messages": [{"role": "user", "content": description}],
    }
    record = runtime.usage_recorder(pool, "haiku")
    last: Exception | None = None
    for _ in range(2):
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            return _validate(text)
        except (ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last = exc
    raise DescribeError(f"invalid haiku output: {last}", retryable=True)
