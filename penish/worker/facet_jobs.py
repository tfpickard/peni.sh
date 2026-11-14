"""Facet jobs: `mycelium_split` cuts the private analysis into four facets, and
`mycelium_extract` asks one tailored vision call for them (plus offers and needs).

Both embed each body under its own kind (`facet:<name>/<recipe>`) with the local
embedder. Split is free and runs for everyone from a startup sweep; extract is paid,
capped per day, and runs at go-live and from the admin `extract all` button. Whatever
writes an extract also stores its offers and needs (phrase_jobs) in the same transaction.

Imports stay light (no ingest, no admin_jobs at module level): ingest and the api call
the invalidation helpers here, and admin_jobs registers the handlers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anthropic
import jsonschema
import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from penish import runtime
from penish.config import Settings
from penish.mycelium import facets
from penish.mycelium import store as mycelium
from penish.text import no_em_dash
from penish.worker import phrase_jobs, providers
from penish.worker.describe import DescribeError, call_text, output_config, parse_json
from penish.worker.jobs import HardError, Job, Pool, SoftError
from penish.worker.metrics import refresh_facet_stats

if TYPE_CHECKING:
    from penish.worker.ingest import DocumentEmbedder

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]

SPLIT_JOB = "mycelium_split"
EXTRACT_JOB = "mycelium_extract"
# Sweeps space their jobs out so an upload dropped mid-sweep is claimed next: the queue
# orders by run_after, and a job not yet due cannot sit in front of one that is. A split
# is a local embed of four short texts; an extract is a paid vision call. The spacing has
# to follow the cost: jobs that come due faster than they run pile up past due, and a
# past-due backlog is claimed before a fresh upload. So a split that will have to buy
# its facets is spaced like an extract.
SPLIT_STEP = timedelta(seconds=3)
EXTRACT_STEP = timedelta(seconds=30)
# A split waiting on the same picture's extract job asks again this long after it is due.
EXTRACT_WAIT = timedelta(minutes=1)
# Facet means are refreshed when a sweep has drained, or when this many vectors (or a
# tenth of the space, whichever is more) have arrived since the last refresh.
REFRESH_LAG = 25
MAX_PHRASES = 5
_MAX_TOKENS = 8000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{f: {"type": "string"} for f in facets.FACETS},
        # No maxItems: not every provider's strict mode takes array bounds, so the
        # limit is applied in clean() instead.
        "offers": {"type": "array", "items": {"type": "string"}},
        "needs": {"type": "array", "items": {"type": "string"}},
    },
    "required": [*facets.FACETS, "offers", "needs"],
    "additionalProperties": False,
}
# A value that opens with a facet's label was written as a labelled analysis, not as the
# one part asked for.
_LABELLED = re.compile(rf"^\s*({'|'.join(facets.FACETS)})\s*:", re.I | re.M)
_SPACE = re.compile(r"\s+")


@dataclass
class Extracted:
    facets: dict[str, str]
    offers: list[str]
    needs: list[str]
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


Extractor = Callable[..., Awaitable[Extracted]]


# -- the call ---------------------------------------------------------------


def _phrases(raw: list[str]) -> list[str]:
    out: dict[str, None] = {}
    for p in raw:
        s = _SPACE.sub(" ", no_em_dash(p)).strip().rstrip(".").strip().lower()
        if s:
            out.setdefault(s)
    return list(out)[:MAX_PHRASES]


def clean(raw: Any) -> tuple[dict[str, str], list[str], list[str]]:
    """(facets, offers, needs) from a parsed extract response, or ValueError /
    jsonschema.ValidationError. Over-long phrase lists are trimmed, not refused."""
    jsonschema.validate(raw, SCHEMA)
    out: dict[str, str] = {}
    for f in facets.FACETS:
        value = no_em_dash(raw[f]).strip()
        if not value:
            raise ValueError(f"empty {f}")
        if _LABELLED.search(value):
            raise ValueError(f"{f} has labels")
        out[f] = value
    return out, _phrases(raw["offers"]), _phrases(raw["needs"])


async def extract(
    image_bytes: bytes,
    mime: str,
    *,
    settings: Settings,
    pool: Pool | None = None,
    client: Any = None,
) -> Extracted:
    """One vision call for the four facets, offers and needs. With a pool, every attempt
    is checked against the daily cap first (SoftError with run_after when over)."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    get = runtime.getter(pool, settings)
    model = await get("extract.model")
    effort = await get("extract.effort")
    prompt = await get("extract.prompt")
    record = runtime.usage_recorder(pool, "extract")
    kwargs: dict[str, Any] = {
        # Thinking shares this budget, as in describe.
        "max_tokens": _MAX_TOKENS,
        "system": prompt,
        "output_config": output_config(SCHEMA, effort),
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
                    {"type": "text", "text": "Return the JSON object."},
                ],
            }
        ],
    }
    last: Exception | None = None
    for _ in range(2):
        if pool is not None:
            await providers.check_daily_cap(pool)
        text = await call_text(client, model=model, create_kwargs=kwargs, record=record)
        try:
            raw = parse_json(text)
            parts, offers, needs = clean(raw)
        except (ValueError, jsonschema.ValidationError) as exc:
            last = exc
            continue
        return Extracted(facets=parts, offers=offers, needs=needs, model=model, raw=raw)
    raise DescribeError(f"invalid extract output: {last}", retryable=True)


