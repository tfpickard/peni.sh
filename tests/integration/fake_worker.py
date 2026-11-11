"""Runs the real worker main() with the describer, haiku writer and embedder faked.

CI has no network and no model, so those three are the only seams replaced; the
watcher, job loop, ingest handler and database are the real ones.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

import penish.worker.__main__ as worker_main
from penish.config import Settings, get_settings
from penish.worker import ingest
from penish.worker.describe import Described, expand


async def fake_describe(
    image_bytes: bytes, mime: str, *, want: set[str], settings: Settings, pool: Any = None
) -> Described:
    tag = hashlib.sha256(image_bytes).hexdigest()[:8]
    # The test counts lines here to tell whether the model was consulted at all.
    with (settings.data_dir / "describe_calls.log").open("a") as f:
        f.write(f"{tag} {','.join(sorted(want))}\n")
    delay = float(os.environ.get("FAKE_DESCRIBE_SLEEP", "0"))
    if delay:
        await asyncio.sleep(delay)
    want = expand(want)
    return Described(
        caption=f"Fake caption {tag}" if "caption" in want else None,
        description=f"Fake description of image {tag}." if "description" in want else None,
        tags=["alpha", "beta", "gamma", "delta", "epsilon", "zeta"] if "tags" in want else None,
        model="fake-describer",
        analysis=f"Subject: fake analysis of image {tag}." if "analysis" in want else None,
    )


async def fake_haiku(
    description: str, *, settings: Settings, pool: Any = None
) -> tuple[str, str, str]:
    return "one fake line", "another fake line", "a last fake line"


class FakeEmbedder:
    def __init__(self, *_: Any, **__: Any) -> None:
        self.dim = get_settings().embed_dim

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).normal(size=self.dim)
            out.append(v / np.linalg.norm(v))
        return np.asarray(out, dtype=np.float32)


def install() -> None:
    # handle_ingest binds `describer=describe` at definition time, so patching the
    # module attribute alone would not reach it; the keyword default must change too.
    ingest.describe = fake_describe  # type: ignore[assignment]
    ingest.handle_ingest.__kwdefaults__["describer"] = fake_describe  # type: ignore[index]
    ingest.handle_haiku.__kwdefaults__["writer"] = fake_haiku  # type: ignore[index]
    worker_main.Embedder = FakeEmbedder  # type: ignore[misc,assignment]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Run as a script, so the repo root is not on sys.path unless the caller put it there.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    install()
    asyncio.run(worker_main.main())
