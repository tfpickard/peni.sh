"""The tag cloud, the categories and public search: tag matches first, then nearest by
embedding."""

import re
import unicodedata
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from penish.api import embed_client
from penish.api.routes_images import grid_items
from penish.api.social import RateLimit, ip_hash
from penish.categories import CATEGORIES
from penish.collection.numerals import parse_number
from penish.config import get_settings
from penish.mycelium.store import overgrowth_by_id
from penish.text import slugify

router = APIRouter()

KIND = "description"
Q_MAX = 200
SEARCH_LIMIT = 30
SEARCH_WINDOW = 60
_CACHE_MAX = 256
_TAG_MATCHES = 12

# Each uncached query costs the worker a CPU forward pass on a box it shares.
search_limit = RateLimit(SEARCH_LIMIT, SEARCH_WINDOW)
# Query vectors only change with the model, and the model name is part of the value.
_vectors: OrderedDict[str, dict[str, Any]] = OrderedDict()

_LIVE = "i.status = 'live' and i.published_at is not null"
_COLS = "i.id, i.pub_sha, i.slug, i.width, i.height, i.blurhash, i.palette"


@router.get("/api/tags")
async def tag_cloud(request: Request) -> dict[str, Any]:
    s = get_settings()
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            f"""
            select t.tag, count(*)::int as n
              from tags t join images i on i.id = t.image_id
             where {_LIVE}
             group by t.tag
            having count(*) >= %s
             order by n desc, t.tag
             limit %s
            """.encode(),
            (s.tag_cloud_min, s.tag_cloud_limit),
        )
        rows = await cur.fetchall()
    return {"tags": [{"tag": r["tag"], "n": r["n"]} for r in rows]}


@router.get("/api/categories")
async def categories(request: Request) -> dict[str, Any]:
    """Every category in gallery order, empty ones too, each with its newest picture."""
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            f"""
            select c.category, count(*) over (partition by c.category)::int as n,
                   i.id, i.pub_sha, i.slug, i.width, i.height, i.blurhash, i.palette
              from image_categories c join images i on i.id = c.image_id
             where {_LIVE}
             order by c.category, i.published_at desc, i.id desc
            """.encode()
        )
        rows = await cur.fetchall()
        first: dict[str, dict[str, Any]] = {}
        for r in rows:
            first.setdefault(r["category"], r)
        og = await overgrowth_by_id(conn, [r["id"] for r in first.values()])
    covers = dict(zip(first, grid_items(list(first.values()), og), strict=True))
    return {
        "categories": [
            {
                "slug": slug,
                "label": label,
                "n": first[slug]["n"] if slug in first else 0,
                "cover": covers.get(slug),
            }
            for slug, label, _ in CATEGORIES
        ]
    }


async def _query_vector(request: Request, q: str) -> dict[str, Any] | None:
    """The embedding for q, or None when the worker cannot be asked right now."""
    key = q.lower()
    hit = _vectors.get(key)
    if hit is not None:
        _vectors.move_to_end(key)
        return hit
    if not search_limit.hit(ip_hash(request)):
        raise HTTPException(status_code=429, detail="wait")
    try:
        emb = await embed_client.embed_query_full(q)
    except embed_client.EmbedUnavailable:
        return None
    _vectors[key] = emb
    if len(_vectors) > _CACHE_MAX:
        _vectors.popitem(last=False)
    return emb


# Separators a reader puts in a leaf reference or an abbreviation: `iii.44`, `ptp iii, 44`,
# `π.τ.π.`. Folded to spaces before anything is compared.
_SEPARATORS = re.compile(r"[.,:;/·]+")
_LEAF_WORDS = {"leaf", "f", "fol", "folio"}


