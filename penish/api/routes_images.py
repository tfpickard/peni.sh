from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from penish.api.media import variants_for, variants_for_many
from penish.api.social import approved_comments, vote_summary, voter_for
from penish.similarity import Mode, image_metrics, neighbors

router = APIRouter()

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_K = 8
PUBLIC_TEXTS = ("caption", "description", "prompt")
# The admin form's cap on one tag; nothing longer can be stored.
TAG_MAX = 60
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
    tag: str | None = Query(None, max_length=TAG_MAX),
) -> dict[str, Any]:
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
