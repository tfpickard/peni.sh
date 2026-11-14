"""Naming: the clerk titles a fruited gallery and files a haiku for it, in one call.

Outside the tick, which never calls a model. A scheduler (`schedule`, from a loop in
the worker and right after the admin turns naming on) queues `mycelium_name` jobs for
living galleries that have a serial and no title, oldest fruiting first, at most
`name_per_day` per UTC day (counted from `named` events) and `name_batch` waiting at
once. The admin's regenerate queues one for a single gallery, dead ones included.

The switch (`mycelium_state.naming`) gates every call: the scheduler and the admin
check it before queueing, the call before each attempt (a retry included) and the
handler again in the transaction that writes, so a call already made when the switch
goes off is thrown away and no new one is made. An owner
title is never overwritten. Every call also counts against the daily cap.

The model sees the captions of the live members, the tags two or more of them share
and the phrases of their needs that are being fed. Need phrases are private: they go
to the model and nowhere else, and an answer that repeats one is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anthropic
import jsonschema
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish import runtime
from penish.config import Settings
from penish.mycelium import colonies as colony
from penish.mycelium import store as mycelium
from penish.text import no_em_dash
from penish.worker import haiku, providers
from penish.worker.describe import DescribeError, call_text, output_config, parse_json
from penish.worker.jobs import HardError, Job, Pool, SoftError

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]
Haiku = tuple[str, str, str]

JOB = "mycelium_name"
NAMED = "named"
OWNER = "owner"
MODEL = "model"
# The scheduler's wake. Cheap: one locked read and at most one insert.
INTERVAL = 60.0
TITLE_MAX = 60
TITLE_WORDS = 8
LINE_MAX = haiku.MAX_LINE
# A gallery holds at most max_size (12 by default) members; a dead one is read from its
# peak. Bounded anyway so a raised max_size cannot grow the prompt without limit.
CAPTIONS_MAX = 24
TAGS_MAX = 12
NEEDS_MAX = 8
# A need counts as being fed once its satisfaction has moved this far off zero: drift
# alone never brings one back to exactly zero, but it does bring it below this.
FED_MIN = 0.05
# A gallery whose naming job failed for good is left alone this long, so one that the
# model cannot name does not take every scheduler wake and the day's calls with it.
FAILED_PAUSE = "1 day"
LISTING_MAX = 300
_MAX_TOKENS = 4000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # No item bounds: not every provider's strict mode takes them; checked below.
        "haiku": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "haiku"],
    "additionalProperties": False,
}
_SPACE = re.compile(r"\s+")
_QUOTES = "\"'“”‘’`"


@dataclass(frozen=True, slots=True)
class Inputs:
    captions: list[str]
    tags: list[str]
    needs: list[str]


@dataclass(frozen=True, slots=True)
class Named:
    title: str
    haiku: Haiku
    model: str


Namer = Callable[..., Awaitable[Named]]


class NamingOff(Exception):
    pass


class NotHere(Exception):
    pass


class Busy(Exception):
    """A naming call for that gallery is already out."""


# -- the call ---------------------------------------------------------------


def clean_title(raw: str) -> str:
    """One line, em dashes converted, outer quotes and a trailing period dropped; empty
    stays empty. ValueError when too long."""
    t = _SPACE.sub(" ", no_em_dash(raw)).strip().strip(_QUOTES).strip().rstrip(".").strip()
    if len(t) > TITLE_MAX:
        raise ValueError("title too long")
    return t


def _leak(text: str, needs: Sequence[str]) -> str | None:
    low = text.lower()
    for p in needs:
        if p and re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", low):
            return p
    return None


def validate(raw: Any, needs: Sequence[str] = ()) -> tuple[str, Haiku]:
    """(title, haiku) from a parsed answer, or ValueError / jsonschema.ValidationError."""
    jsonschema.validate(raw, SCHEMA)
    title = clean_title(raw["title"]).lower()
    if not title:
        raise ValueError("empty title")
    if len(title.split()) > TITLE_WORDS:
        raise ValueError("title too many words")
    lines = [_SPACE.sub(" ", no_em_dash(x)).strip() for x in raw["haiku"]]
    if len(lines) != 3 or any(not x or len(x) > LINE_MAX for x in lines):
        raise ValueError(f"haiku must be three non-empty lines of at most {LINE_MAX} characters")
    # The model is told not to; a phrase that slipped through would be public.
    if _leak("\n".join([title, *lines]), needs) is not None:
        raise ValueError("answer repeats a need")
    return title, (lines[0], lines[1], lines[2])


def message(inputs: Inputs) -> str:
    captions = "\n".join(f"- {c}" for c in inputs.captions)
    tags = ", ".join(inputs.tags) if inputs.tags else "none"
    needs = "\n".join(f"- {n}" for n in inputs.needs) if inputs.needs else "none"
    return f"Captions:\n{captions}\n\nShared tags: {tags}\n\nNeeds being fed:\n{needs}"


async def name_gallery(
    inputs: Inputs,
    *,
    settings: Settings,
    pool: Pool | None = None,
    client: Any = None,
    get: runtime.Getter | None = None,
    examples: list[Haiku] | None = None,
) -> Named:
    """One call for a title and a haiku. With a pool, every attempt is checked against
    the switch (NamingOff) and the daily cap (SoftError with run_after when over) first,
    and recorded as `name`."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    get = get or runtime.getter(pool, settings)
    model = await get("name.model")
    template = await get("name.prompt")
    if examples is None:
        examples = await haiku.load_examples(pool) if pool is not None else []
    record = runtime.usage_recorder(pool, "name")
    kwargs: dict[str, Any] = {
        # Thinking shares this budget, as in describe.
        "max_tokens": _MAX_TOKENS,
        "system": haiku.build_prompt(template, examples),
        "output_config": output_config(SCHEMA, None),
        "messages": [{"role": "user", "content": message(inputs)}],
    }
    last: Exception | None = None
    for _ in range(2):
        if pool is not None:
            # A retry is a new paid call; one the switch went off for is not made.
            async with pool.connection() as conn:
                if not await naming_on(conn):
                    raise NamingOff
            await providers.check_daily_cap(pool)
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            title, lines = validate(parse_json(text), inputs.needs)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            continue
        return Named(title=title, haiku=lines, model=model)
    raise DescribeError(f"invalid name output: {last}", retryable=True)


