from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from penish.api import (
    admin,
    admin_ops,
    admin_pictures,
    attention,
    index,
    media,
    routes_images,
    routes_meta,
    routes_search,
    social,
)
from penish.config import get_settings
from penish.db import make_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = make_pool(get_settings().database_url)
    await pool.open()
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(routes_meta.router)
    app.include_router(routes_images.router)
    app.include_router(routes_search.router)
    app.include_router(media.router)
    app.include_router(admin.router)
    app.include_router(admin_ops.router)
    app.include_router(admin_pictures.router)
    app.include_router(social.router)
    app.include_router(attention.router)
    app.middleware("http")(admin.no_store)
    # Last: its catch-all must not shadow routes registered above it.
    app.include_router(index.router)
    return app