def _fold(text: str) -> str:
    """Lowercase without accents or breathings, so `πανσοφικον` and `Πανσοφικὸν` are one
    word to a reader and to this."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_SEPARATORS.sub(" ", bare).split())


def _names(c: dict[str, Any]) -> set[str]:
    """Every name a reader might give the collection: slug, full title, its first word and
    both abbreviations. Spaced forms are also kept closed up, so `p t p` is `ptp`."""
    title = _fold(c["title"])
    out = {_fold(c["slug"]), title, title.split()[0] if title else "", _fold(c["abbrev"])}
    out.add(_fold(c["abbrev_latin"]))
    return {n for n in out if n} | {n.replace(" ", "") for n in out if n}


def _leaf_ref(words: list[str], named: bool) -> tuple[int, int] | None:
    """(book, position) for `iii 44` or `iii leaf 44`. Without a collection's name in front
    the book must be roman, so `3 44` stays an ordinary search."""
    words = [w for w in words if w not in _LEAF_WORDS] if len(words) > 2 else words
    if len(words) != 2:
        return None
    book_word, position_word = words
    if not named and not book_word.isalpha():
        return None
    if not (position_word.isascii() and position_word.isdigit()):
        return None
    try:
        return parse_number(book_word), parse_number(position_word)
    except ValueError:
        return None


async def _collection_query(
    conn: Any, q: str, limit: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """What the query asks of a published collection: (collection, leaf rows, exact).

    A name alone gives the collection and its live leaves in book and leaf order. A leaf
    reference with the collection's name in front (`ptp iii 44`) gives that one leaf, and
    exact is True: nothing else is looked for. A bare reference (`v 8`) gives the leaf too,
    but exact is False, since `i 1` is as likely to be words as a place: the usual search
    follows it. Neither gives (None, [], False). Unpublished collections are never read.
    """
    folded = _fold(q)
    if not folded:
        return None, [], False
    cur = await conn.execute(
        "select id, slug, title, abbrev, abbrev_latin from collections where published order by id"
    )
    collections = await cur.fetchall()
    if not collections:
        return None, [], False
    words = folded.split()
    for c in collections:
        names = _names(c)
        if folded in names or folded.replace(" ", "") in names:
            cur = await conn.execute(
                f"""
                select {_COLS} from collection_leaves l join images i on i.id = l.image_id
                 where l.collection_id = %s and {_LIVE}
                 order by l.book, l.position limit %s
                """.encode(),
                (c["id"], limit),
            )
            return c, await cur.fetchall(), False
    # A leaf reference, with or without a name in front: `ptp iii 44`, `iii.44`.
    candidates = [c["id"] for c in collections]
    named = False
    for c in collections:
        for name in sorted(_names(c), key=len, reverse=True):
            n = name.split()
            if len(words) > len(n) and words[: len(n)] == n:
                candidates, named, words = [c["id"]], True, words[len(n) :]
                break
        if named:
            break
    ref = _leaf_ref(words, named)
    if ref is None:
        return None, [], False
    cur = await conn.execute(
        f"""
        select {_COLS} from collection_leaves l join images i on i.id = l.image_id
         where l.collection_id = any(%s) and l.book = %s and l.position = %s and {_LIVE}
         order by l.collection_id limit 1
        """.encode(),
        (candidates, *ref),
    )
    row = await cur.fetchone()
    return None, [row] if row else [], row is not None and named


@router.get("/api/search")
async def search(
    request: Request,
    q: str = Query("", max_length=Q_MAX),
    limit: int = Query(24, ge=1, le=100),
) -> dict[str, Any]:
    q = " ".join(q.split())
    if not q:
        return {"q": "", "tags": [], "items": []}
    # Tags are kebab-case, so "black and white" has to meet black-and-white. An owner tag
    # may be free text, hence the literal query as a second spelling.
    as_tag = slugify(q)
    spellings = list({q.lower(), as_tag} - {""})
    pool = request.app.state.pool

    async with pool.connection() as conn:
        collection, held, exact = await _collection_query(conn, q, limit)
        if exact:
            # A leaf asked for by its place, collection named, is the answer; the rest of
            # search would be noise.
            og = await overgrowth_by_id(conn, [r["id"] for r in held])
            return {"q": q, "tags": [], "items": grid_items(held, og)}
        cur = await conn.execute(
            f"""
            select t.tag, count(*)::int as n
              from tags t join images i on i.id = t.image_id
             where {_LIVE}
               and exists (select 1 from unnest(%(exact)s::text[]) s
                            where position(s in lower(t.tag)) > 0)
             group by t.tag
             order by (lower(t.tag) = any(%(exact)s)) desc, n desc, t.tag
             limit %(k)s
            """.encode(),
            {"exact": spellings, "k": _TAG_MATCHES},
        )
        tags = [{"tag": r["tag"], "n": r["n"]} for r in await cur.fetchall()]

    emb = await _query_vector(request, q)
    params: dict[str, Any] = {"exact": spellings, "limit": limit, "kind": KIND}
    tagged = (
        "exists (select 1 from tags t where t.image_id = i.id and lower(t.tag) = any(%(exact)s))"
    )
    cols = _COLS
    if emb is None:
        # Without the worker there is no ranking, only pictures that carry the tag outright.
        sql = f"""
            select {cols} from images i
             where {_LIVE} and {tagged}
             order by i.published_at desc, i.id desc
             limit %(limit)s
        """
    else:
        vec = emb["vector"]
        params |= {
            "qvec": "[" + ",".join(repr(float(x)) for x in vec) + "]",
            "qmodel": emb.get("model") or get_settings().embed_model,
            "qdim": len(vec),
        }
        # Filtering on dim as well keeps a mislabelled row from making pgvector raise.
        sql = f"""
            select {cols}, {tagged} as tagged, e.vec <=> %(qvec)s::vector as dist
              from images i
              left join embeddings e on e.image_id = i.id and e.kind = %(kind)s
                                    and e.model = %(qmodel)s and e.dim = %(qdim)s
             where {_LIVE} and (e.image_id is not null or {tagged})
             order by tagged desc, dist asc nulls last, i.id desc
             limit %(limit)s
        """
    async with pool.connection() as conn:
        cur = await conn.execute(sql.encode(), params)
        rows = await cur.fetchall()
        # The collection asked for by name, or a leaf by its place, comes first: then the
        # usual matches.
        if held:
            first = {r["id"] for r in held}
            rows = (held + [r for r in rows if r["id"] not in first])[:limit]
        og = await overgrowth_by_id(conn, [r["id"] for r in rows])
    if emb is None and not rows and not tags:
        raise HTTPException(status_code=503, detail="search unavailable")
    out: dict[str, Any] = {"q": q, "tags": tags, "items": grid_items(rows, og)}
    if collection is not None:
        out["collection"] = {k: collection[k] for k in ("slug", "title", "abbrev", "abbrev_latin")}
    return out
