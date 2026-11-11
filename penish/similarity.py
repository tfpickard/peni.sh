"""Nearest and farthest neighbors plus per-image metrics, read side only.

Imported by the api, so this stays numpy- and torch-free: all the math runs in
Postgres against the untyped `vector` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from penish.config import get_settings

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
Mode = Literal["raw", "centered", "auto"]
UsedMode = Literal["raw", "centered"]


def _candidates(extra_join: str = "") -> str:
    # dim is filtered as well as model so a mislabelled row is skipped instead
    # of making pgvector raise on a width mismatch mid-scan.
    return f"""
  from embeddings e
  join images i on i.id = e.image_id
  cross join src s
  {extra_join}
  where e.kind = %(kind)s
    and e.model = s.model
    and e.dim = s.dim
    and i.status = 'live'
    and e.image_id <> %(id)s
"""


_RAW_SCORE = "1 - (e.vec <=> s.vec)"

# A vector equal to the mean has no direction once centered and <=> returns NaN,
# which Postgres sorts above every number, so it would top the similar list.
# Such rows keep their raw score instead.
_CENTERED_SCORE = """
  case when vector_norm(e.vec - m.mean) = 0
       then 1 - (e.vec <=> s.vec)
       else 1 - ((e.vec - m.mean) <=> (s.vec - m.mean))
  end
"""


@dataclass(frozen=True, slots=True)
class Neighbor:
    image_id: int
    slug: str | None
    score: float


@dataclass(frozen=True, slots=True)
class Neighbors:
    similar: list[Neighbor]
    dissimilar: list[Neighbor]
    mode: UsedMode


@dataclass(frozen=True, slots=True)
class Metrics:
    """Per-image embedding metrics, each rounded to 3 decimals.

    loneliness: mean cosine distance to the k nearest live neighbors (raw).
        Higher means the image sits further from everything else. None when the
        image has no live neighbors.
    typicality: cosine similarity between the image vector and the corpus mean
        vector, `1 - (vec <=> mean)`. Higher means closer to the average image.
        None before the corpus mean exists.
    surprise: cosine distance to the nearest live image at ingest time, frozen
        then so later uploads do not rewrite it. None for the first image.
    """

    loneliness: float | None
    typicality: float | None
    surprise: float | None


_SRC_CTE = """
  with src as (
    select vec, model, dim from embeddings
    where image_id = %(id)s and kind = %(kind)s
  )
"""


async def _choose_mode(
    conn: AsyncConnection[DictRow], image_id: int, kind: str, mode: Mode
) -> UsedMode | None:
    """Return the mode to use, or None when the image has no vector of this kind."""
    cur = await conn.execute(
        """
        select
          (select count(*)
             from embeddings e2 join images i2 on i2.id = e2.image_id
            where e2.kind = e.kind and e2.model = e.model and e2.dim = e.dim
              and i2.status = 'live') as n_live,
          case when vector_dims(m.mean) = e.dim
               then vector_norm(e.vec - m.mean) end as centered_norm
        from embeddings e
        left join corpus_stats m on m.kind = e.kind and m.model = e.model
        where e.image_id = %(id)s and e.kind = %(kind)s
        """,
        {"id": image_id, "kind": kind},
    )
    row = await cur.fetchone()
    if row is None:
        return None
    if mode == "raw":
        return "raw"
    # No mean yet, a mean from a different width, or a source vector that is the
    # mean (a 1-image corpus): centering has no direction to offer.
    norm = row["centered_norm"]
    centered_ok = norm is not None and norm > 0
    if mode == "centered":
        return "centered" if centered_ok else "raw"
    enough = int(row["n_live"]) >= get_settings().centered_min_images
    return "centered" if centered_ok and enough else "raw"


async def neighbors(
    pool: Pool,
    image_id: int,
    *,
    kind: str = "description",
    k: int = 8,
    mode: Mode = "auto",
) -> Neighbors:
    """Top-k most similar and bottom-k least similar live images, self excluded.

    Centered scoring subtracts the corpus mean first, so "dissimilar" means
    pointing away from the average rather than merely sharing less of what
    every embedding has in common.
    """
    async with pool.connection() as conn:
        used = await _choose_mode(conn, image_id, kind, mode)
        if used is None:
            return Neighbors(similar=[], dissimilar=[], mode="raw")

        if used == "centered":
            score = _CENTERED_SCORE
            sql_from = _candidates("join corpus_stats m on m.kind = e.kind and m.model = s.model")
        else:
            score = _RAW_SCORE
            sql_from = _candidates()

        params = {"id": image_id, "kind": kind, "k": k}
        out: dict[str, list[Neighbor]] = {}
        for name, direction in (("similar", "desc"), ("dissimilar", "asc")):
            sql = f"""
              {_SRC_CTE}
              select e.image_id, i.slug, {score} as score
              {sql_from}
              order by score {direction}, e.image_id
              limit %(k)s
            """
            cur = await conn.execute(sql.encode(), params)
            out[name] = [
                Neighbor(image_id=r["image_id"], slug=r["slug"], score=float(r["score"]))
                for r in await cur.fetchall()
            ]
        return Neighbors(similar=out["similar"], dissimilar=out["dissimilar"], mode=used)


def _r3(x: float | None) -> float | None:
    return None if x is None else round(float(x), 3)


async def image_metrics(
    pool: Pool, image_id: int, *, kind: str = "description", k: int = 5
) -> Metrics:
    """Loneliness, typicality and surprise for one image. See `Metrics`."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            {_SRC_CTE}
            select
              (select avg(d) from (
                 select e.vec <=> s.vec as d
                 {_candidates()}
                 order by d
                 limit %(k)s
               ) nn) as loneliness,
              (select 1 - (s.vec <=> m.mean)
                 from src s
                 join corpus_stats m on m.kind = %(kind)s and m.model = s.model
                where vector_dims(m.mean) = s.dim) as typicality,
              (select surprise from images where id = %(id)s) as surprise
            """.encode(),
            {"id": image_id, "kind": kind, "k": k},
        )
        row = await cur.fetchone()
    assert row is not None
    return Metrics(
        loneliness=_r3(row["loneliness"]),
        typicality=_r3(row["typicality"]),
        surprise=_r3(row["surprise"]),
    )
