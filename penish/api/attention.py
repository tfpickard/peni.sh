"""Views and clicks. A view clears a picture's overgrowth; a view that arrives from a
candidate neighbor is also a click, which feeds that edge on the next tick. Nothing
about the visitor is stored: both limits key on an ip hash held in memory only."""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from penish.api.routes_images import resolve_live
from penish.api.social import RateLimit, _gate, ip_hash
from penish.mycelium import store

VIEW_LIMIT = 30
VIEW_WINDOW = 60
# slugify emits at most 60 characters; the rest is room for a "-N" suffix.
SLUG_MAX = 100

# A person paging through pictures stays far below this; a script does not get far.
view_limit = RateLimit(VIEW_LIMIT, VIEW_WINDOW)

# Clicks move the organism, so one reader gets far fewer of them than views: without
# this, one person paging back and forth could pin an edge above anything walkers reach.
CLICK_LIMIT = 6
CLICK_WINDOW = 600
click_limit = RateLimit(CLICK_LIMIT, CLICK_WINDOW)

router = APIRouter()


class ViewIn(BaseModel):
    # The picture the reader came from; a click when that pair is a candidate edge.
    from_: str | None = Field(
        default=None, alias="from", max_length=SLUG_MAX, pattern=r"^[a-z0-9-]+$"
    )


@router.post("/api/images/{slug}/view", status_code=204, dependencies=[Depends(_gate)])
async def view(
    request: Request, slug: str, body: Annotated[ViewIn | None, Body()] = None
) -> Response:
    # Before the lookup, so a flood never reaches the database.
    if not view_limit.hit(ip_hash(request)):
        raise HTTPException(status_code=429, detail="wait")
    async with request.app.state.pool.connection() as conn:
        img = await resolve_live(conn, slug)
        if img is None:
            raise HTTPException(status_code=404, detail="not here")
        await store.record_view(conn, int(img["id"]))
        # The lookup opened a transaction, so the view's attention row lock would
        # otherwise last through the click check, which reads network tables a tick
        # or genesis may be writing; the view stands on its own either way.
        await conn.commit()
        if body is not None and body.from_ is not None:
            await _click(request, conn, body.from_, int(img["id"]))
    return Response(status_code=204)


async def _click(request: Request, conn: Any, from_slug: str, dst: int) -> None:
    """Queue a click when the organism is running and (from, to) is a candidate edge.

    Anything else is dropped without a word: the view already counted, and the
    reader is told nothing either way. The budget is spent only on clicks that
    would count, so ordinary browsing never uses it up.
    """
    src = await resolve_live(conn, from_slug)
    if src is None or int(src["id"]) == dst:
        return
    if not await store.click_feeds(conn, int(src["id"]), dst):
        return
    if not click_limit.hit(ip_hash(request)):
        return
    await store.record_click(conn, int(src["id"]), dst)
