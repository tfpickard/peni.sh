"""File each picture into one of the gallery's categories, two at most.

Its own job rather than part of the description, so the description prompt stays the
owner's to edit, and so the whole collection can be filed without describing it again.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from importlib import resources
from typing import Any

import anthropic
import jsonschema

from penish import runtime
from penish.categories import CATEGORIES, MAX_PER_IMAGE, SLUGS
from penish.config import Settings
from penish.worker.describe import DescribeError, call_text, output_config, parse_json
from penish.worker.jobs import HardError, Job, Pool, SoftError

log = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string", "enum": list(SLUGS)}}
    },
    "required": ["categories"],
    "additionalProperties": False,
}


def prompt() -> str:
    body = resources.files("penish.prompts").joinpath("categorize.md").read_text("utf-8")
    listing = "\n".join(f"- `{slug}`: {note}" for slug, _, note in CATEGORIES)
    return body.replace("{categories}", listing)


def clean(raw: Any) -> list[str]:
    """Known slugs in the order given, no repeats, one to MAX_PER_IMAGE of them."""
    jsonschema.validate(raw, SCHEMA)
    out = list(dict.fromkeys(c for c in raw["categories"] if c in SLUGS))
    if not out:
        raise ValueError("no category")
    # Erotica is the one category asked for by name; trimming must not drop it.
    if len(out) > MAX_PER_IMAGE:
        keep = out[:MAX_PER_IMAGE]
        if "erotica" in out and "erotica" not in keep:
            keep[-1] = "erotica"
        out = keep
    return out


async def classify(
    image_bytes: bytes,
    mime: str,
    *,
    settings: Settings,
    pool: Pool | None = None,
    client: Any = None,
) -> tuple[list[str], str]:
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    get = runtime.getter(pool, settings)
    model = await get("describe.model")
    kwargs: dict[str, Any] = {
        "max_tokens": 2000,
        "system": prompt(),
        "output_config": output_config(SCHEMA, None),
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
                    {"type": "text", "text": "Which categories?"},
                ],
            }
        ],
    }
    record = runtime.usage_recorder(pool, "describe")
    last: Exception | None = None
    for _ in range(2):
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            return clean(parse_json(text)), model
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
    raise DescribeError(f"invalid categories: {last}", retryable=True)


async def handle_categorize(
    job: Job, *, pool: Pool, settings: Settings, classifier: Any = None
) -> None:
    # Imported here: admin_jobs imports ingest, which enqueues this job.
    from penish.worker.admin_jobs import _describe_input, find_original

    image_id = int(job.payload["image_id"])
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select i.sha256, i.crop,
                   exists (select 1 from image_categories c
                            where c.image_id = i.id and c.source = 'owner') as owned
              from images i where i.id = %s
            """,
            (image_id,),
        )
        row = await cur.fetchone()
    if row is None:
        log.info("categorize: image %s is gone; skipping", image_id)
        return
    if row["owned"]:
        return
    orig = find_original(settings.media_orig, row["sha256"])
    if orig is None:
        raise HardError(f"original for image {image_id} is missing")
    try:
        data = await asyncio.to_thread(_describe_input, orig, row["crop"])
    except ValueError as e:
        raise HardError(f"categorize input: {e}") from e
    try:
        cats, model = await (classifier or classify)(
            data, "image/webp", settings=settings, pool=pool
        )
    except DescribeError as e:
        raise (SoftError if e.retryable else HardError)(f"categorize: {e}") from e
    async with pool.connection() as conn, conn.transaction():
        # Rechecked inside the write: an owner choice made while the model ran wins.
        cur = await conn.execute(
            "select 1 from image_categories where image_id = %s and source = 'owner'",
            (image_id,),
        )
        if await cur.fetchone():
            return
        await conn.execute("delete from image_categories where image_id = %s", (image_id,))
        await conn.execute(
            """
            insert into image_categories (image_id, category, source, model)
            select %s, c, 'model', %s from unnest(%s::text[]) c
            """,
            (image_id, model, cats),
        )
    log.info("categorize: image %s is %s", image_id, ", ".join(cats))


async def enqueue_uncategorized(pool: Pool) -> int:
    """Every live picture without a category gets a job. Run at worker start, so the
    collection that predates categories is filed once and a lost job is made good."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key)
            select 'categorize', jsonb_build_object('image_id', i.id), 'categorize:' || i.id
              from images i
             where i.status = 'live'
               and not exists (select 1 from image_categories c where c.image_id = i.id)
             order by i.id
            on conflict do nothing
            """
        )
        return cur.rowcount
