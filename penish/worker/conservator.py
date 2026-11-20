"""The conservator's note on a collection leaf: the object, never the picture's meaning.

Written once a leaf is filed, and on demand from the admin. The model is told the
collection's own record, where the leaf sits, where it sat before, and the notes already
written in its book, so hands, inks and repairs stay named the same way across a book.
An owner note is never replaced: the job skips it, and the write is conditional on it too.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jsonschema

from penish import runtime
from penish.collection import store
from penish.collection.numerals import roman
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

JOB = store.NOTE_JOB
MAX_SENTENCES = 4
MAX_CHARS = 600

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}

# The words by which a note starts reacting to the leaf instead of recording it.
_REACTS = re.compile(
    r"\b(unusual(ly)?|curious(ly)?|remarkabl[ey]|interesting(ly)?|notabl[ey]|strange(ly)?|"
    r"oddly|surprising(ly)?|striking(ly)?|mysterious(ly)?|puzzling|peculiar(ly)?|"
    r"bizarre|enigmatic|intriguing(ly)?|apparently|seems?|suggests?|depicts?|"
    r"appears to (depict|show|represent))\b",
    re.I,
)
# Names that stay unsaid however sure the model is of them.
_UNSAID = re.compile(
    r"\b(rak[oó]w|polish brethren|socinian\w*|arian(s|ism)?|byzantium|byzantine empire)\b",
    re.I,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


@dataclass(frozen=True)
class Note:
    text: str
    model: str


Writer = Callable[..., Awaitable[Note]]


def facts(ctx: store.NoteContext) -> str:
    """The text that goes beside the picture: the record, the place, the book's notes."""
    lines = ["Record of the Pansophicon:"]
    for label, value in ctx.record:
        parts = [p.strip() for p in value.splitlines() if p.strip()]
        if len(parts) <= 1:
            lines.append(f"{label}: {parts[0] if parts else ''}")
        else:
            lines.append(f"{label}:")
            lines.extend(f"  {p}" for p in parts)
    lines.append("")
    lines.append(
        f"This leaf: book {roman(ctx.book)}, leaf {ctx.position} of {ctx.leaves_per_book}."
    )
    if ctx.previous is not None:
        b, p = ctx.previous
        lines.append(f"Earlier place in the sequence: book {roman(b)}, leaf {p}.")
    lines.append("")
    if ctx.book_notes:
        lines.append(f"Notes already written for other leaves of book {roman(ctx.book)}:")
        lines.extend(f"leaf {pos}: {text}" for pos, text in ctx.book_notes)
    else:
        lines.append(f"No other leaf of book {roman(ctx.book)} has a note yet.")
    return "\n".join(lines)


def clean(raw: Any) -> str:
    """The note, or ValueError saying why it is not one."""
    jsonschema.validate(raw, SCHEMA)
    text = " ".join(no_em_dash(raw["note"]).split())
    if not text:
        raise ValueError("empty note")
    if len(text) > MAX_CHARS:
        raise ValueError(f"note is {len(text)} characters")
    if "?" in text or "!" in text:
        raise ValueError("note asks or exclaims")
    if m := _REACTS.search(text):
        raise ValueError(f"note uses {m.group(0)!r}")
    if m := _UNSAID.search(text):
        raise ValueError(f"note names {m.group(0)!r}")
    n = len(_SENTENCE_END.findall(text))
    if not 1 <= n <= MAX_SENTENCES:
        raise ValueError(f"note has {n} sentences")
    return text


async def write(
    image_bytes: bytes,
    mime: str,
    ctx: store.NoteContext,
    *,
    settings: Settings,
    pool: Pool | None = None,
    client: Any = None,
) -> Note:
    if client is None:
        client = LazyAnthropic(settings.anthropic_api_key)
    get = runtime.getter(pool, settings)
    model = await get("conservator.model")
    prompt = await get("conservator.prompt")
    kwargs: dict[str, Any] = {
        # Room for thinking on models that do it by default; the note itself is short.
        "max_tokens": 3000,
        "system": prompt,
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
                    {"type": "text", "text": facts(ctx)},
                ],
            }
        ],
    }
    record = runtime.usage_recorder(pool, "conservator")
    last: Exception | None = None
    text: str | None = None
    for attempt in (1, 2, 3):
        text = await call_text(
            client, model=model, create_kwargs=kwargs, record=record, purpose="conservator"
        )
        try:
            return Note(clean(parse_json(text)), model)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            rejected(text, exc, attempt=attempt, purpose="conservator", model=model)
    raise invalid("invalid conservator output", text, last)


async def handle_note(job: Job, *, pool: Pool, settings: Settings, writer: Any = None) -> None:
    # Imported here: admin_jobs imports this module's neighbours, which import ingest.
    from penish.worker.admin_jobs import _describe_input, find_original

    try:
        image_id = int(job.payload["image_id"])
        collection_id = int(job.payload["collection_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad {JOB} payload: {e}") from e
    rewrite = bool(job.payload.get("rewrite"))
    async with pool.connection() as conn:
        ctx = await store.note_context(conn, collection_id, image_id)
    if ctx is None:
        log.info("%s: image %s is not filed; skipping", JOB, image_id)
        return
    if ctx.note_source == "owner" or (ctx.note is not None and not rewrite):
        return
    orig = find_original(settings.media_orig, ctx.sha256)
    if orig is None:
        raise HardError(f"original for image {image_id} is missing")
    try:
        # The displayed pixels: what was cropped away is not part of the leaf as shown.
        data = await asyncio.to_thread(_describe_input, orig, ctx.crop)
    except ValueError as e:
        raise HardError(f"{JOB} input: {e}") from e
    try:
        note = await (writer or write)(data, "image/webp", ctx, settings=settings, pool=pool)
    except DescribeError as e:
        raise (SoftError if e.retryable else HardError)(f"{JOB}: {e}") from e
    async with pool.connection() as conn:
        stored = await store.store_model_note(
            conn, collection_id, image_id, note.text, note.model, rewrite=rewrite
        )
    if not stored:
        log.info("%s: image %s got a note meanwhile; kept", JOB, image_id)
        return
    log.info("%s: image %s noted", JOB, image_id)
