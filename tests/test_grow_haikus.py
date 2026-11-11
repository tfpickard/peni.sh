import importlib.util
import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import psycopg
import pytest

from penish.config import Settings

_spec = importlib.util.spec_from_file_location(
    "grow_haikus", Path(__file__).resolve().parent.parent / "scripts" / "grow_haikus.py"
)
assert _spec and _spec.loader
gh: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

SETTINGS = Settings(anthropic_api_key="test")


class FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self.messages = FakeMessages(list(replies))


class FakeEmbedder:
    """Maps a haiku's first word to a chosen vector; unknown words get orthogonal axes."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.next_axis = 0

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            key = t.split()[0].lower()
            if key not in self.table:
                v = [0.0] * 8
                v[self.next_axis % 8] = 1.0
                self.next_axis += 1
                self.table[key] = v
            out.append(self.table[key])
        return np.array(out, dtype=np.float32)


def reply(*haikus: list[str]) -> str:
    return json.dumps({"haikus": list(haikus)})


async def _pool(db: str) -> list[tuple[str, str, str]]:
    async with await psycopg.AsyncConnection.connect(db) as conn:
        return await gh.load_pool(conn)  # type: ignore[no-any-return]


async def _grow(pool: Any, client: FakeClient, n: int = 5, embedder: Any = None) -> Any:
    return await gh.grow(
        pool=pool,
        n=n,
        batch=5,
        model="m",
        settings=SETTINGS,
        client=client,
        embedder=embedder,
        rng=random.Random(0),
    )


async def test_seed_duplicates_rejected(clean_db: str) -> None:
    pool = await _pool(clean_db)
    seed = list(pool[0])
    # Case and punctuation differ, so only normalization catches it.
    loud = [seed[0].upper() + "!", seed[1], seed[2]]
    client = FakeClient(reply(seed, loud, ["a new thing", "grows in the drain", "we call it dave"]))
    stats = await _grow(pool, client, n=1)
    assert stats.dup == 2 and stats.accepted == 1


async def test_em_dash_fixed(clean_db: str) -> None:
    pool = await _pool(clean_db)
    client = FakeClient(reply(["a knee \u2014 again", "the second one grows in", "nobody counts"]))
    stats = await _grow(pool, client, n=1)
    assert stats.haikus[0][0] == "a knee -- again"


async def test_invalid_rejected(clean_db: str) -> None:
    pool = await _pool(clean_db)
    good = ["one valid", "line two here", "line three"]
    client = FakeClient(
        reply(["only", "two"], ["a", "", "c"], ["a", "b", "x" * 61], ["a", "b", "c", "d"], good)
    )
    stats = await _grow(pool, client, n=1)
    assert stats.invalid == 4 and stats.accepted == 1


async def test_insert_source_model(clean_db: str) -> None:
    pool = await _pool(clean_db)
    h = ("a gill on the wrist", "the dentist admires it", "twice a year, fine")
    stats = await _grow(pool, FakeClient(reply(list(h))), n=1)
    async with await psycopg.AsyncConnection.connect(clean_db) as conn:
        await gh.insert(conn, stats.haikus)
        await gh.insert(conn, stats.haikus)  # conflict path must not raise
        cur = await conn.execute("select source::text, image_id from haikus where l1 = %s", (h[0],))
        assert await cur.fetchall() == [("model", None)]


async def test_dry_run_inserts_nothing(clean_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    async with await psycopg.AsyncConnection.connect(clean_db) as conn:
        before = len(await gh.load_pool(conn))
    args = SimpleNamespace(n=1, batch=5, dry_run=True, model="m")
    h = ["moss in the mailbox", "the postman sorts around it", "tuesday, as usual"]
    gh.anthropic.AsyncAnthropic = lambda **_: FakeClient(reply(h))
    gh.embeddings_available = lambda: False
    gh.get_settings = lambda: Settings(database_url=clean_db, anthropic_api_key="t")
    assert await gh.run(args) == 0
    async with await psycopg.AsyncConnection.connect(clean_db) as conn:
        assert len(await gh.load_pool(conn)) == before
    out = capsys.readouterr().out
    assert "moss in the mailbox" in out and "accepted 1, rejected 0" in out


async def test_semantic_dedupe() -> None:
    pool = [("alpha", "x", "y")]
    emb = FakeEmbedder(
        {
            "alpha": [1, 0, 0, 0, 0, 0, 0, 0],
            "close": [0.99, 0.1, 0, 0, 0, 0, 0, 0],
            "far": [0, 1, 0, 0, 0, 0, 0, 0],
            "closer": [0, 0.99, 0.1, 0, 0, 0, 0, 0],
        }
    )
    client = FakeClient(
        reply(["close", "to", "the pool"], ["far", "from", "it"], ["closer", "to", "far"]),
        *[reply()] * 12,
    )
    stats = await _grow(pool, client, n=3, embedder=emb)
    # "close" hits the pool; "closer" hits the haiku accepted earlier in this run.
    assert [h[0] for h in stats.haikus] == ["far"]
    assert stats.dup == 2
