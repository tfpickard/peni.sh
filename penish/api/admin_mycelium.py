"""Admin for the organism: start and pause, tunables, regrow, reset, genesis, and what
it has grown.

Every route answers with the whole page state, so the client never has to guess
what an action changed. State changes are POSTs, which `_gate` checks for the
custom header.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from penish.api.admin import _gate, require_admin
from penish.mycelium import genesis, store
from penish.mycelium.tunables import DEFAULTS, SPECS, TunableError, Tunables
from penish.text import no_em_dash
from penish.worker.jobs import enqueue

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


async def _payload(conn: store.Conn) -> dict[str, Any]:
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
        "tunables": _tunables(current),
        "tunables_version": version,
        "history": await store.tunables_history(conn, HISTORY),
        "series": await store.series(conn, "whole", SERIES),
        "colonies": await store.colonies_listing(conn),
        "events": await store.recent_events(conn, EVENTS),
    }


@router.get("")
async def overview(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        return await _payload(conn)


@router.post("/start")
async def start(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.start(conn)
        return await _payload(conn)


@router.post("/pause")
async def pause(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.pause(conn)
        return await _payload(conn)


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
        return await _payload(conn)


@router.post("/regrow")
async def regrow(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.regrow(conn)
        return await _payload(conn)


@router.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        await store.reset(conn)
        return await _payload(conn)


@router.post("/genesis")
async def start_genesis(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    async with pool.connection() as conn:
        st = await store.read_state(conn)
    # The job checks again under the lock; this only spares a doomed job.
    if st.running:
        raise HTTPException(status_code=409, detail="running")
    if st.public:
        raise HTTPException(status_code=409, detail="public")
    # A second press while one waits or runs collapses into it.
    await enqueue(pool, genesis.JOB, {}, dedupe_key=genesis.JOB)
    async with pool.connection() as conn:
        return await _payload(conn)
