"""Offers and needs: the short phrases of a picture's extract, embedded and stored in
`offers_needs` for the `need` layer.

The extract call (facet_jobs) returns them; this module embeds and stores them. Writes
come from three places, all with the local embedder and none with a model call: the
`mycelium_extract` job (and a split's fallback call) in the same transaction as the
extract it read them from, and the `mycelium_phrases` job, which backfills them from an
`image_extracts` row stored before phrases were kept, or re-embeds them for a new embed
model. The startup sweep `enqueue_unphrased` finds those pictures.

Phrases are embedded as documents (no query prompt), as every short text the worker
embeds is: a need is compared with an offer, and both are the same kind of text, so
they share one space and a phrase gets the same vector in either role.

Imports stay light (no facet_jobs at module level): facet_jobs calls the helpers here
inside its own write.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import jsonschema
import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from penish.config import Settings
from penish.mycelium import facets
from penish.mycelium import store as mycelium
from penish.worker.jobs import HardError, Job, Pool

if TYPE_CHECKING:
    from penish.worker.ingest import DocumentEmbedder

log = logging.getLogger(__name__)

Conn = AsyncConnection[DictRow]

JOB = "mycelium_phrases"
OFFER = "offer"
NEED = "need"
# Phrases come only from the extract call, so they carry its recipe.
RECIPE = facets.EXTRACT
# A backfill job embeds ten short phrases at most, well under a second on the CPU, so a
# sweep comes due slower than it runs and never builds a past-due backlog in front of a
# fresh upload.
STEP = timedelta(seconds=1)

# (role, phrase)
Pair = tuple[str, str]


def pairs(offers: Iterable[str], needs: Iterable[str]) -> list[Pair]:
    """Offers then needs, each in the order the extract gave them (already cleaned and
    deduped within its role by facet_jobs.clean)."""
    return [(OFFER, p) for p in offers] + [(NEED, p) for p in needs]


def is_current(rows: Iterable[Mapping[str, Any]], want: Sequence[Pair], settings: Settings) -> bool:
    """Stored rows (role, phrase, model, dim) hold exactly these phrases, each under
    today's embed model."""
    got = list(rows)
    have = {(r["role"], r["phrase"]) for r in got}
    return have == set(want) and all(
        r["model"] == settings.embed_model and r["dim"] == settings.embed_dim for r in got
    )


async def current(conn: Conn, image_id: int, want: Sequence[Pair], settings: Settings) -> bool:
    cur = await conn.execute(
        "select role, phrase, model, dim from offers_needs where image_id = %s and recipe = %s",
        (image_id, RECIPE),
    )
    return is_current(await cur.fetchall(), want, settings)


async def embed(
    embedder: DocumentEmbedder, want: Sequence[Pair], settings: Settings
) -> dict[Pair, np.ndarray]:
    if not want:
        return {}
    out = await embedder.embed_documents([p for _, p in want])
    vecs = np.asarray(out, dtype=np.float32)
    if vecs.shape != (len(want), settings.embed_dim):
        raise HardError(f"phrase embeddings have shape {vecs.shape}")
    return {pair: vecs[i] for i, pair in enumerate(want)}


async def replace(
    conn: Conn, image_id: int, vectors: Mapping[Pair, np.ndarray], settings: Settings
) -> None:
    """The picture's phrases become exactly these, inside the caller's transaction.

    A phrase that comes back keeps its row, so its id (which a need edge carries) and a
    need's satisfaction survive; only its vector and model are refreshed. A phrase that
    is new starts at satisfaction 0 and one that is gone is deleted, taking its edges
    with it. The caller has taken the state row first and marked the graph dirty.
    """
    roles = [r for r, _ in vectors]
    texts = [p for _, p in vectors]
    await conn.execute(
        """
        delete from offers_needs o
         where o.image_id = %s and o.recipe = %s
           and (o.role, o.phrase) not in (select * from unnest(%s::text[], %s::text[]))
        """,
        (image_id, RECIPE, roles, texts),
    )
    if not vectors:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            """
            insert into offers_needs (image_id, role, phrase, vec, model, dim, recipe)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (image_id, role, phrase, recipe) do update set
              vec = excluded.vec, model = excluded.model, dim = excluded.dim
            """,
            [
                (image_id, role, phrase, vec, settings.embed_model, settings.embed_dim, RECIPE)
                for (role, phrase), vec in vectors.items()
            ],
        )


