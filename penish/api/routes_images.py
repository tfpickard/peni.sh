import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response

from penish.api.media import variants_for, variants_for_many
from penish.api.social import approved_comments, vote_summary, voter_for
from penish.config import get_settings
from penish.similarity import Mode, image_metrics, neighbors
from penish.sort import EMBEDDED, SEEDED, SORTS, OrderCache, Row, Sort, order

router = APIRouter()

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_K = 8
PUBLIC_TEXTS = ("caption", "description", "prompt")
_LIVE = "status = 'live' and published_at is not null"


def grid_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of images (pub_sha, slug, width, height, blurhash, palette) as grid entries."""
    variants = variants_for_many((r["pub_sha"], r["width"], r["height"]) for r in rows)
    return [
        {
            "slug": r["slug"],
            "width": r["width"],
            "height": r["height"],
            "blurhash": r["blurhash"],
            "palette": r["palette"],
            "variants": variants[r["pub_sha"]],
        }
        for r in rows
    ]


def _not_here() -> HTTPException:
    return HTTPException(status_code=404, detail="not here")


def _encode_cursor(published_at: datetime, image_id: int) -> str:
    # Integer microseconds: an ISO string carries a "+" that a query string turns into a space.
    return f"{(published_at - _EPOCH) // timedelta(microseconds=1)},{image_id}"


def _decode_cursor(cursor: str) -> tuple[int, int]:
    try:
        micros, image_id = cursor.split(",")
        return int(micros), int(image_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad cursor") from None


@router.get("/api/images")
async def list_images(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(48, ge=1, le=100),
    # No length cap: owner sidecars store tags of any length, and every stored tag is linked.
    tag: str | None = None,
    sort: str = "newest",
    seed: str = Query("", max_length=32),
) -> dict[str, Any]:
    if sort not in SORTS:
        raise HTTPException(status_code=400, detail="bad sort")
    if sort != "newest":
        return await _sorted_page(request, sort, seed, tag, cursor, limit)
    where = ""
    params: list[Any] = []
    count: int | None = None
    if tag:
        where = "and exists (select 1 from tags t where t.image_id = images.id and t.tag = %s)"
        params = [tag]
    if cursor:
        micros, cid = _decode_cursor(cursor)
        where += (
            " and (published_at, id) < (timestamptz 'epoch' + %s * interval '1 microsecond', %s)"
        )
        params += [micros, cid]
    async with request.app.state.pool.connection() as conn:
        if tag and not cursor:
            # Only the first page carries the total; later pages would just repeat it.
            cur = await conn.execute(
                f"select count(*) as n from images where {_LIVE} and exists"
                " (select 1 from tags t where t.image_id = images.id and t.tag = %s)".encode(),
                [tag],
            )
            count = (await cur.fetchone() or {"n": 0})["n"]
        cur = await conn.execute(
            f"""
            select id, pub_sha, slug, width, height, blurhash, palette, published_at
            from images
            where status = 'live' and published_at is not null {where}
            order by published_at desc, id desc
            limit %s
            """.encode(),
            [*params, limit + 1],
        )
        rows = await cur.fetchall()
    # One extra row tells us whether another page exists without a count query.
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1]["published_at"], rows[-1]["id"]) if more else None
    out: dict[str, Any] = {"items": grid_items(rows), "next_cursor": next_cursor}
    if count is not None:
        out["count"] = count
    return out


_orders = OrderCache()


async def _sorted_page(
    request: Request, sort: str, seed: str, tag: str | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    """A page of a computed order.

    The cursor names the snapshot it was cut from as well as an offset, so a publish or a
    tag edit between pages cannot shift the rest of a listing under the reader. A snapshot
    that has been evicted is 410; the client starts the order again.
    """
    token: str | None = None
    offset = 0
    if cursor:
        token, _, raw = cursor.partition(".")
        try:
            offset = int(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad cursor") from None
        if not token or offset < 0:
            raise HTTPException(status_code=400, detail="bad cursor")
    settings = get_settings()
    tag_where = (
        " and exists (select 1 from tags t where t.image_id = images.id and t.tag = %(tag)s)"
        if tag
        else ""
    )
    vp = {"tag": tag, "model": settings.embed_model, "dim": settings.embed_dim}
    async with request.app.state.pool.connection() as conn:
        if token is not None:
            ids = _orders.get((token,))
            if ids is None:
                raise HTTPException(status_code=410, detail="gone")
        else:
            token, ids = await _snapshot(conn, sort, seed, tag, tag_where, vp)
        page = ids[offset : offset + limit]
        cur = await conn.execute(
            f"""
            select id, pub_sha, slug, width, height, blurhash, palette from images
            where {_LIVE} {tag_where} and id = any(%(ids)s)
            """.encode(),
            {**vp, "ids": page},
        )
        found = {r["id"]: r for r in await cur.fetchall()}
    # A picture hidden or untagged since the snapshot is skipped, not returned as a hole.
    rows_out = [found[i] for i in page if i in found]
    nxt = offset + limit
    out: dict[str, Any] = {
        "items": grid_items(rows_out),
        "next_cursor": f"{token}.{nxt}" if nxt < len(ids) else None,
    }
    if tag and not cursor:
        out["count"] = len(ids)
    return out


async def _snapshot(
    conn: Any, sort: str, seed: str, tag: str | None, tag_where: str, vp: dict[str, Any]
) -> tuple[str, list[int]]:
    """The current order and its token, computed only when something it depends on moved."""
    # Anything that could move a picture in any order: a publish, a hide, a recrop, a
    # re-embed, and for a tag listing, the tag being added or removed anywhere.
    cur = await conn.execute(
        f"""
        select
          (select row(count(*), max(updated_at), max(published_at))::text
             from images where {_LIVE}) as images,
          (select row(count(*), max(created_at))::text from embeddings
            where kind = 'description' and model = %(model)s) as vecs,
          (select updated_at::text from corpus_stats
            where kind = 'description' and model = %(model)s) as mean,
          (select md5(string_agg(image_id::text, ',' order by image_id))
             from tags where tag = %(tag)s) as members
        """.encode(),
        vp,
    )
    version = tuple((await cur.fetchone() or {}).values())
    key = repr((sort, seed if sort in SEEDED else "", tag, version))
    token = hashlib.sha256(key.encode()).hexdigest()[:16]
    ids = _orders.get((token,))
    if ids is not None:
        return token, ids
    cur = await conn.execute(
        f"""
        select id, published_at, palette, surprise from images
        where {_LIVE} {tag_where}
        order by published_at desc, id desc
        """.encode(),
        vp,
    )
    rows = [
        Row(r["id"], r["published_at"], r["palette"], r["surprise"]) for r in await cur.fetchall()
    ]
    vecs: dict[int, Any] = {}
    mean = None
    # A reshuffle mints a new seed every time; reading every vector for it would be waste.
    if sort in EMBEDDED:
        cur = await conn.execute(
            f"""
            select e.image_id, e.vec::real[] as vec from embeddings e
            join images on images.id = e.image_id
            where e.kind = 'description' and e.model = %(model)s and e.dim = %(dim)s
              and {_LIVE} {tag_where}
            """.encode(),
            vp,
        )
        vecs = {r["image_id"]: np.asarray(r["vec"], dtype=np.float64) for r in await cur.fetchall()}
        cur = await conn.execute(
            """
            select mean::real[] as mean from corpus_stats
            where kind = 'description' and model = %(model)s and vector_dims(mean) = %(dim)s
            """,
            vp,
        )
        m = await cur.fetchone()
        # Same rule as the similar lists: below the threshold the mean is mostly one
        # picture, and subtracting it would erase that picture.
        if m and len(vecs) >= get_settings().centered_min_images:
            mean = np.asarray(m["mean"], dtype=np.float64)
    ids = order(cast(Sort, sort), rows, vecs, seed=seed, centered=mean)
    _orders.put((token,), ids)
    return token, ids


async def _adjacent(conn: Any, img: dict[str, Any]) -> tuple[str | None, str | None]:
    """Prev and next slugs in grid order, wrapping at both ends so a swipe never dead-ends."""
    if img["published_at"] is None:
        return None, None
    key = (img["published_at"], img["id"])
    # Grid order is (published_at, id) desc, so "next" is the largest key below this one.
    # Past either end, each query falls back to the opposite end of the grid.
    cur = await conn.execute(
        f"""
        (select slug from images where {_LIVE} and (published_at, id) > (%(p)s, %(i)s)
         order by published_at, id limit 1)
        union all
        (select slug from images where {_LIVE} order by published_at, id limit 1)
        limit 1
        """.encode(),
        {"p": key[0], "i": key[1]},
    )
    prev = await cur.fetchone()
    cur = await conn.execute(
        f"""
        (select slug from images where {_LIVE} and (published_at, id) < (%(p)s, %(i)s)
         order by published_at desc, id desc limit 1)
        union all
        (select slug from images where {_LIVE} order by published_at desc, id desc limit 1)
        limit 1
        """.encode(),
        {"p": key[0], "i": key[1]},
    )
    nxt = await cur.fetchone()

    def other(row: dict[str, Any] | None) -> str | None:
        # A lone picture wraps onto itself.
        return row["slug"] if row and row["slug"] != img["slug"] else None

    return other(prev), other(nxt)


async def resolve_live(conn: Any, slug: str) -> dict[str, Any] | None:
    """Live image by slug or by an old alias; None when neither is live."""
    cur = await conn.execute(
        """
        select * from images where slug = %(s)s and status = 'live'
        union all
        select i.* from slug_aliases a join images i on i.id = a.image_id
        where a.slug = %(s)s and i.status = 'live'
        limit 1
        """,
        {"s": slug},
    )
    row: dict[str, Any] | None = await cur.fetchone()
    return row


@router.get("/api/images/{slug}")
async def image_detail(
    request: Request,
    response: Response,
    slug: str,
    sim: Literal["raw", "centered"] | None = None,
) -> dict[str, Any]:
    # votes.mine depends on the caller's cookie, so no cache may share this response.
    response.headers["Cache-Control"] = "no-store"
    pool = request.app.state.pool
    async with pool.connection() as conn:
        img = await resolve_live(conn, slug)
        if img is None:
            raise _not_here()
        if img["slug"] != slug:
            return {"redirect": img["slug"]}
        # An allowlist, not a filter: the analysis and any future private field stay out.
        cur = await conn.execute(
            "select field, body, source, model from image_texts where image_id = %s"
            " and field = any(%s)",
            (img["id"], list(PUBLIC_TEXTS)),
        )
        texts = {
            r["field"]: {"body": r["body"], "source": r["source"], "model": r["model"]}
            for r in await cur.fetchall()
        }
        cur = await conn.execute(
            "select tag, source from tags where image_id = %s order by tag", (img["id"],)
        )
        tags = [{"tag": r["tag"], "source": r["source"]} for r in await cur.fetchall()]
        votes = await vote_summary(conn, img["id"], voter_for(request))
        comments = await approved_comments(conn, img["id"])
        prev, nxt = await _adjacent(conn, img)

    mode: Mode = sim or "auto"
    nb = await neighbors(pool, img["id"], k=_K, mode=mode)
    metrics = await image_metrics(pool, img["id"])

    ids = [n.image_id for n in (*nb.similar, *nb.dissimilar)]
    meta: dict[int, dict[str, Any]] = {}
    if ids:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select id, pub_sha, width, height, blurhash from images where id = any(%s)",
                (ids,),
            )
            meta = {r["id"]: r for r in await cur.fetchall()}
    nb_variants = variants_for_many((m["pub_sha"], m["width"], m["height"]) for m in meta.values())

    def strip(neighbor_list: list[Any]) -> list[dict[str, Any]]:
        out = []
        for n in neighbor_list:
            m = meta[n.image_id]
            vs = nb_variants[m["pub_sha"]]
            # 480 is the grid size; a small source may only have its native width.
            pick = next((v for v in vs if v["w"] == 480), vs[0] if vs else None)
            out.append(
                {
                    "slug": n.slug,
                    "score": round(n.score, 3),
                    "blurhash": m["blurhash"],
                    "src": (pick["webp"] or pick["avif"]) if pick else None,
                }
            )
        return out

    return {
        "slug": img["slug"],
        "width": img["width"],
        "height": img["height"],
        "blurhash": img["blurhash"],
        "palette": img["palette"],
        "variants": variants_for(img["pub_sha"], img["width"], img["height"]),
        "caption": texts.get("caption"),
        "description": texts.get("description"),
        "prompt": texts.get("prompt"),
        "tags": tags,
        "similar": strip(nb.similar),
        "dissimilar": strip(nb.dissimilar),
        "mode": nb.mode,
        "metrics": {
            "loneliness": metrics.loneliness,
            "typicality": metrics.typicality,
            "surprise": metrics.surprise,
        },
        "published_at": img["published_at"].isoformat() if img["published_at"] else None,
        "votes": votes,
        "comments": comments,
        "prev": prev,
        "next": nxt,
    }
