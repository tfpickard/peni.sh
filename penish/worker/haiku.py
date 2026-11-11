import json
import re
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

from penish.config import Settings
from penish.text import no_em_dash
from penish.worker.describe import DescribeError, call_text, parse_json

_ROOT = Path(__file__).resolve().parent.parent.parent
_PROMPT_TEMPLATE = (_ROOT / "penish" / "prompts" / "haiku.md").read_text()
_SEED_SQL = _ROOT / "migrations" / "002_seed_haikus.sql"

MAX_LINE = 60
_ROW = re.compile(
    r"\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\s*,\s*'seed'\s*\)"
)
_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}


def seed_examples(n: int = 6, path: Path = _SEED_SQL) -> list[tuple[str, str, str]]:
    rows = [
        (a.replace("''", "'"), b.replace("''", "'"), c.replace("''", "'"))
        for a, b, c in _ROW.findall(path.read_text())
    ]
    if len(rows) <= n:
        return rows
    # Evenly spaced and deterministic, so the prompt is stable between runs.
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def build_prompt() -> str:
    examples = "\n\n".join("\n".join(r) for r in seed_examples())
    return _PROMPT_TEMPLATE.replace("{examples}", examples)


def _validate(text: str) -> tuple[str, str, str]:
    data = parse_json(text)
    jsonschema.validate(data, _SCHEMA)
    lines = [no_em_dash(x).strip() for x in data["lines"]]
    if len(lines) != 3 or any(not x or len(x) > MAX_LINE for x in lines):
        raise ValueError("haiku must be three non-empty lines of at most 60 characters")
    return lines[0], lines[1], lines[2]


async def haiku_from(
    description: str, *, settings: Settings, client: Any = None
) -> tuple[str, str, str]:
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    kwargs: dict[str, Any] = {
        "max_tokens": 300,
        "system": build_prompt(),
        "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
        "messages": [{"role": "user", "content": description}],
    }
    last: Exception | None = None
    for _ in range(2):
        text = await call_text(client, model=settings.haiku_model, create_kwargs=kwargs)
        try:
            return _validate(text)
        except (ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last = exc
    raise DescribeError(f"invalid haiku output: {last}", retryable=True)
