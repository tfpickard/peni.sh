"""Grow the haiku deck with model-written entries.

Usage: uv run python scripts/grow_haikus.py --n 200 [--batch 20] [--dry-run] [--model ...]
"""

import argparse
import asyncio
import importlib.util
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import jsonschema
import numpy as np
import psycopg

from penish.config import Settings, get_settings
from penish.worker.describe import call_text, parse_json
from penish.worker.haiku import _validate as validate_haiku

_ROOT = Path(__file__).resolve().parent.parent
_PROMPT = (_ROOT / "penish" / "prompts" / "grow_haikus.md").read_text()
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


def build_prompt(examples: list[Haiku], n: int) -> str:
    text = "\n\n".join("\n".join(e) for e in examples)
    return _PROMPT.replace("{examples}", text).replace("{n}", str(n))


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
    model: str,
    settings: Settings,
    client: Any,
    embedder: Any = None,
    rng: random.Random | None = None,
) -> Stats:
    rng = rng or random.Random()
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
            "system": build_prompt(examples, ask),
            "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
            "messages": [{"role": "user", "content": f"Write {ask} haikus."}],
        }
        text = await call_text(client, model=model, create_kwargs=kwargs)
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
    cur = await conn.execute("select l1, l2, l3 from haikus where active")
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


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    embedder: Any = None
    if embeddings_available():
        from penish.worker.embed import Embedder

        embedder = Embedder()
    else:
        print("warning: sentence_transformers missing; skipping semantic dedupe", file=sys.stderr)

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        pool = await load_pool(conn)
        if not pool:
            print("error: haiku pool is empty; nothing to use as examples", file=sys.stderr)
            return 1
        stats = await grow(
            pool=pool,
            n=args.n,
            batch=args.batch,
            model=args.model,
            settings=settings,
            client=client,
            embedder=embedder,
        )
        if args.dry_run:
            for h in stats.haikus:
                print("\n".join(h), end="\n\n")
        else:
            await insert(conn, stats.haikus)
    print(summary(stats))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=Settings().describe_model)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
