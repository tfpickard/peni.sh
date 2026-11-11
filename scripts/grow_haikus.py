"""Grow the haiku deck with model-written entries.

Usage: uv run python scripts/grow_haikus.py --n 200 [--batch 20] [--dry-run] [--model ...]

Model and prompt default to the runtime settings (describe.model, grow.prompt), so the
CLI and the admin grow button write in the same voice.
"""

import argparse
import asyncio
import sys
from typing import Any

import anthropic
import psycopg

from penish import runtime
from penish.config import get_settings
from penish.db import make_pool
from penish.worker.grow import (
    Stats,
    build_prompt,
    embeddings_available,
    grow,
    insert,
    load_pool,
    normalize,
    summary,
)

__all__ = ["Stats", "build_prompt", "grow", "insert", "load_pool", "normalize", "summary"]


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    embedder: Any = None
    if embeddings_available():
        from penish.worker.embed import Embedder

        embedder = Embedder()
    else:
        print("warning: sentence_transformers missing; skipping semantic dedupe", file=sys.stderr)

    db = make_pool(settings.database_url, min_size=1, max_size=2)
    await db.open()
    try:
        async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
            deck = await load_pool(conn)
            if not deck:
                print("error: haiku pool is empty; nothing to use as examples", file=sys.stderr)
                return 1
            stats = await grow(
                pool=deck,
                n=args.n,
                batch=args.batch,
                model=args.model,
                settings=settings,
                client=client,
                embedder=embedder,
                get=runtime.getter(db, settings),
                record=runtime.usage_recorder(db, "grow"),
            )
            if args.dry_run:
                for h in stats.haikus:
                    print("\n".join(h), end="\n\n")
            else:
                await insert(conn, stats.haikus)
    finally:
        await db.close()
    print(summary(stats))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=None, help="default: the runtime describe.model")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
