"""Query embeddings for the api's semantic search, served from the worker's loaded model.

No auth on purpose: compose.yaml publishes no port for the worker, so only containers on
the compose network can reach :8001. Publishing that port would make this public.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Generator
from typing import Any, Protocol

import numpy as np
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

PORT = 8001
# Search boxes, not documents; also bounds the CPU one request can burn.
MAX_TEXT = 2000


class QueryEmbedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed_query(self, text: str) -> np.ndarray: ...


def make_app(embedder: QueryEmbedder) -> Starlette:
    async def embed(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except ValueError:
            return JSONResponse({"detail": "bad json"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return JSONResponse({"detail": "bad text"}, status_code=400)
        vec = await embedder.embed_query(text.strip())
        return JSONResponse(
            {
                "vector": [float(x) for x in np.asarray(vec, dtype=np.float32)],
                "model": embedder.model_name,
                "dim": embedder.dim,
            }
        )

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/embed", embed, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )


class _Server(uvicorn.Server):
    # uvicorn would otherwise swap in its own SIGTERM/SIGINT handlers and re-raise them
    # on exit, fighting the worker's handlers; the worker's stop event drives shutdown.
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def serve(
    embedder: QueryEmbedder, stop: asyncio.Event, *, host: str = "0.0.0.0", port: int = PORT
) -> None:
    config = uvicorn.Config(
        make_app(embedder), host=host, port=port, log_level="warning", access_log=False
    )
    server = _Server(config)

    async def watch() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    except SystemExit:
        # uvicorn exits the process when it cannot bind. Search is a side feature; ingest
        # and the job loop must keep running without it.
        log.error("embed server could not start on %s:%d", host, port)
    finally:
        watcher.cancel()
