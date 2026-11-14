"""Corpus-level stats and the frozen per-image surprise, written by the worker."""

from __future__ import annotations

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from penish.mycelium import facets
from penish.mycelium import store as mycelium

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

# Up to here the exact all-pairs average in SQL is about a million cosines,
# which stays well under a second. Beyond it a fixed-seed sample is close
# enough and keeps the number stable between refreshes of the same corpus.
EXACT_TEMPERATURE_MAX_N = 1500
TEMPERATURE_SAMPLE_PAIRS = 200_000
TEMPERATURE_SEED = 0

_LIVE = """
  from embeddings e
  join images i on i.id = e.image_id
  where e.kind = %(kind)s and e.model = %(model)s and i.status = 'live'
"""


def _sampled_mean_pairwise_distance(vecs: np.ndarray, pairs: int, seed: int) -> float:
    n = len(vecs)
    unit = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    rng = np.random.default_rng(seed)
    a = rng.integers(0, n, size=pairs)
    # Shifting by 1..n-1 mod n gives a uniform partner that is never a itself.
    b = (a + rng.integers(1, n, size=pairs)) % n
    dots = np.einsum("ij,ij->i", unit[a], unit[b])
    return float(np.mean(1.0 - dots))


async def refresh_corpus_stats(pool: Pool, *, kind: str, model: str) -> None:
    """Recompute mean, n and temperature for one (kind, model) space, upsert them and
    append a corpus_stats_history row.

    temperature is the mean pairwise cosine distance between live images: how
    spread out the corpus is. Null below two images.

    A refresh of `description` also refreshes every facet kind that has vectors (or a
    row) under the same model, since whatever changed the live set changed their means
    too, and each facet layer is centered on its own. A facet kind alone refreshes
    just that kind.
    """
    async with pool.connection() as conn:
        if facets.is_kind(kind):
            await _refresh_mean(conn, kind=kind, model=model)
        else:
            await _refresh_full(conn, kind=kind, model=model)
        if kind == mycelium.KIND:
            for k in await _facet_kinds(conn, model):
                await _refresh_mean(conn, kind=k, model=model)
        if kind == mycelium.KIND or facets.is_kind(kind):
            # The candidate graph is centered on these means, and the writer that marked
            # it dirty committed before this refresh, so a tick between the two may
            # have rebuilt on the old mean and cleared the mark. Last, not first: the
            # tick never locks corpus_stats, so this order cannot deadlock with it, and
            # the state row is not held across the pairwise average.
            await mycelium.mark_dirty(conn)


async def refresh_facet_stats(pool: Pool, *, model: str, recipe: str | None = None) -> None:
    """Every facet mean of one recipe (or both) in one transaction, with one dirty mark,
    for a job that has just embedded a batch of facets.

    Kinds go in sorted order, as in refresh_corpus_stats: each upsert row-locks its
    corpus_stats row until commit, and two refreshers walking the same rows in different
    orders deadlock.
    """
    async with pool.connection() as conn:
        for k in sorted(facets.kinds(recipe)):
            await _refresh_mean(conn, kind=k, model=model)
        await mycelium.mark_dirty(conn)


async def _facet_kinds(conn: AsyncConnection[DictRow], model: str) -> list[str]:
    # A kind whose last vector went away still has a row whose mean must follow.
    cur = await conn.execute(
        """
        select kind from embeddings where model = %(m)s and kind like 'facet:%%' group by kind
        union
        select kind from corpus_stats where model = %(m)s and kind like 'facet:%%'
        """,
        {"m": model},
    )
    # Sorted here rather than by the database's collation, so refresh_facet_stats can
    # lock in exactly the same order.
    return sorted(str(r["kind"]) for r in await cur.fetchall() if facets.is_kind(str(r["kind"])))


async def _refresh_mean(conn: AsyncConnection[DictRow], *, kind: str, model: str) -> None:
    """Mean and n only. Nothing reads a facet space's temperature or charts its history,
    and eight pairwise averages per publish would cost seconds for no reader."""
    await conn.execute(
        f"""
        insert into corpus_stats (kind, model, mean, n, temperature, updated_at)
        select %(kind)s, %(model)s, avg(e.vec), count(*), null, now()
        {_LIVE}
        on conflict (kind, model) do update set
          mean = excluded.mean,
          n = excluded.n,
          temperature = excluded.temperature,
          updated_at = excluded.updated_at
        """.encode(),
        {"kind": kind, "model": model},
    )


async def _refresh_full(conn: AsyncConnection[DictRow], *, kind: str, model: str) -> None:
    params = {"kind": kind, "model": model}
    cur = await conn.execute(f"select count(*) as n {_LIVE}".encode(), params)
    row = await cur.fetchone()
    n = int(row["n"]) if row else 0

    temperature: float | None = None
    if 2 <= n <= EXACT_TEMPERATURE_MAX_N:
        cur = await conn.execute(
            f"""
            with live as (select e.image_id, e.vec {_LIVE})
            select avg(a.vec <=> b.vec) as t
            from live a join live b on a.image_id < b.image_id
            """.encode(),
            params,
        )
        row = await cur.fetchone()
        temperature = float(row["t"]) if row and row["t"] is not None else None
    elif n > EXACT_TEMPERATURE_MAX_N:
        cur = await conn.execute(f"select e.vec {_LIVE} order by e.image_id".encode(), params)
        vecs = np.stack([r["vec"].to_numpy() for r in await cur.fetchall()]).astype(np.float64)
        temperature = _sampled_mean_pairwise_distance(
            vecs, TEMPERATURE_SAMPLE_PAIRS, TEMPERATURE_SEED
        )

    # avg(vector) accumulates in double precision, so the mean is exact up to
    # the final float4 store, not drifted by incremental updates.
    await conn.execute(
        f"""
        insert into corpus_stats (kind, model, mean, n, temperature, updated_at)
        select %(kind)s, %(model)s, avg(e.vec), count(*), %(t)s, now()
        {_LIVE}
        on conflict (kind, model) do update set
          mean = excluded.mean,
          n = excluded.n,
          temperature = excluded.temperature,
          updated_at = excluded.updated_at
        """.encode(),
        {**params, "t": temperature},
    )
    # Same transaction as the upsert, so the history never disagrees with the current row.
    await conn.execute(
        "insert into corpus_stats_history (kind, model, n, temperature)"
        " values (%(kind)s, %(model)s, %(n)s, %(t)s)",
        {**params, "n": n, "t": temperature},
    )


async def surprise_at_arrival(pool: Pool, image_id: int, *, kind: str, model: str) -> float | None:
    """Cosine distance to the nearest other live image right now.

    Called before the image itself goes live, so it measures how unexpected the
    image was against the corpus as it stood on arrival. None for the first one.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            with src as (
              select vec, dim from embeddings
              where image_id = %(id)s and kind = %(kind)s and model = %(model)s
            )
            select
              exists (select 1 from src) as has_src,
              (select min(e.vec <=> s.vec)
                 from embeddings e
                 join images i on i.id = e.image_id
                 cross join src s
                where e.kind = %(kind)s and e.model = %(model)s and e.dim = s.dim
                  and i.status = 'live' and e.image_id <> %(id)s
               ) as d
            """,
            {"id": image_id, "kind": kind, "model": model},
        )
        row = await cur.fetchone()
    assert row is not None
    if not row["has_src"]:
        raise LookupError(f"no {kind} embedding for image {image_id} under model {model}")
    return None if row["d"] is None else float(row["d"])
