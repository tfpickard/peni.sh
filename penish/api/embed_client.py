"""Query embeddings from the worker, which is the only process with the model loaded."""

from __future__ import annotations

from typing import Any

import httpx

from penish.config import get_settings

# The model is warm in steady state; a cold load is tens of seconds, and a search box
# should fail rather than hang that long.
TIMEOUT = httpx.Timeout(10.0, connect=2.0)


class EmbedUnavailable(Exception):
    """The worker could not be reached or answered badly. Callers degrade, not 500."""


async def embed_query_full(text: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """{vector, model, dim} from the worker. `model` is what to filter embeddings by."""
    url = get_settings().embed_server_url.rstrip("/") + "/embed"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(url, json={"text": text})
        else:
            r = await client.post(url, json={"text": text}, timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise EmbedUnavailable(str(e)) from e
    vec = body.get("vector") if isinstance(body, dict) else None
    if not isinstance(vec, list) or not vec:
        raise EmbedUnavailable("no vector in response")
    return {"vector": [float(x) for x in vec], "model": body.get("model"), "dim": body.get("dim")}


async def embed_query(text: str, *, client: httpx.AsyncClient | None = None) -> list[float]:
    return list((await embed_query_full(text, client=client))["vector"])
