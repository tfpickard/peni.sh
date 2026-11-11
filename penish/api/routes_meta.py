from typing import Any

from fastapi import APIRouter, Request

from penish.config import get_settings

router = APIRouter()


async def random_haiku(request: Request) -> dict[str, Any] | None:
    # random() over a full scan is fine while the deck is a few hundred rows.
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, l1, l2, l3 from haikus where active order by random() limit 1"
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "lines": [row["l1"], row["l2"], row["l3"]]}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, bool]:
    async with request.app.state.pool.connection() as conn:
        await conn.execute("select 1")
    return {"ok": True}


@router.get("/api/haiku")
async def haiku(request: Request) -> dict[str, Any]:
    h = await random_haiku(request)
    if h is None:
        # Only reachable if the seed deck was deactivated; the client falls back to its own copy.
        return {"id": None, "lines": []}
    return h


@router.get("/api/stats")
async def stats(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("select count(*) as n from images where status = 'live'")
        pictures = (await cur.fetchone() or {"n": 0})["n"]
        cur = await conn.execute(
            "select temperature from corpus_stats where kind = 'description' and model = %s",
            (get_settings().embed_model,),
        )
        row = await cur.fetchone()
    return {"pictures": pictures, "temperature": row["temperature"] if row else None}