# -- invalidation (called inside the caller's transaction) ------------------


async def _enqueue_if_live(conn: Conn, job_type: str, image_id: int) -> None:
    await conn.execute(
        """
        insert into jobs (type, payload, dedupe_key)
        select %(t)s, jsonb_build_object('image_id', i.id), %(t)s || ':' || i.id
          from images i where i.id = %(id)s and i.status = 'live'
        on conflict do nothing
        """,
        {"t": job_type, "id": image_id},
    )


async def invalidate_split(conn: Conn, image_id: int) -> None:
    """The analysis changed: its split facets and their vectors go with the old words.

    Same transaction as the text write, so a crash cannot leave facets of a text that is
    gone. A live picture is queued for a new split there too; one that is not live yet
    gets its split at go-live. The caller marks the graph dirty, as it already does for
    the description vector.
    """
    await conn.execute(
        "delete from image_facets where image_id = %s and recipe = %s", (image_id, facets.SPLIT)
    )
    await conn.execute(
        "delete from embeddings where image_id = %s and kind = any(%s)",
        (image_id, list(facets.kinds(facets.SPLIT))),
    )
    await _enqueue_if_live(conn, SPLIT_JOB, image_id)


async def invalidate_extract(conn: Conn, image_id: int) -> None:
    """The pixels changed (a crop): the extract was a reading of the old ones.

    A new description does not come here; the extract call reads the image, not the
    analysis. Split rows with a model set go too: they are the fallback extract call's
    reading of the same old pixels, not a cut of the analysis, and an owner-written
    analysis survives the crop's redescribe, so nothing else would replace them.

    Offers and needs stay until the new extract replaces them: a need that comes back
    with the same words keeps its satisfaction, which a delete here would throw away.
    With the raw extract gone, the phrases backfill has nothing to rebuild them from.
    """
    await conn.execute("delete from image_extracts where image_id = %s", (image_id,))
    await conn.execute(
        "delete from image_facets where image_id = %s and recipe = %s",
        (image_id, facets.EXTRACT),
    )
    await conn.execute(
        "delete from embeddings where image_id = %s and kind = any(%s)",
        (image_id, list(facets.kinds(facets.EXTRACT))),
    )
    await _enqueue_if_live(conn, EXTRACT_JOB, image_id)
    cur = await conn.execute(
        "delete from image_facets where image_id = %s and recipe = %s and model is not null",
        (image_id, facets.SPLIT),
    )
    if cur.rowcount:
        await conn.execute(
            "delete from embeddings where image_id = %s and kind = any(%s)",
            (image_id, list(facets.kinds(facets.SPLIT))),
        )
        await _enqueue_if_live(conn, SPLIT_JOB, image_id)


# -- sweeps -----------------------------------------------------------------


_MISSING = """
    i.status = 'live'
    and (select count(*) from image_facets f
           join embeddings e on e.image_id = f.image_id
            and e.kind = 'facet:' || f.facet || '/' || f.recipe
            and e.model = %(model)s and e.dim = %(dim)s
          where f.image_id = i.id and f.recipe = %(recipe)s) < %(n)s
"""


