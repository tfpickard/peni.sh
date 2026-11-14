"""Views. A view clears a picture's overgrowth. Nothing about the visitor is stored: the
rate limit keys on an ip hash held in memory only."""

from typing import Annotated

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

router = APIRouter()


class ViewIn(BaseModel):
    # Validated now, used from Increment 2, when it becomes traffic on an edge.
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
    return Response(status_code=204)
