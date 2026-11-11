import time

import numpy as np
import pytest

pytest.importorskip("sentence_transformers")

from penish.worker.embed import Embedder  # noqa: E402

CAT_A = (
    "A tabby cat sleeps on a windowsill in warm afternoon light, curled up beside a potted plant."
)
CAT_B = "An orange striped cat naps on a window ledge in the late sun, next to a houseplant."
UNRELATED = (
    "A rusted cargo ship crosses a stormy grey sea under heavy clouds, waves breaking over the bow."
)


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return Embedder()


async def test_dim_and_dtype(embedder: Embedder) -> None:
    vecs = await embedder.embed_documents([CAT_A, UNRELATED])
    assert vecs.shape == (2, 1024)
    assert embedder.dim == 1024
    assert vecs.dtype == np.float32


async def test_unit_norm(embedder: Embedder) -> None:
    vecs = await embedder.embed_documents([CAT_A, CAT_B, UNRELATED])
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)


async def test_paraphrase_beats_unrelated(embedder: Embedder) -> None:
    a, b, c = await embedder.embed_documents([CAT_A, CAT_B, UNRELATED])
    assert float(a @ b) > float(a @ c)


async def test_query_differs_from_document(embedder: Embedder) -> None:
    doc = (await embedder.embed_documents([CAT_A]))[0]
    query = await embedder.embed_query(CAT_A)
    assert query.shape == (1024,)
    assert not np.allclose(doc, query, atol=1e-4)


async def test_timing(embedder: Embedder) -> None:
    texts = [CAT_A * 6] * 4
    start = time.perf_counter()
    await embedder.embed_documents(texts)
    per = (time.perf_counter() - start) / len(texts)
    print(f"embed: {per:.3f}s per description")
