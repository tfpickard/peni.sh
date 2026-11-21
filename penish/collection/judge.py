"""Which book a leaf belongs in, as a model reads it from the picture's own text.

Vectors place pictures by what they look like, and pictures of one medium look alike
whatever they show. The judge reads the caption, the description and the analysis beside
each of the collection's books with its title and anchor, and names a book by subject. The
vector scores still place the leaf within its book, and stand in whenever the judge is off,
fails or refuses, or names a book with no room.

Every answer is kept by what was asked (`input_hash`), so the same question is never paid
for twice. Nothing here takes a lock or writes a leaf: the caller asks first, then files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import jsonschema
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from penish import runtime
from penish.collection.numerals import parse_number, roman
from penish.config import Settings
from penish.runtime import Pool, UsageRecorder
from penish.text import no_em_dash
from penish.worker.describe import (
    DescribeError,
    LazyAnthropic,
    call_text,
    output_config,
    parse_json,
    rejected,
)

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]

MODEL_KEY = "filer.model"
PROMPT_KEY = "filer.prompt"
PURPOSE = "filer"
# A filing waits on this call; past a minute the scores file it instead.
TIMEOUT = 60.0
REASON_MAX = 200
# Kept for good: the same prompt gets the same refusal. Every other failure is asked again
# the next time the question comes up.
SETTLED = frozenset({"refused"})
_FIELDS = ("caption", "description", "analysis")

_SHAPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "book": {"type": "string"},
        "runner_up": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["book", "runner_up", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Texts:
    caption: str | None = None
    description: str | None = None
    analysis: str | None = None

    def any(self) -> bool:
        return any((self.caption, self.description, self.analysis))


@dataclass(frozen=True)
class ShownBook:
    """A book of the collection as the judge is shown it. Every book is shown, full or
    not: a book filling would otherwise change every leaf's question and void every kept
    answer. Whether the named book has room is the filing's business, after the answer."""

    number: int
    title: str | None
    anchor: str


@dataclass(frozen=True)
class Judgement:
    book: int
    runner_up: int
    reason: str
    model: str

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "book": self.book,
            "runner_up": self.runner_up,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Failed:
    """No judgement, and why: `refused`, `invalid`, `timeout` or `error` from the call, or
    `off`, `no text` or `full` from the filing around it."""

    error: str
    model: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error}
        if self.model:
            out["model"] = self.model
        if self.detail:
            out["detail"] = self.detail
        return out


Verdict = Judgement | Failed


def default_client(settings: Settings) -> Any:
    return LazyAnthropic(settings.anthropic_api_key)


# -- the question ------------------------------------------------------------------


def render(texts: Texts, books: Iterable[ShownBook]) -> str:
    """The user turn: the books first, so the leaf is read against them."""
    lines = ["The books of the codex. The leaf goes into one of them:", ""]
    for b in sorted(books, key=lambda x: x.number):
        head = f"book {roman(b.number)}"
        lines.append(f"{head}: {b.title}" if b.title else head)
        if b.anchor:
            lines.append(b.anchor)
        lines.append("")
    lines.append("The leaf:")
    for name in _FIELDS:
        body = getattr(texts, name)
        if body:
            lines.append("")
            lines.append(f"{name.capitalize()}:")
            lines.append(body.strip())
    return "\n".join(lines)


def schema(books: Iterable[ShownBook]) -> dict[str, Any]:
    # The books as an enum, so a model that honours the schema cannot name another.
    names = [roman(b.number) for b in sorted(books, key=lambda x: x.number)]
    props = {
        "book": {"type": "string", "enum": names},
        "runner_up": {"type": "string", "enum": names},
        "reason": {"type": "string"},
    }
    return {**_SHAPE, "properties": props}


def input_hash(model: str, prompt: str, texts: Texts, books: Iterable[ShownBook]) -> str:
    """Names the question. The prompt is in it as well: a rewritten prompt is a new one."""
    body = {
        "v": 1,
        "model": model,
        "prompt": prompt,
        **{name: getattr(texts, name) or "" for name in _FIELDS},
        "books": [
            [b.number, b.title or "", b.anchor] for b in sorted(books, key=lambda x: x.number)
        ],
    }
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _numeral(raw: str) -> int:
    try:
        return parse_number(raw)
    except ValueError:
        raise ValueError(f"no book {raw[:20]!r}") from None


def check(raw: Any, books: Iterable[ShownBook]) -> tuple[int, int, str]:
    """(book, runner_up, reason), or ValueError saying why the answer is not a judgement."""
    jsonschema.validate(raw, _SHAPE)
    numbers = {b.number for b in books}
    book = _numeral(raw["book"])
    runner_up = _numeral(raw["runner_up"])
    for n in (book, runner_up):
        if n not in numbers:
            raise ValueError(f"no book {n}")
    if book == runner_up:
        raise ValueError("book and runner-up are the same")
    reason = " ".join(raw["reason"].split())
    if not reason:
        raise ValueError("empty reason")
    # Converted rather than refused: the answer is paid for, and the dash is the house's
    # rule, not the model's mistake about the leaf.
    reason = no_em_dash(reason)
    if len(reason) > REASON_MAX:
        raise ValueError(f"reason is {len(reason)} characters")
    return book, runner_up, reason


