"""Admin for the organism: start and pause, tunables, regrow, reset, genesis, the facet
source, the phrase backfill, and what it has grown.

Need and offer phrases appear here and nowhere public.

Every route answers with the whole page state, so the client never has to guess
what an action changed. State changes are POSTs, which `_gate` checks for the
custom header.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from penish import runtime
from penish.api.admin import _gate, require_admin
from penish.config import get_settings
from penish.mycelium import genesis, store
from penish.mycelium.tunables import DEFAULTS, SPECS, TunableError, Tunables
from penish.text import no_em_dash
from penish.worker import facet_jobs, phrase_jobs
from penish.worker.jobs import Pool, enqueue

router = APIRouter(
    prefix="/api/admin/mycelium",
    dependencies=[Depends(_gate), Depends(require_admin)],
    include_in_schema=False,
)

NOTE_MAX = 200
HISTORY = 20
SERIES = 1008
EVENTS = 50


def _pool(request: Request) -> Any:
    return request.app.state.pool


def _tunables(current: Tunables) -> list[dict[str, Any]]:
    out = []
    for f in dataclasses.fields(Tunables):
        default = getattr(DEFAULTS, f.name)
        spec = SPECS[f.name]
        is_int = isinstance(default, int)
        num = int if is_int else float
        out.append(
            {
                "name": f.name,
                "value": getattr(current, f.name),
                "default": default,
                "lo": num(spec.lo),
                "hi": num(spec.hi),
                "meaning": spec.meaning,
                "int": is_int,
            }
        )
    return out


async def _genesis(conn: store.Conn) -> dict[str, Any]:
    cur = await conn.execute(
        "select status::text as status, last_error from jobs where type = %s"
        " order by id desc limit 1",
        (genesis.JOB,),
    )
    row = await cur.fetchone()
    if row is None:
        return {"status": "none", "error": None}
    # A queued retry is not a failure yet, so only a failed run shows its error.
    return {
        "status": row["status"],
        "error": row["last_error"] if row["status"] == "failed" else None,
    }


async def _facets(pool: Pool, conn: store.Conn, active: str) -> dict[str, Any]:
    s = get_settings()
    cur = await conn.execute(
        "select type, count(*)::int as n from jobs where type = any(%s)"
        " and status in ('queued', 'running') group by type",
        ([facet_jobs.SPLIT_JOB, facet_jobs.EXTRACT_JOB],),
    )
    queued = {r["type"]: r["n"] for r in await cur.fetchall()}
    return {
        "active": active,
        "counts": await store.facet_counts(conn, model=s.embed_model, dim=s.embed_dim),
        "queued": {
            "split": queued.get(facet_jobs.SPLIT_JOB, 0),
            "extract": queued.get(facet_jobs.EXTRACT_JOB, 0),
        },
        "cap": {
            "used_today": await runtime.calls_today(pool),
            "daily_calls": int(await runtime.get(pool, "mycelium.daily_calls")),
        },
    }


async def _phrases(conn: store.Conn) -> dict[str, Any]:
    """How far the phrases have got: live pictures holding any under today's embed model
    and dim, of those with a stored extract to take them from, and the jobs waiting.

    A crop deletes the extract but keeps the phrases, so `pictures` counts only pictures
    that still have an extract; otherwise the page could read `phrased 10 of 9 extracted`
    until the re-extraction lands. `needs` and `offers` count every live phrase, since
    those are what the walkers see."""
    s = get_settings()
    cur = await conn.execute(
        """
        select count(distinct o.image_id) filter (
                 where exists (select 1 from image_extracts x where x.image_id = o.image_id)
               )::int as pictures,
               count(*) filter (where o.role = 'need')::int as needs,
               count(*) filter (where o.role = 'offer')::int as offers
          from offers_needs o
          join images i on i.id = o.image_id and i.status = 'live'
         where o.model = %(model)s and o.dim = %(dim)s and o.recipe = %(recipe)s
        """,
        {"model": s.embed_model, "dim": s.embed_dim, "recipe": store.PHRASE_RECIPE},
    )
    have = await cur.fetchone()
    cur = await conn.execute(
        "select count(*)::int as n from image_extracts x"
        " join images i on i.id = x.image_id and i.status = 'live'"
    )
    extracted = await cur.fetchone()
    cur = await conn.execute(
        "select count(*)::int as n from jobs where type = %s and status in ('queued', 'running')",
        (phrase_jobs.JOB,),
    )
    queued = await cur.fetchone()
    return {
        "pictures": have["pictures"] if have else 0,
        "extracted": extracted["n"] if extracted else 0,
        "needs": have["needs"] if have else 0,
        "offers": have["offers"] if have else 0,
        "queued": queued["n"] if queued else 0,
    }


async def _payload(pool: Pool, conn: store.Conn) -> dict[str, Any]:
    settings = get_settings()
    st = await store.read_state(conn)
    current, version, _ = await store.current_tunables(conn)
    return {
        "running": st.running,
        "tick": st.last_tick,
        "next_due": st.next_due.isoformat() if st.next_due else None,
        "behind": st.behind(),
        "graph_version": st.graph_version,
        "graph_dirty": st.graph_dirty,
        "public": st.public,
        "simulated_through": st.simulated_through,
        "genesis": await _genesis(conn),
        # Why a full genesis would be refused now, in the words the job refuses with:
        # once any gallery address is public it never runs again.
        "genesis_refused": "public" if st.public else "running" if st.running else None,
        "tunables": _tunables(current),
        "tunables_version": version,
        "history": await store.tunables_history(conn, HISTORY),
        "series": await store.series(conn, "whole", SERIES),
        "colonies": await store.colonies_listing(conn),
        "events": await store.recent_events(conn, EVENTS),
        "facets": await _facets(pool, conn, st.facet_recipe),
        "phrases": await _phrases(conn),
        "needs": await store.needs_listing(
            conn, model=settings.embed_model, dim=settings.embed_dim, tunables=current
        ),
    }


@router.get("")
async def overview(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        return await _payload(_pool(request), conn)


@router.post("/start")
async def start(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.start(conn)
        return await _payload(_pool(request), conn)


@router.post("/pause")
async def pause(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.pause(conn)
        return await _payload(_pool(request), conn)


class TunablesPost(BaseModel):
    # Any, not numbers: tunables.coerce gives the one error message for every bad value.
    values: dict[str, Any]
    note: str | None = None


@router.post("/tunables")
async def tunables(body: TunablesPost, request: Request) -> dict[str, Any]:
    if not body.values:
        raise HTTPException(status_code=400, detail="nothing to change")
    note = no_em_dash(body.note.strip()) if body.note else ""
    if len(note) > NOTE_MAX:
        raise HTTPException(status_code=400, detail="note too long")
    async with _pool(request).connection() as conn:
        try:
            await store.set_tunables(conn, body.values, note or None)
        except TunableError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return await _payload(_pool(request), conn)


@router.post("/regrow")
async def regrow(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.regrow(conn)
        return await _payload(_pool(request), conn)


@router.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.reset(conn)
        return await _payload(_pool(request), conn)


class GenesisPost(BaseModel):
    # Absent, or every active layer, is a full genesis; only facet layers replay just
    # those, which stays allowed after galleries are public.
    layers: list[str] | None = None


@router.post("/genesis")
async def start_genesis(request: Request, body: GenesisPost | None = None) -> dict[str, Any]:
    pool = _pool(request)
    try:
        scoped = genesis.scope(body.layers if body else None)
    except genesis.GenesisRefused as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    async with pool.connection() as conn:
        st = await store.read_state(conn)
    # The job checks again under the lock; this only spares a doomed job.
    if st.running:
        raise HTTPException(status_code=409, detail="running")
    if scoped is None and st.public:
        raise HTTPException(status_code=409, detail="public")
    # A second press while one waits or runs collapses into it, whatever its layers.
    payload = {} if scoped is None else {"layers": list(scoped)}
    await enqueue(pool, genesis.JOB, payload, dedupe_key=genesis.JOB)
    async with pool.connection() as conn:
        return await _payload(pool, conn)


@router.post("/extract_all")
async def extract_all(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    # Staggered and deduped in the sweep itself; jobs over the daily cap wait for midnight.
    await facet_jobs.enqueue_unextracted(pool, get_settings())
    async with pool.connection() as conn:
        return await _payload(pool, conn)


@router.post("/embed_phrases")
async def embed_phrases(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    # No model call: phrases are embedded from extracts already stored. Pictures with no
    # extract are `extract all`'s. Deduped and spaced in the sweep itself.
    await phrase_jobs.enqueue_unphrased(pool, get_settings())
    async with pool.connection() as conn:
        return await _payload(pool, conn)


class FacetSourcePost(BaseModel):
    recipe: str


@router.post("/facet_source")
async def facet_source(body: FacetSourcePost, request: Request) -> dict[str, Any]:
    s = get_settings()
    async with _pool(request).connection() as conn:
        try:
            await store.set_facet_recipe(conn, body.recipe, model=s.embed_model, dim=s.embed_dim)
        except store.FacetSourceRefused as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return await _payload(_pool(request), conn)
