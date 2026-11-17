"""Whether a picture shows actual nudity, and which kinds.

Its own call rather than part of the description, so it can be rerun alone, and so ingest
can run it first: the describer and the haiku writer are told the answer before they
write. A positive result tags the picture with the kinds and files it under erotica, both
with source 'detect', so a later negative result can take back exactly what it added.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from penish import runtime
from penish.config import Settings
from penish.text import no_em_dash
from penish.worker.describe import (
    DescribeError,
    LazyAnthropic,
    call_text,
    invalid,
    output_config,
    parse_json,
    rejected,
)
from penish.worker.jobs import HardError, Job, Pool, SoftError

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]

JOB = "nudity"
ALL_JOB = "nudity_all"
# (kind, what it means). The kind is also the tag; the note is for the detector only.
KINDS: tuple[tuple[str, str], ...] = (
    ("topless", "bare female breasts or nipples"),
    ("bare-buttocks", "bare buttocks"),
    ("full-frontal", "bare genitals or pubic area"),
    ("fully-nude", "a figure wearing nothing at all, whatever it turns toward the viewer"),
    ("sex-act", "sexual activity between bare figures"),
)
KIND_NAMES: tuple[str, ...] = tuple(k for k, _ in KINDS)
# Every positive result carries this tag as well, so one tag page holds them all.
TAG = "nudity"
CATEGORY = "erotica"
SOURCE = "detect"
_SEEN_MAX = 300

SCHEMA: dict[str, Any] = {
    "type": "object",
    # `seen` first: naming what is bare before choosing kinds keeps the kinds honest.
    "properties": {
        "seen": {"type": "string"},
        "kinds": {"type": "array", "items": {"type": "string", "enum": list(KIND_NAMES)}},
    },
    "required": ["seen", "kinds"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Nudity:
    kinds: tuple[str, ...]
    seen: str
    model: str

    @property
    def nude(self) -> bool:
        return bool(self.kinds)

    @property
    def tags(self) -> list[str]:
        return [TAG, *self.kinds] if self.kinds else []


Detector = Callable[..., Awaitable[Nudity]]


def prompt(template: str) -> str:
    listing = "\n".join(f"- `{kind}`: {note}" for kind, note in KINDS)
    return template.replace("{kinds}", listing)


def clean(raw: Any, model: str) -> Nudity:
    jsonschema.validate(raw, SCHEMA)
    got = set(raw["kinds"])
    # The detector's order is noise; a fixed order keeps tags and the admin view stable.
    kinds = tuple(k for k in KIND_NAMES if k in got)
    seen = no_em_dash(raw["seen"]).strip()[:_SEEN_MAX]
    return Nudity(kinds=kinds, seen=seen, model=model)


async def detect(
    image_bytes: bytes,
    mime: str,
    *,
    settings: Settings,
    pool: Pool | None = None,
    client: Any = None,
) -> Nudity:
    if client is None:
        client = LazyAnthropic(settings.anthropic_api_key)
    get = runtime.getter(pool, settings)
    model = await get("nudity.model")
    template = await get("nudity.prompt")
    kwargs: dict[str, Any] = {
        # Room for thinking on models that do it by default; the JSON itself is tiny.
        "max_tokens": 2000,
        "system": prompt(template),
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
                    {"type": "text", "text": "Is there nudity?"},
                ],
            }
        ],
    }
    record = runtime.usage_recorder(pool, "nudity")
    last: Exception | None = None
    text: str | None = None
    for attempt in (1, 2):
        text = await call_text(
            client, model=model, create_kwargs=kwargs, record=record, purpose="nudity"
        )
        try:
            return clean(parse_json(text), model)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            rejected(text, exc, attempt=attempt, purpose="nudity", model=model)
    raise invalid("invalid nudity output", text, last)


# -- storage ------------------------------------------------------------------


async def kinds_for(conn: Conn, image_id: int) -> tuple[str, ...] | None:
    """The stored kinds, empty for a clean result, None when never checked."""
    cur = await conn.execute("select kinds from image_nudity where image_id = %s", (image_id,))
    row = await cur.fetchone()
    return None if row is None else tuple(row["kinds"])


async def store(conn: Conn, image_id: int, n: Nudity) -> None:
    """Record a result and apply it. Rows an earlier result added go first, so a clean
    result takes them back and a changed one replaces them."""
    async with conn.transaction():
        await conn.execute(
            """
            insert into image_nudity (image_id, kinds, seen, model, checked_at)
            values (%s, %s, %s, %s, now())
            on conflict (image_id) do update set
              kinds = excluded.kinds, seen = excluded.seen, model = excluded.model,
              checked_at = now()
            """,
            (image_id, list(n.kinds), n.seen, n.model),
        )
        await conn.execute(
            "delete from tags where image_id = %s and source = %s", (image_id, SOURCE)
        )
        await conn.execute(
            "delete from image_categories where image_id = %s and source = %s",
            (image_id, SOURCE),
        )
        if not n.nude:
            return
        # A tag the describer or the owner already gave stays theirs.
        await conn.execute(
            """
            insert into tags (image_id, tag, source, model)
            select %s, t, %s, %s from unnest(%s::text[]) t
            on conflict do nothing
            """,
            (image_id, SOURCE, n.model, n.tags),
        )
        # Categories the owner chose are left as they are. Erotica sits beside the
        # medium rather than displacing it, so a clean rerun loses nothing by removing it.
        await conn.execute(
            """
            insert into image_categories (image_id, category, source, model)
            select %(id)s, %(cat)s, %(src)s, %(m)s
             where not exists (select 1 from image_categories
                                where image_id = %(id)s and source = 'owner')
            on conflict do nothing
            """,
            {"id": image_id, "cat": CATEGORY, "src": SOURCE, "m": n.model},
        )


# -- jobs ---------------------------------------------------------------------


async def handle_nudity(job: Job, *, pool: Pool, settings: Settings, detector: Any = None) -> None:
    # Imported here: admin_jobs imports ingest, which imports this module.
    from penish.worker.admin_jobs import _describe_input, find_original

    try:
        image_id = int(job.payload["image_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad nudity payload: {e}") from e
    async with pool.connection() as conn:
        cur = await conn.execute("select sha256, crop from images where id = %s", (image_id,))
        row = await cur.fetchone()
    if row is None:
        log.info("nudity: image %s is gone; skipping", image_id)
        return
    orig = find_original(settings.media_orig, row["sha256"])
    if orig is None:
        raise HardError(f"original for image {image_id} is missing")
    try:
        # The displayed pixels: nudity cropped away is not on the site.
        data = await asyncio.to_thread(_describe_input, orig, row["crop"])
    except ValueError as e:
        raise HardError(f"nudity input: {e}") from e
    try:
        found = await (detector or detect)(data, "image/webp", settings=settings, pool=pool)
    except DescribeError as e:
        raise (SoftError if e.retryable else HardError)(f"nudity: {e}") from e
    async with pool.connection() as conn:
        cur = await conn.execute("select 1 from images where id = %s", (image_id,))
        if await cur.fetchone() is None:
            return
        await store(conn, image_id, found)
    log.info("nudity: image %s is %s", image_id, ", ".join(found.kinds) or "clean")


async def enqueue_for(conn: Conn, ids: Sequence[int]) -> list[int]:
    """One job per picture; the ids that got a new one (a queued job absorbs a repeat)."""
    cur = await conn.execute(
        """
        insert into jobs (type, payload, dedupe_key)
        select %(job)s, jsonb_build_object('image_id', i.id), %(job)s || ':' || i.id
          from images i
         where i.id = any(%(ids)s)
         order by i.id
        on conflict do nothing
        returning (payload->>'image_id')::bigint as id
        """,
        {"job": JOB, "ids": list(ids)},
    )
    return [int(r["id"]) for r in await cur.fetchall()]


async def handle_nudity_all(job: Job, *, pool: Pool) -> None:
    # Pictures still in ingest are checked there, before they are described.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from images where status in ('live', 'unpublished') order by id"
        )
        ids = [int(r["id"]) for r in await cur.fetchall()]
        n = len(await enqueue_for(conn, ids)) if ids else 0
    log.info("nudity_all: enqueued %d", n)


async def enqueue_unchecked(pool: Pool) -> int:
    """Every live picture never checked gets a job. Run at worker start, so the collection
    that predates detection is checked once and a lost job is made good."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key)
            select %(job)s, jsonb_build_object('image_id', i.id), %(job)s || ':' || i.id
              from images i
             where i.status = 'live'
               and not exists (select 1 from image_nudity n where n.image_id = i.id)
             order by i.id
            on conflict do nothing
            """,
            {"job": JOB},
        )
        return cur.rowcount