async def ask(
    texts: Texts,
    books: list[ShownBook],
    *,
    model: str,
    prompt: str,
    client: Any,
    record: UsageRecorder | None = None,
    within: float = TIMEOUT,
) -> Verdict:
    """One call, no retries. Never raises: whatever goes wrong is a Failed, and the caller
    files by score."""
    kwargs: dict[str, Any] = {
        # Room for thinking on models that do it by default; the answer itself is short.
        "max_tokens": 2000,
        "system": prompt,
        "output_config": output_config(schema(books), None),
        "messages": [{"role": "user", "content": [{"type": "text", "text": render(texts, books)}]}],
    }
    try:
        reply = await asyncio.wait_for(
            call_text(client, model=model, create_kwargs=kwargs, record=record, purpose=PURPOSE),
            within,
        )
    except TimeoutError:
        log.warning("filer judge %s timed out after %.0fs", model, within)
        return Failed("timeout", model)
    except DescribeError as e:
        # call_text has logged it already.
        return Failed("refused" if e.refused else "error", model, str(e)[:200])
    except Exception as e:
        log.exception("filer judge %s failed", model)
        return Failed("error", model, str(e)[:200])
    try:
        book, runner_up, reason = check(parse_json(reply), books)
    except (ValueError, jsonschema.ValidationError) as e:
        rejected(reply, e, attempt=1, purpose=PURPOSE, model=model)
        return Failed("invalid", model, str(e)[:200])
    return Judgement(book, runner_up, reason, getattr(reply, "served", None) or model)


# -- the cache ---------------------------------------------------------------------


async def texts_of(conn: Conn, image_ids: Iterable[int]) -> dict[int, Texts]:
    ids = sorted(set(image_ids))
    out: dict[int, dict[str, str]] = {i: {} for i in ids}
    if not ids:
        return {}
    cur = await conn.execute(
        "select image_id, field, body from image_texts"
        " where image_id = any(%s) and field = any(%s)",
        (ids, list(_FIELDS)),
    )
    for r in await cur.fetchall():
        out[r["image_id"]][r["field"]] = r["body"]
    return {i: Texts(**f) for i, f in out.items()}


def _verdict(row: dict[str, Any]) -> Verdict:
    if row["error"] is not None:
        return Failed(row["error"], row["model"])
    return Judgement(int(row["book"]), int(row["runner_up"]), row["reason"], row["model"])


async def lookup(conn: Conn, collection_id: int, image_id: int, h: str) -> Verdict | None:
    cur = await conn.execute(
        "select model, book, runner_up, reason, error from collection_judgements"
        " where collection_id = %s and image_id = %s and input_hash = %s",
        (collection_id, image_id, h),
    )
    row = await cur.fetchone()
    return None if row is None else _verdict(row)


def settled(v: Verdict | None) -> bool:
    """A kept answer that is not asked again."""
    return isinstance(v, Judgement) or (isinstance(v, Failed) and v.error in SETTLED)


async def keep(
    conn: Conn, collection_id: int, image_id: int, h: str, asked: str, v: Verdict
) -> None:
    """A judgement replaces a failure kept for the same question, never the other way."""
    values: tuple[str, int | None, int | None, str | None, str | None]
    if isinstance(v, Judgement):
        values = (v.model, v.book, v.runner_up, v.reason, None)
    else:
        values = (v.model or asked, None, None, None, v.error)
    await conn.execute(
        """
        insert into collection_judgements
          (collection_id, image_id, input_hash, model, book, runner_up, reason, error)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (collection_id, image_id, input_hash) do update set
          model = excluded.model, book = excluded.book, runner_up = excluded.runner_up,
          reason = excluded.reason, error = excluded.error, created_at = now()
        where collection_judgements.error is not null
        """,
        (collection_id, image_id, h, *values),
    )


async def judged(
    pool: Pool,
    collection_id: int,
    image_id: int,
    texts: Texts,
    books: list[ShownBook],
    *,
    settings: Settings,
    client: Any = None,
    within: float = TIMEOUT,
) -> Verdict:
    """The kept answer to this question, or a new one, kept. Takes no lock."""
    get = runtime.getter(pool, settings)
    model = str(await get(MODEL_KEY))
    prompt = str(await get(PROMPT_KEY))
    h = input_hash(model, prompt, texts, books)
    async with pool.connection() as conn:
        hit = await lookup(conn, collection_id, image_id, h)
    if hit is not None and settled(hit):
        return hit
    v = await ask(
        texts,
        books,
        model=model,
        prompt=prompt,
        client=client if client is not None else default_client(settings),
        record=runtime.usage_recorder(pool, PURPOSE),
        within=within,
    )
    async with pool.connection() as conn:
        await keep(conn, collection_id, image_id, h, model, v)
    return v