async def _enqueue_missing(
    pool: Pool, job_type: str, recipe: str, step: timedelta, s: Settings
) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            insert into jobs (type, payload, dedupe_key, run_after)
            select %(t)s, jsonb_build_object('image_id', i.id), %(t)s || ':' || i.id,
                   now() + %(step)s * row_number() over (order by i.id)
              from images i
             where {_MISSING}
             order by i.id
            on conflict do nothing
            """.encode(),  # noqa: S608 -- _MISSING is a fixed literal from this module
            {
                "t": job_type,
                "step": step,
                "model": s.embed_model,
                "dim": s.embed_dim,
                "recipe": recipe,
                "n": len(facets.FACETS),
            },
        )
        return cur.rowcount


async def enqueue_unsplit(pool: Pool, settings: Settings) -> int:
    """Every live picture without all four split facets and vectors (under today's embed
    model) gets a job. Run at worker start, like the categorize sweep, so the collection
    that predates facets is split once and a lost job is made good.

    A picture whose analysis will not split and that has no extract to borrow (stored or
    queued) needs the paid fallback call, so its job goes on the extract grid; the rest
    are local embeds on the split grid.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            select i.id,
                   (select body from image_texts t
                     where t.image_id = i.id and t.field = 'analysis') as analysis,
                   (select count(*) from image_facets f
                     where f.image_id = i.id and f.recipe = %(recipe)s) = %(n)s as bodies,
                   exists (select 1 from image_extracts x where x.image_id = i.id)
                   or exists (select 1 from jobs j
                               where j.dedupe_key = %(ex)s || ':' || i.id
                                 and j.status in ('queued', 'running')) as borrow
              from images i
             where {_MISSING}
             order by i.id
            """.encode(),  # noqa: S608 -- _MISSING is a fixed literal from this module
            {
                "model": settings.embed_model,
                "dim": settings.embed_dim,
                "recipe": facets.SPLIT,
                "n": len(facets.FACETS),
                "ex": EXTRACT_JOB,
            },
        )
        rows = await cur.fetchall()
        ids: list[int] = []
        delays: list[timedelta] = []
        free = paid = 0
        for r in rows:
            if r["bodies"] or r["borrow"] or r["analysis"] is None or facets.split(r["analysis"]):
                free += 1
                delays.append(SPLIT_STEP * free)
            else:
                paid += 1
                delays.append(EXTRACT_STEP * paid)
            ids.append(int(r["id"]))
        if not ids:
            return 0
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key, run_after)
            select %(t)s, jsonb_build_object('image_id', x.id), %(t)s || ':' || x.id,
                   now() + x.delay
              from unnest(%(ids)s::bigint[], %(delays)s::interval[]) as x(id, delay)
             order by x.id
            on conflict do nothing
            """,
            {"t": SPLIT_JOB, "ids": ids, "delays": delays},
        )
        return cur.rowcount


async def enqueue_unextracted(pool: Pool, settings: Settings) -> int:
    """The admin `extract all`: live pictures without the extract recipe. A picture whose
    extract is stored but whose vectors are stale re-embeds without a call."""
    return await _enqueue_missing(pool, EXTRACT_JOB, facets.EXTRACT, EXTRACT_STEP, settings)


# -- shared job steps -------------------------------------------------------


async def _image(conn: Conn, image_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        """
        select i.id, i.sha256, i.pub_sha, i.crop, i.status::text as status,
               (select body from image_texts t
                 where t.image_id = i.id and t.field = 'analysis') as analysis
          from images i where i.id = %s
        """,
        (image_id,),
    )
    row: dict[str, Any] | None = await cur.fetchone()
    return row


async def _complete(conn: Conn, image_id: int, recipe: str, s: Settings) -> bool:
    cur = await conn.execute(
        f"select not ({_MISSING}) as done from images i where i.id = %(id)s".encode(),  # noqa: S608
        {
            "id": image_id,
            "model": s.embed_model,
            "dim": s.embed_dim,
            "recipe": recipe,
            "n": len(facets.FACETS),
        },
    )
    row = await cur.fetchone()
    return bool(row and row["done"])


async def _stored(
    conn: Conn, image_id: int, recipe: str
) -> tuple[dict[str, str], str | None] | None:
    """The four stored bodies of one recipe and their model, or None unless all four."""
    cur = await conn.execute(
        "select facet, body, model from image_facets where image_id = %s and recipe = %s",
        (image_id, recipe),
    )
    rows = await cur.fetchall()
    bodies = {r["facet"]: r["body"] for r in rows}
    if set(bodies) != set(facets.FACETS):
        return None
    return {f: bodies[f] for f in facets.FACETS}, rows[0]["model"]