# -- the switch and the rate ------------------------------------------------


async def naming_on(conn: Conn, *, lock: bool = False) -> bool:
    """The switch. `lock` takes the state row `for update`, which is always the first
    lock a naming write takes (store's lock order)."""
    sql = "select naming from mycelium_state where id = 1" + (" for update" if lock else "")
    cur = await conn.execute(sql.encode())
    row = await cur.fetchone()
    return bool(row and row["naming"])


async def set_naming(conn: Conn, on: bool) -> None:
    await conn.execute(
        "update mycelium_state set naming = %s, updated_at = now() where id = 1", (on,)
    )


async def named_today(conn: Conn, now: datetime | None = None) -> int:
    """`named` events since UTC midnight: every title and haiku the model wrote today,
    regenerates included."""
    cur = await conn.execute(
        "select count(*)::int as n from colony_events where kind = %s and scheduled_at >= %s",
        (NAMED, runtime.day_start(now)),
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def waiting(conn: Conn) -> int:
    cur = await conn.execute(
        "select count(*)::int as n from jobs where type = %s and status in ('queued', 'running')",
        (JOB,),
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


def _key(colony_id: int) -> str:
    return f"{JOB}:{colony_id}"


# A living gallery the scheduler may name: a serial, no title of any source, and at least
# one live member with a caption, so the job it gets always has something to send. The
# same test the handler applies, or a job that does nothing would be queued every wake.
_UNNAMED = f"""
    c.serial is not null and c.state = any(%(living)s) and c.title_source is null
    and exists (select 1 from colony_members m
                  join images i on i.id = m.image_id and i.status = 'live'
                  join image_texts t on t.image_id = i.id and t.field = 'caption'
                 where m.colony_id = c.id and m.left_tick is null)
    and not exists (select 1 from jobs j
                     where j.dedupe_key = '{JOB}:' || c.id
                       and (j.status in ('queued', 'running')
                            or (j.status = 'failed'
                                and j.updated_at > now() - interval '{FAILED_PAUSE}')))
"""


async def schedule(pool: Pool, *, now: datetime | None = None) -> int:
    """Queue naming for the oldest unnamed living galleries, within the day's rate and the
    batch bound, only while naming is on. Returns how many were queued.

    Holds the state row while it counts and inserts, so two schedulers (the worker's loop
    and an admin switching naming on) cannot both fill the same room. Waits for galleries
    to be public: before that every serial comes from a genesis that may be run again,
    which would throw the names away with the colonies.
    """
    async with pool.connection() as conn:
        # Off is the usual state; a plain read keeps the wake from queueing behind a tick
        # or a genesis for the state row.
        if not await naming_on(conn):
            return 0
    async with pool.connection() as conn, conn.transaction():
        if not await naming_on(conn, lock=True) or not await mycelium.galleries_public(conn):
            return 0
        t = await mycelium.load_tunables(conn)
        busy = await waiting(conn)
        room = min(t.name_per_day - await named_today(conn, now) - busy, t.name_batch - busy)
        if room <= 0:
            return 0
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key)
            select %(t)s, jsonb_build_object('colony_id', c.id), %(t)s || ':' || c.id
              from colonies c
             where {_UNNAMED}
             order by c.fruited_tick nulls last, c.serial
             limit %(room)s
            on conflict do nothing
            """.encode(),  # noqa: S608 -- _UNNAMED is a fixed literal from this module
            {"t": JOB, "living": sorted(colony.LIVING), "room": room},
        )
        return cur.rowcount


async def run_loop(pool: Pool, stop: asyncio.Event, *, interval: float = INTERVAL) -> None:
    """Beside the tick: wake, queue what the rate allows, sleep. Never takes the worker
    down."""
    while not stop.is_set():
        try:
            n = await schedule(pool)
            if n:
                log.info("queued %d galleries to name", n)
        except Exception:
            log.exception("naming scheduler failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


# -- inputs -----------------------------------------------------------------


async def _colony(conn: Conn, colony_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    cur = await conn.execute(
        (
            "select id, serial, state, layer, title, title_source, peak_members"
            " from colonies where id = %s" + (" for update" if lock else "")
        ).encode(),
        (colony_id,),
    )
    row: dict[str, Any] | None = await cur.fetchone()
    return row


async def _member_ids(conn: Conn, row: dict[str, Any]) -> list[int]:
    """Live pictures the gallery holds now, or held at its peak once it is dead."""
    if row["state"] == colony.DEAD:
        ids = [int(m["id"]) for m in row["peak_members"] or [] if m.get("id") is not None]
    else:
        cur = await conn.execute(
            "select image_id from colony_members"
            " where colony_id = %s and left_tick is null and image_id is not null",
            (row["id"],),
        )
        ids = [int(r["image_id"]) for r in await cur.fetchall()]
    if not ids:
        return []
    cur = await conn.execute(
        "select id from images where id = any(%s) and status = 'live' order by id", (ids,)
    )
    return [int(r["id"]) for r in await cur.fetchall()]


async def gather(conn: Conn, row: dict[str, Any], *, settings: Settings) -> Inputs:
    """What the model is told about one gallery. Only live pictures count."""
    ids = await _member_ids(conn, row)
    if not ids:
        return Inputs(captions=[], tags=[], needs=[])
    cur = await conn.execute(
        "select body from image_texts where image_id = any(%s) and field = 'caption'"
        " order by image_id limit %s",
        (ids, CAPTIONS_MAX),
    )
    captions = [str(r["body"]) for r in await cur.fetchall()]
    cur = await conn.execute(
        """
        select tag from tags where image_id = any(%s)
         group by tag having count(*) >= 2
         order by count(*) desc, tag limit %s
        """,
        (ids, TAGS_MAX),
    )
    tags = [str(r["tag"]) for r in await cur.fetchall()]
    cur = await conn.execute(
        """
        select phrase from offers_needs
         where image_id = any(%(ids)s) and role = 'need' and recipe = %(recipe)s
           and model = %(model)s and dim = %(dim)s and satisfaction >= %(fed)s
         group by phrase
         order by max(satisfaction) desc, phrase limit %(n)s
        """,
        {
            "ids": ids,
            "recipe": mycelium.PHRASE_RECIPE,
            "model": settings.embed_model,
            "dim": settings.embed_dim,
            "fed": FED_MIN,
            "n": NEEDS_MAX,
        },
    )
    needs = [str(r["phrase"]) for r in await cur.fetchall()]
    return Inputs(captions=captions, tags=tags, needs=needs)


def _wanted(row: dict[str, Any] | None, regenerate: bool) -> bool:
    if row is None or row["serial"] is None:
        return False
    if regenerate:
        return row["state"] in (*colony.LIVING, colony.DEAD)
    return row["state"] in colony.LIVING and row["title_source"] is None


# -- the job ----------------------------------------------------------------


async def write(pool: Pool, colony_id: int, named: Named, *, regenerate: bool) -> bool:
    """Store a title and its haiku; False when nothing was written.

    State row first, then the colony row, as the tick takes them. The switch and the
    owner rule are read again under those locks: a switch turned off, or a title the
    owner wrote, while the call was out wins over the answer.
    """
    async with pool.connection() as conn, conn.transaction():
        if not await naming_on(conn, lock=True):
            log.info("name: naming went off; dropping the answer for colony %s", colony_id)
            return False
        row = await _colony(conn, colony_id, lock=True)
        if not _wanted(row, regenerate):
            log.info("name: colony %s no longer wants a name", colony_id)
            return False
        assert row is not None
        if row["title_source"] != OWNER:
            await conn.execute(
                "update colonies set title = %s, title_source = %s, updated_at = now()"
                " where id = %s",
                (named.title, MODEL, colony_id),
            )
        # A regenerate that writes the same lines again brings that haiku back as the
        # gallery's current one rather than adding a twin.
        l1, l2, l3 = named.haiku
        await conn.execute(
            """
            insert into haikus (l1, l2, l3, source, colony_id) values (%s, %s, %s, 'model', %s)
            on conflict (colony_id, lower(l1 || E'\\n' || l2 || E'\\n' || l3))
              where colony_id is not null
            do update set active = true, created_at = now()
            """,
            (l1, l2, l3, colony_id),
        )
        await conn.execute(
            """
            insert into colony_events (tick, scheduled_at, kind, colony_id, layer, simulated)
            select coalesce(s.last_tick, 0), now(), %s, %s, %s, false
              from mycelium_state s where s.id = 1
            """,
            (NAMED, colony_id, row["layer"]),
        )
    return True


async def handle_name(
    job: Job, *, pool: Pool, settings: Settings, namer: Namer = name_gallery
) -> None:
    try:
        colony_id = int(job.payload["colony_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"name: bad payload {job.payload!r}") from e
    regenerate = bool(job.payload.get("regenerate"))
    async with pool.connection() as conn:
        if not await naming_on(conn):
            log.info("name: naming is off; colony %s not named", colony_id)
            return
        row = await _colony(conn, colony_id)
        if not _wanted(row, regenerate):
            log.info("name: colony %s does not want a name", colony_id)
            return
        assert row is not None
        inputs = await gather(conn, row, settings=settings)
    if not inputs.captions:
        log.info("name: colony %s has no live member with a caption", colony_id)
        return
    try:
        named = await namer(inputs, settings=settings, pool=pool)
    except NamingOff:
        log.info("name: naming went off; colony %s not named", colony_id)
        return
    except DescribeError as e:
        raise (SoftError if e.retryable else HardError)(f"name: {e}") from e
    if await write(pool, colony_id, named, regenerate=regenerate):
        log.info("name: gallery %s from %s", row["serial"], named.model)


# -- admin ------------------------------------------------------------------


async def set_title(conn: Conn, serial: int, title: str) -> None:
    """The owner's title, which nothing automatic overwrites. Empty gives the gallery
    back its serial name and to the clerk. NotHere for an unknown serial; ValueError for
    a title that will not do."""
    clean = clean_title(title)
    async with conn.transaction():
        await naming_on(conn, lock=True)
        cur = await conn.execute("select id from colonies where serial = %s for update", (serial,))
        row = await cur.fetchone()
        if row is None:
            raise NotHere(serial)
        await conn.execute(
            "update colonies set title = %s, title_source = %s, updated_at = now() where id = %s",
            (clean or None, OWNER if clean else None, row["id"]),
        )


async def regenerate(pool: Pool, serial: int) -> int:
    """Queue a naming job for one gallery, living or dead. NamingOff while the switch is
    off; NotHere for an unknown serial. A job already queued for it is turned into a
    regenerate, so a gallery that died while its automatic job waited is still named.
    Busy while one is running: its handler has read its payload already, so a merged
    regenerate would be dropped without a word."""
    async with pool.connection() as conn:
        if not await naming_on(conn):
            raise NamingOff
        cur = await conn.execute(
            "select id from colonies where serial = %s and state = any(%s)",
            (serial, [*sorted(colony.LIVING), colony.DEAD]),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotHere(serial)
        colony_id = int(row["id"])
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key) values (%s, %s, %s)
            on conflict (dedupe_key)
              where status in ('queued', 'running') and dedupe_key is not null
            do update set payload = jobs.payload || excluded.payload, updated_at = now()
              where jobs.status = 'queued'
            returning id
            """,
            (JOB, Jsonb({"colony_id": colony_id, "regenerate": True}), _key(colony_id)),
        )
        got = await cur.fetchone()
    if got is None:
        raise Busy(serial)
    return int(got["id"])