def _parse(body: Any) -> list[Pair] | None:
    # Imported here: facet_jobs imports this module.
    from penish.worker.facet_jobs import clean

    try:
        _, offers, needs = clean(body)
    except (ValueError, jsonschema.ValidationError):
        return None
    return pairs(offers, needs)


async def backfill(
    pool: Pool, image_id: int, *, settings: Settings, embedder: DocumentEmbedder
) -> bool:
    """Phrases from the stored extract, without a call. False when there is nothing to do:
    the picture is gone or not live, has no extract (`extract all` covers that), has one
    that does not parse (the extract job pays for a new one), or its phrases are current.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select i.status::text as status, x.body, x.created_at
              from images i left join image_extracts x on x.image_id = i.id
             where i.id = %s
            """,
            (image_id,),
        )
        row = await cur.fetchone()
        if row is None or row["status"] != "live" or row["body"] is None:
            return False
        want = _parse(row["body"])
        if want is None:
            log.warning("phrases: stored extract of image %s does not parse", image_id)
            return False
        if await current(conn, image_id, want, settings):
            return False
    vectors = await embed(embedder, want, settings)
    async with pool.connection() as conn, conn.transaction():
        # First, as every embedding write takes it: a tick or genesis holding it reads
        # these rows, and the mark makes the next tick see the new phrases.
        await mycelium.mark_dirty(conn)
        cur = await conn.execute(
            "select status::text as status from images where id = %s for key share", (image_id,)
        )
        img = await cur.fetchone()
        if img is None or img["status"] != "live":
            return False
        cur = await conn.execute(
            "select created_at from image_extracts where image_id = %s for share", (image_id,)
        )
        x = await cur.fetchone()
        if x is None or x["created_at"] != row["created_at"]:
            # A crop deleted the extract, or a new one was written with its own phrases.
            # Either way the extract job owns them now.
            return False
        await replace(conn, image_id, vectors, settings)
    return True


async def handle_phrases(
    job: Job, *, pool: Pool, settings: Settings, embedder: DocumentEmbedder
) -> None:
    try:
        image_id = int(job.payload["image_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HardError(f"bad {job.type} payload: {e}") from e
    if await backfill(pool, image_id, settings=settings, embedder=embedder):
        log.info("phrases: image %s", image_id)


async def enqueue_unphrased(pool: Pool, settings: Settings) -> int:
    """Every live picture whose stored extract's phrases are missing or under an older
    embed model gets a job. Run at worker start beside the split sweep, so extracts
    stored before phrases were kept are backfilled once and a lost job is made good.
    Pictures with no extract are the `extract all` button's."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select i.id, x.body,
                   coalesce((select jsonb_agg(jsonb_build_object(
                                 'role', o.role, 'phrase', o.phrase,
                                 'model', o.model, 'dim', o.dim))
                               from offers_needs o
                              where o.image_id = i.id and o.recipe = %s), '[]') as have
              from images i join image_extracts x on x.image_id = i.id
             where i.status = 'live'
             order by i.id
            """,
            (RECIPE,),
        )
        ids = [
            int(r["id"])
            for r in await cur.fetchall()
            if (want := _parse(r["body"])) is not None and not is_current(r["have"], want, settings)
        ]
        if not ids:
            return 0
        cur = await conn.execute(
            """
            insert into jobs (type, payload, dedupe_key, run_after)
            select %(t)s, jsonb_build_object('image_id', x.id), %(t)s || ':' || x.id,
                   now() + %(step)s * x.n
              from unnest(%(ids)s::bigint[]) with ordinality as x(id, n)
             order by x.id
            on conflict do nothing
            """,
            {"t": JOB, "ids": ids, "step": STEP},
        )
        return cur.rowcount