async def _stored_extract(conn: Conn, image_id: int) -> Extracted | None:
    """An extract already paid for: from its facet rows, or failing those from its raw
    response."""
    cur = await conn.execute(
        "select model, body from image_extracts where image_id = %s", (image_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    raw = row["body"]
    try:
        parts, offers, needs = clean(raw)
    except (ValueError, jsonschema.ValidationError):
        return None
    got = await _stored(conn, image_id, facets.EXTRACT)
    if got is not None:
        parts = got[0]
    return Extracted(facets=parts, offers=offers, needs=needs, model=row["model"], raw=raw)


async def _embed(
    embedder: DocumentEmbedder, bodies: Mapping[str, str], s: Settings
) -> dict[str, np.ndarray]:
    out = await embedder.embed_documents([bodies[f] for f in facets.FACETS])
    vecs = np.asarray(out, dtype=np.float32)
    if vecs.shape != (len(facets.FACETS), s.embed_dim):
        raise HardError(f"facet embeddings have shape {vecs.shape}")
    return {f: vecs[i] for i, f in enumerate(facets.FACETS)}


def _input_bytes(settings: Settings, row: Mapping[str, Any]) -> bytes:
    # Imported here: admin_jobs imports ingest, which imports this module.
    from penish.worker.admin_jobs import _describe_input, find_original

    orig = find_original(settings.media_orig, row["sha256"])
    if orig is None:
        raise HardError(f"original for image {row['id']} is missing")
    try:
        # The displayed pixels, crop included, as the describer sees them.
        return _describe_input(orig, row["crop"])
    except ValueError as e:
        raise HardError(f"extract input: {e}") from e


async def _call(
    pool: Pool, job: Job, row: Mapping[str, Any], settings: Settings, extractor: Extractor
) -> Extracted:
    data = await asyncio.to_thread(_input_bytes, settings, row)
    try:
        return await extractor(data, "image/webp", settings=settings, pool=pool)
    except DescribeError as e:
        raise (SoftError if e.retryable else HardError)(f"extract: {e}") from e
    except SoftError as e:
        if e.run_after is None:
            raise
        # Over the daily cap. Everything deferred lines up behind what is already waiting
        # for the same reset, instead of all coming due at midnight in front of the day's
        # uploads. A split gets here only on its paid fallback, so both job types share
        # one line spaced for a vision call.
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select count(*) as n from jobs where type = any(%s) and status = 'queued'"
                " and run_after >= %s and id <> %s",
                ([SPLIT_JOB, EXTRACT_JOB], e.run_after, job.id),
            )
            n = await cur.fetchone()
        raise SoftError(
            str(e), run_after=e.run_after + EXTRACT_STEP * int(n["n"] if n else 0)
        ) from e


async def _phrase_vectors(
    pool: Pool, image_id: int, got: Extracted, settings: Settings, embedder: DocumentEmbedder
) -> dict[phrase_jobs.Pair, np.ndarray] | None:
    """Vectors for the extract's offers and needs, or None when the stored ones already
    are exactly these under today's embed model (a re-embed of the facets alone, or a
    re-extraction that came back with the same phrases)."""
    want = phrase_jobs.pairs(got.offers, got.needs)
    async with pool.connection() as conn:
        if await phrase_jobs.current(conn, image_id, want, settings):
            return None
    return await phrase_jobs.embed(embedder, want, settings)


@dataclass
class _Write:
    recipe: str
    bodies: dict[str, str]
    model: str | None
    vectors: dict[str, np.ndarray]


async def _write(
    pool: Pool,
    image_id: int,
    writes: list[_Write],
    *,
    settings: Settings,
    analysis: str | None = None,
    pub_sha: str | None = None,
    extracted: Extracted | None = None,
    phrases: Mapping[phrase_jobs.Pair, np.ndarray] | None = None,
) -> bool:
    """Store facets and vectors, the raw extract and the phrases when given; False if the
    picture is gone. SoftError when the analysis (split) or the pixels (extract) changed
    since they were read: the invalidation that came with the change may have collapsed
    into this very job, so nothing else would replace what it is about to write.

    The state row is taken first, as every other embedding write takes it: a genesis
    holds it for its whole replay and checks the replay against a fresh read of these
    vectors at the end, so a facet commit landing mid-replay would fail the run. After
    it, images, then the analysis row, the order every writer of those rows takes them
    in. Holding the mark to commit also keeps a tick from clearing it before it can see
    the new vectors.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await mycelium.mark_dirty(conn)
            # FOR SHARE, not KEY SHARE: a crop's update of pub_sha must wait for this
            # commit (and then invalidate it) or be seen by it.
            cur = await conn.execute(
                "select pub_sha from images where id = %s for share", (image_id,)
            )
            img = await cur.fetchone()
            if img is None:
                return False
            if pub_sha is not None and img["pub_sha"] != pub_sha:
                raise SoftError("picture changed while extracting")
            if analysis is not None:
                cur = await conn.execute(
                    "select body from image_texts where image_id = %s and field = 'analysis'"
                    " for share",
                    (image_id,),
                )
                text = await cur.fetchone()
                if text is None or text["body"] != analysis:
                    raise SoftError("analysis changed while splitting")
            if extracted is not None:
                await conn.execute(
                    """
                    insert into image_extracts (image_id, model, body, created_at)
                    values (%s, %s, %s, now())
                    on conflict (image_id) do update set
                      model = excluded.model, body = excluded.body, created_at = now()
                    """,
                    (image_id, extracted.model, Jsonb(extracted.raw)),
                )
            if phrases is not None:
                # With the extract they were read from, so a crop (which waits on the
                # state row and the image row) either sees both or neither.
                await phrase_jobs.replace(conn, image_id, phrases, settings)
            for w in writes:
                await conn.execute(
                    "delete from image_facets where image_id = %s and recipe = %s",
                    (image_id, w.recipe),
                )
                async with conn.cursor() as cur2:
                    await cur2.executemany(
                        "insert into image_facets (image_id, facet, recipe, body, model)"
                        " values (%s, %s, %s, %s, %s)",
                        [(image_id, f, w.recipe, w.bodies[f], w.model) for f in facets.FACETS],
                    )
                    await cur2.executemany(
                        """
                        insert into embeddings (image_id, kind, vec, model, dim, created_at)
                        values (%s, %s, %s, %s, %s, now())
                        on conflict (image_id, kind) do update set
                          vec = excluded.vec, model = excluded.model, dim = excluded.dim,
                          created_at = now()
                        """,
                        [
                            (
                                image_id,
                                facets.kind(f, w.recipe),
                                w.vectors[f],
                                settings.embed_model,
                                settings.embed_dim,
                            )
                            for f in facets.FACETS
                        ],
                    )
    return True


async def _refresh(pool: Pool, job_type: str, recipes: list[str], settings: Settings) -> None:
    """Refresh the recipe's facet means when this job's sweep has drained or has moved
    far from the stored mean; per picture, a sweep would recompute them thousands of
    times."""
    kind = facets.kind(facets.FACETS[0], recipes[0])
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select exists (select 1 from jobs where type = %(t)s and status = 'queued') as more,
                   (select n from corpus_stats where kind = %(k)s and model = %(m)s) as n,
                   (select count(*) from embeddings e join images i on i.id = e.image_id
                     where e.kind = %(k)s and e.model = %(m)s and i.status = 'live')::int as have
            """,
            {"t": job_type, "k": kind, "m": settings.embed_model},
        )
        row = await cur.fetchone()
    assert row is not None
    n = int(row["n"]) if row["n"] is not None else None
    if row["more"] and n is not None and abs(row["have"] - n) < max(REFRESH_LAG, n // 10):
        return
    for recipe in recipes:
        await refresh_facet_stats(pool, model=settings.embed_model, recipe=recipe)


async def _still_complete(pool: Pool, image_id: int, recipe: str, settings: Settings) -> None:
    """An invalidation that commits after _write but before the job is marked done finds
    this job still running, so its re-enqueue collapses into it and is lost. Looking
    again once the work is done turns that into a retry of this same job. What is left
    is the gap between this read and jobs.complete, which the startup sweep (split) or
    `extract all` (extract) make good."""
    async with pool.connection() as conn:
        if not await _complete(conn, image_id, recipe, settings):
            raise SoftError(f"{recipe} facets invalidated while writing")


def _image_id(job: Job) -> int:
    try:
        return int(job.payload["image_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad {job.type} payload: {e}") from e


# -- mycelium_split ---------------------------------------------------------


async def _extract_waiting(conn: Conn, image_id: int) -> datetime | None:
    cur = await conn.execute(
        "select run_after from jobs where dedupe_key = %s and status in ('queued', 'running')",
        (f"{EXTRACT_JOB}:{image_id}",),
    )
    row = await cur.fetchone()
    return None if row is None else row["run_after"]


async def handle_split(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    extractor: Extractor = extract,
) -> None:
    image_id = _image_id(job)
    async with pool.connection() as conn:
        row = await _image(conn, image_id)
        if row is None or row["status"] != "live":
            # Gone, or not published yet: go-live queues the split again.
            log.info("split: image %s is not live; skipping", image_id)
            return
        if await _complete(conn, image_id, facets.SPLIT, settings):
            return
        analysis = row["analysis"]
        if analysis is None:
            log.info("split: image %s has no analysis; skipping", image_id)
            return
        # Rows left by an earlier run (vectors under an older embed model, or a fallback
        # already paid for) are the current analysis's: an edit would have deleted them.
        stored = await _stored(conn, image_id, facets.SPLIT)
        reuse = await _stored_extract(conn, image_id)
        waiting = await _extract_waiting(conn, image_id)

    extracted: Extracted | None = None
    phrases: dict[phrase_jobs.Pair, np.ndarray] | None = None
    writes: list[_Write] = []
    if stored is not None:
        bodies, model = stored
    elif (parts := facets.split(analysis)) is not None:
        bodies, model = parts, None
    elif reuse is not None:
        bodies, model = reuse.facets, reuse.model
    elif waiting is not None:
        # The same call is already queued for this picture; split takes its answer
        # rather than paying for it twice.
        raise SoftError(
            "waiting for extract", run_after=max(waiting, datetime.now(UTC)) + EXTRACT_WAIT
        )
    else:
        # Owner free text, or labels out of order: one extract call, stored as the
        # extract recipe too, since that is exactly what it is.
        extracted = await _call(pool, job, row, settings, extractor)
        bodies, model = extracted.facets, extracted.model
        writes.append(
            _Write(
                facets.EXTRACT,
                extracted.facets,
                extracted.model,
                await _embed(embedder, extracted.facets, settings),
            )
        )
        phrases = await _phrase_vectors(pool, image_id, extracted, settings, embedder)

    vectors = await _embed(embedder, bodies, settings)
    writes.insert(0, _Write(facets.SPLIT, bodies, model, vectors))
    done = await _write(
        pool,
        image_id,
        writes,
        settings=settings,
        analysis=analysis,
        # Bodies with a model are a reading of the pixels (a fallback call now, or a
        # stored or borrowed one), so a crop since they were read makes them stale.
        pub_sha=row["pub_sha"] if model is not None else None,
        extracted=extracted,
        phrases=phrases,
    )
    if not done:
        return
    await _refresh(pool, SPLIT_JOB, [w.recipe for w in writes], settings)
    await _still_complete(pool, image_id, facets.SPLIT, settings)
    log.info("split: image %s%s", image_id, "" if model is None else f" from {model}")


# -- mycelium_extract -------------------------------------------------------


async def handle_extract(
    job: Job,
    *,
    pool: Pool,
    settings: Settings,
    embedder: DocumentEmbedder,
    extractor: Extractor = extract,
) -> None:
    image_id = _image_id(job)
    async with pool.connection() as conn:
        row = await _image(conn, image_id)
        if row is None or row["status"] != "live":
            log.info("extract: image %s is not live; skipping", image_id)
            return
        complete = await _complete(conn, image_id, facets.EXTRACT, settings)
        stored = None if complete else await _stored_extract(conn, image_id)
    if complete:
        # Facets done. An extract stored before phrases were kept, or phrases under an
        # older embed model, are made good here without a call.
        await phrase_jobs.backfill(pool, image_id, settings=settings, embedder=embedder)
        return

    extracted: Extracted | None = None
    if stored is None:
        extracted = await _call(pool, job, row, settings, extractor)
        got = extracted
    else:
        # Paid for already; only the vectors are missing (a new embed model).
        got = stored
    vectors = await _embed(embedder, got.facets, settings)
    # A re-extraction replaces the picture's phrases; those that came back unchanged keep
    # their rows and so a need's satisfaction.
    phrases = await _phrase_vectors(pool, image_id, got, settings, embedder)
    done = await _write(
        pool,
        image_id,
        [_Write(facets.EXTRACT, got.facets, got.model, vectors)],
        settings=settings,
        pub_sha=row["pub_sha"],
        extracted=extracted,
        phrases=phrases,
    )
    if not done:
        return
    await _refresh(pool, EXTRACT_JOB, [facets.EXTRACT], settings)
    await _still_complete(pool, image_id, facets.EXTRACT, settings)
    log.info("extract: image %s from %s", image_id, got.model)