async def listing(conn: Conn, *, now: datetime | None = None) -> dict[str, Any]:
    """The admin's naming block, JSON-ready:
    `{on, named_today, queued, galleries: [{serial, state, layer, title, title_source,
    haiku: [l1, l2, l3] | null, queued}]}`, living and fading first, then dead, newest
    serial first, at most LISTING_MAX."""
    cur = await conn.execute(
        """
        select c.serial, c.state, c.layer, c.title, c.title_source,
               (select array[h.l1, h.l2, h.l3] from haikus h
                 where h.colony_id = c.id and h.active
                 order by h.created_at desc, h.id desc limit 1) as haiku,
               exists (select 1 from jobs j where j.dedupe_key = %(t)s || ':' || c.id
                         and j.status in ('queued', 'running')) as queued
          from colonies c
         where c.serial is not null and c.state = any(%(states)s)
         order by c.state = 'dead', c.serial desc
         limit %(n)s
        """,
        {"t": JOB, "states": list(mycelium.GALLERY_STATES), "n": LISTING_MAX},
    )
    rows = await cur.fetchall()
    return {
        "on": await naming_on(conn),
        "named_today": await named_today(conn, now),
        "queued": await waiting(conn),
        "galleries": [
            {
                "serial": int(r["serial"]),
                "state": mycelium.PUBLIC_STATES[r["state"]],
                "layer": mycelium.public_kind(r["layer"]),
                "title": r["title"],
                "title_source": r["title_source"],
                "haiku": list(r["haiku"]) if r["haiku"] else None,
                "queued": bool(r["queued"]),
            }
            for r in rows
        ],
    }
