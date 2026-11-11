"""Grow the haiku deck with model-written entries.

Lives in the package, not scripts/, because the worker image ships only penish/ and the
admin's grow job runs there. scripts/grow_haikus.py is the CLI over this.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic
import jsonschema
import numpy as np
import psycopg
from psycopg.rows import tuple_row

from penish import runtime
from penish.config import Settings
from penish.runtime import Getter, Pool, UsageRecorder
from penish.worker.describe import call_text, output_config, parse_json
from penish.worker.haiku import _validate as validate_haiku

_SCHEMA = {
    "type": "object",
    "properties": {
        "haikus": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    },
    "required": ["haikus"],
    "additionalProperties": False,
}
N_EXAMPLES = 12
SIM_LIMIT = 0.9
# Batches that yield nothing new are bounded so a stubborn model cannot loop forever.
_MAX_BATCH_FACTOR = 4

Haiku = tuple[str, str, str]


@dataclass
class Stats:
    accepted: int = 0
    dup: int = 0
    invalid: int = 0
    haikus: list[Haiku] = field(default_factory=list)


def normalize(h: Haiku) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", "", " ".join(h).lower()).split())


def _flat(h: Haiku) -> str:
    return " ".join(h)


def build_prompt(template: str, examples: list[Haiku], n: int) -> str:
    text = "\n\n".join("\n".join(e) for e in examples)
    return template.replace("{examples}", text).replace("{n}", str(n))


def _unit(m: np.ndarray) -> np.ndarray:
    # Re-normalized here so the threshold is a true cosine whatever the embedder returns.
    arr = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    out: np.ndarray = arr / np.where(norms == 0, 1, norms)
    return out


def embeddings_available() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


async def grow(
    *,
    pool: list[Haiku],
    n: int,
    batch: int,
    settings: Settings,
    client: Any,
    model: str | None = None,
    embedder: Any = None,
    rng: random.Random | None = None,
    get: Getter | None = None,
    record: UsageRecorder | None = None,
) -> Stats:
    """`pool` is the existing deck (examples and dedupe), not a db pool. `model` None means
    the runtime describe.model; the prompt is always the runtime grow.prompt."""
    rng = rng or random.Random()
    get = get or runtime.getter(None, settings)
    template = await get("grow.prompt")
    if model is None:
        model = await get("grow.model")
    stats = Stats()
    seen = {normalize(h) for h in pool}
    # Row-stacked unit vectors of everything already in the deck plus this run's accepts.
    vecs: np.ndarray | None = None
    if embedder is not None and pool:
        vecs = _unit(await embedder.embed_documents([_flat(h) for h in pool]))

    for _ in range(max(1, -(-n // batch)) * _MAX_BATCH_FACTOR):
        if stats.accepted >= n:
            break
        ask = min(batch, n - stats.accepted)
        examples = rng.sample(pool, min(N_EXAMPLES, len(pool)))
        kwargs: dict[str, Any] = {
            "max_tokens": max(1024, ask * 150),
            "system": build_prompt(template, examples, ask),
            "output_config": output_config(_SCHEMA, None),
            "messages": [{"role": "user", "content": f"Write {ask} haikus."}],
        }
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            data = parse_json(text)
            jsonschema.validate(data, _SCHEMA)
        except (ValueError, jsonschema.ValidationError):
            stats.invalid += 1
            continue

        fresh: list[Haiku] = []
        for entry in data["haikus"]:
            try:
                # Reuses haiku.py's line checks and em dash fix; its input is the JSON text.
                h = validate_haiku(json.dumps({"lines": entry}))
            except (ValueError, jsonschema.ValidationError):
                stats.invalid += 1
                continue
            key = normalize(h)
            if key in seen:
                stats.dup += 1
                continue
            seen.add(key)
            fresh.append(h)

        if embedder is not None and fresh:
            cand = _unit(await embedder.embed_documents([_flat(h) for h in fresh]))
            for h, v in zip(fresh, cand, strict=True):
                if stats.accepted >= n:
                    break
                if vecs is not None and float((vecs @ v).max()) > SIM_LIMIT:
                    stats.dup += 1
                    continue
                vecs = v[None, :] if vecs is None else np.vstack([vecs, v])
                stats.haikus.append(h)
                stats.accepted += 1
        else:
            for h in fresh:
                if stats.accepted >= n:
                    break
                stats.haikus.append(h)
                stats.accepted += 1
    return stats


async def load_pool(conn: psycopg.AsyncConnection[Any]) -> list[Haiku]:
    # Tuple rows whatever the connection's row factory, so both the CLI's plain
    # connection and the worker pool's dict-row connections work.
    async with conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute("select l1, l2, l3 from haikus where active")
        return [(a, b, c) for a, b, c in await cur.fetchall()]


async def insert(conn: psycopg.AsyncConnection[Any], haikus: list[Haiku]) -> None:
    async with conn.cursor() as cur:
        await cur.executemany(
            "insert into haikus (l1, l2, l3, source, image_id) values (%s, %s, %s, 'model', null)"
            " on conflict do nothing",
            haikus,
        )


def summary(s: Stats) -> str:
    return f"accepted {s.accepted}, rejected {s.dup + s.invalid} (dup {s.dup}, invalid {s.invalid})"


async def grow_into(
    db: Pool,
    n: int,
    *,
    settings: Settings,
    batch: int = 20,
    client: Any = None,
    embedder: Any = None,
    rng: random.Random | None = None,
) -> Stats:
    """Grow the deck by up to n and insert the accepts; the core of the admin grow job."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    async with db.connection() as conn:
        deck = await load_pool(conn)
    if not deck:
        raise ValueError("haiku pool is empty; nothing to use as examples")
    stats = await grow(
        pool=deck,
        n=n,
        batch=batch,
        settings=settings,
        client=client,
        embedder=embedder,
        rng=rng,
        get=runtime.getter(db, settings),
        record=runtime.usage_recorder(db, "grow"),
    )
    async with db.connection() as conn:
        await insert(conn, stats.haikus)
    return stats
