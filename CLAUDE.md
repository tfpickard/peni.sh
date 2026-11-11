# peni.sh

Single-owner image gallery. Tom rsyncs files into an inbox; a Python worker
describes, embeds and publishes each image; every image page shows its most
similar and most dissimilar neighbors plus a few embedding metrics.

## House rules

- No em dashes anywhere (code, comments, docs, model output). Use `--`.
- Comments explain why, not what.
- Never commit or push unless asked.
- Model output never overwrites a text row with `source='owner'`.

## Voice and style

Minimalist and dry. Never wink, never explain the joke. Write as if the site
has always been this way. Applies to UI, errors, prompts and design.

- UI copy: plain lowercase labels, as few words as possible (`142 pictures`,
  `similar`, `dissimilar`, `loneliness 0.41`). No exclamation marks, emoji,
  "oops", or asides about the site being weird. No tooltips on metric names.
- Empty and error states are flat, no apology: `0 pictures`, `not here`.
- Model prompts ask for a deadpan, matter-of-fact voice that treats strange
  content as ordinary. Descriptions stay literal and precise.
- og tags: caption and description only. No taglines or calls to action.
- Visual: lots of empty dark ground, one type scale, no cards or shadows,
  almost no chrome. Magenta and cyan sparingly. The Fungal haiku is the only
  flourish.
- No meta: no about page, no copy explaining the embedding math.

## Stack

- Python 3.12, uv, package `penish` (config, db, storage, api/, worker/).
- FastAPI + psycopg3 pool + Postgres 17 with pgvector (untyped `vector`
  column, exact scan, queries always filter `kind` and `model`).
- Worker: watchfiles inbox watcher, SKIP LOCKED job loop, pyvips derivatives,
  Claude vision describer, local Qwen3-Embedding-0.6B (optional `worker` extra,
  CPU torch).
- Web: SvelteKit + adapter-static under `web/`, served by the api.
- Deploy: GHCR images, VPS pulls on a systemd timer. No inbound deploy path.

## Dev

    docker compose -f compose.yaml -f compose.dev.yaml up --build

Local python:

    uv sync                    # api deps + dev group
    uv sync --extra worker     # also torch + sentence-transformers

## Checks

    uv run ruff check .
    uv run ruff format .
    uv run mypy penish
    uv run pytest

Run ruff and mypy before calling work done.
