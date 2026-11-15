"""Offers and needs as stored phrases: written with the extract, backfilled from a
stored extract without a model call, re-embedded for a new embed model, and replaced on
a re-extraction with satisfaction kept for the phrases that came back. The need layer
itself (edges, damping, feeding) is in test_mycelium_needs.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.worker import admin_jobs, jobs, phrase_jobs
from penish.worker.facet_jobs import EXTRACT_JOB, Extracted
from penish.worker.phrase_jobs import JOB, enqueue_unphrased, handle_phrases
from tests.test_admin_jobs import crop, live, owner
from tests.test_facets import (
    CLEAN,
    REPLY,
    UNLABELLED,
    clean_mark,
    dirty,
    extract_job,
    faceted,
    fake,
    job,
    no_call,
    split,
    usage,
)
from tests.test_ingest import DIM, FakeEmbedder, q

OFFERS = ["a working lamp", "light", "dry moss", "a floor", "shade"]
NEEDS = ["an outlet"]
# The same picture read again after a crop: one offer and the need survive word for
# word, the rest are new.
AGAIN = {
    **REPLY,
    "offers": ["Light.", "a brass stem"],
    "needs": ["an outlet", "a ladder"],
}
RECT = {"x": 100, "y": 100, "w": 600, "h": 500}


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


async def phrases(p: Any, image_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    rows = await q(
        p,
        "select id, role, phrase, vec, model, dim, recipe, satisfaction from offers_needs"
        " where image_id = %s",
        image_id,
    )
    return {(r["role"], r["phrase"]): r for r in rows}


def want(offers: list[str], needs: list[str]) -> set[tuple[str, str]]:
    return {("offer", o) for o in offers} | {("need", n) for n in needs}


async def stored_extract(p: Any, image_id: int, body: Any = REPLY) -> None:
    """An extract as Increment 3 stored it: the raw response and nothing else."""
    async with p.connection() as conn:
        await conn.execute(
            "insert into image_extracts (image_id, model, body) values (%s, 'm', %s)",
            (image_id, Jsonb(body)),
        )


async def backfill(p: Any, settings: Settings, image_id: int) -> FakeEmbedder:
    emb = FakeEmbedder()
    await handle_phrases(job(JOB, image_id), pool=p, settings=settings, embedder=emb)
    return emb


async def satisfy(p: Any, image_id: int, phrase: str, s: float) -> int:
    async with p.connection() as conn:
        cur = await conn.execute(
            "update offers_needs set satisfaction = %s"
            " where image_id = %s and role = 'need' and phrase = %s returning id",
            (s, image_id, phrase),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row["id"])


# -- with the extract ----------------------------------------------------------


async def test_extract_stores_offers_and_needs(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await clean_mark(p)
    _, ex = fake(REPLY)
    emb = await extract_job(p, settings, a, ex)
    # Cleaned as facet_jobs.clean cleans them, embedded as documents in one batch,
    # offers then needs.
    assert emb.calls[1] == OFFERS + NEEDS
    rows = await phrases(p, a)
    assert set(rows) == want(OFFERS, NEEDS)
    ref = await FakeEmbedder().embed_documents(OFFERS + NEEDS)
    for i, key in enumerate([("offer", o) for o in OFFERS] + [("need", n) for n in NEEDS]):
        r = rows[key]
        assert (r["model"], r["dim"], r["recipe"], r["satisfaction"]) == (
            "test/embed",
            DIM,
            "extract",
            0.0,
        )
        assert np.allclose(r["vec"].to_numpy(), ref[i])
    assert await dirty(p)

    # Done is done: the job again neither calls nor embeds.
    again = await extract_job(p, settings, a, no_call)
    assert again.calls == []


async def test_extract_with_no_phrases_stores_none(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    _, ex = fake({**REPLY, "offers": [], "needs": []})
    emb = await extract_job(p, settings, a, ex)
    assert len(emb.calls) == 1
    assert await phrases(p, a) == {}


async def test_split_fallback_stores_phrases_too(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    async with p.connection() as conn:
        await conn.execute("delete from jobs where type = %s", (EXTRACT_JOB,))
    _, ex = fake(REPLY)
    await split(p, settings, a, extractor=ex)
    assert set(await phrases(p, a)) == want(OFFERS, NEEDS)


async def test_extract_refuses_phrases_of_pixels_that_changed(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)

    async def cropped_meanwhile(*_: Any, **__: Any) -> Extracted:
        async with p.connection() as conn:
            await conn.execute("update images set pub_sha = 'elsewhere' where id = %s", (a,))
        return Extracted(facets=dict(CLEAN), offers=["x"], needs=["y"], model="m", raw=REPLY)

    with pytest.raises(jobs.SoftError, match="picture changed"):
        await extract_job(p, settings, a, cropped_meanwhile)
    assert await phrases(p, a) == {}


# -- re-extraction --------------------------------------------------------------


async def test_reextraction_keeps_satisfaction_of_phrases_that_came_back(
    p: Any, settings: Settings
) -> None:
    a = await faceted(p, settings, 1)
    outlet = await satisfy(p, a, "an outlet", 0.6)
    before = await phrases(p, a)

    # The crop drops the extract but leaves the phrases for the re-extraction to judge.
    await crop(p, settings, a, RECT)
    assert await q(p, "select 1 from image_extracts") == []
    assert set(await phrases(p, a)) == want(OFFERS, NEEDS)
    assert await q(p, "select 1 from jobs where type = %s", EXTRACT_JOB) != []
    # Nothing to backfill from while the new extract is pending.
    assert (await backfill(p, settings, a)).calls == []
    assert set(await phrases(p, a)) == want(OFFERS, NEEDS)

    await clean_mark(p)
    _, ex = fake(AGAIN)
    emb = await extract_job(p, settings, a, ex)
    assert emb.calls[1] == ["light", "a brass stem", "an outlet", "a ladder"]
    rows = await phrases(p, a)
    assert set(rows) == want(["light", "a brass stem"], ["an outlet", "a ladder"])
    # Same words, same row: the id a need edge carries and the satisfaction survive.
    assert rows[("need", "an outlet")]["id"] == outlet
    assert rows[("need", "an outlet")]["satisfaction"] == 0.6
    assert rows[("offer", "light")]["id"] == before[("offer", "light")]["id"]
    # A new need starts unsatisfied.
    assert rows[("need", "a ladder")]["satisfaction"] == 0.0
    assert await dirty(p)


async def test_a_need_reworded_starts_over(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    await satisfy(p, a, "an outlet", 0.9)
    await crop(p, settings, a, RECT)
    _, ex = fake({**REPLY, "needs": ["a wall outlet"]})
    await extract_job(p, settings, a, ex)
    rows = await phrases(p, a)
    assert ("need", "an outlet") not in rows
    assert rows[("need", "a wall outlet")]["satisfaction"] == 0.0


async def test_reextraction_with_nothing_clears_the_phrases(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    await crop(p, settings, a, RECT)
    _, ex = fake({**REPLY, "offers": [], "needs": []})
    await extract_job(p, settings, a, ex)
    assert await phrases(p, a) == {}


# -- the backfill job -------------------------------------------------------------


def test_the_job_is_registered() -> None:
    assert JOB == "mycelium_phrases"
    assert JOB in admin_jobs.HANDLERS


async def test_backfill_from_a_stored_extract_without_a_call(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await stored_extract(p, a)
    await clean_mark(p)
    emb = await backfill(p, settings, a)
    assert emb.calls == [OFFERS + NEEDS]
    assert set(await phrases(p, a)) == want(OFFERS, NEEDS)
    assert await usage(p) == []
    assert await dirty(p)

    # Current already: no embed.
    assert (await backfill(p, settings, a)).calls == []


async def test_backfill_reembeds_for_a_new_model_and_keeps_satisfaction(
    p: Any, settings: Settings
) -> None:
    a = await live(p, settings, 1)
    await stored_extract(p, a)
    await backfill(p, settings, a)
    outlet = await satisfy(p, a, "an outlet", 0.4)
    other = settings.model_copy(update={"embed_model": "test/other"})
    emb = await backfill(p, other, a)
    assert emb.calls == [OFFERS + NEEDS]
    rows = await phrases(p, a)
    assert {r["model"] for r in rows.values()} == {"test/other"}
    assert rows[("need", "an outlet")]["id"] == outlet
    assert rows[("need", "an outlet")]["satisfaction"] == 0.4


async def test_backfill_skips_what_it_cannot_use(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    # No extract: that is `extract all`'s, not a backfill's.
    assert (await backfill(p, settings, a)).calls == []
    # An extract that does not parse: the extract job pays for a new one.
    await stored_extract(p, a, {"offers": "x"})
    assert (await backfill(p, settings, a)).calls == []
    b = await live(p, settings, 2)
    await stored_extract(p, b)
    async with p.connection() as conn:
        await conn.execute("update images set status = 'unpublished' where id = %s", (b,))
    assert (await backfill(p, settings, b)).calls == []
    await backfill(p, settings, 999)
    assert await q(p, "select 1 from offers_needs") == []
    with pytest.raises(jobs.HardError, match="bad mycelium_phrases payload"):
        await handle_phrases(
            jobs.Job(id=0, type=JOB, payload={}, attempts=1, last_error=None),
            pool=p,
            settings=settings,
            embedder=FakeEmbedder(),
        )


async def test_backfill_yields_to_an_extract_that_moved(
    p: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await live(p, settings, 1)
    await stored_extract(p, a)
    real = phrase_jobs.embed

    async def cropped_meanwhile(*args: Any, **kw: Any) -> Any:
        out = await real(*args, **kw)
        async with p.connection() as conn:
            await conn.execute("delete from image_extracts where image_id = %s", (a,))
        return out

    monkeypatch.setattr(phrase_jobs, "embed", cropped_meanwhile)
    await backfill(p, settings, a)
    assert await phrases(p, a) == {}


async def test_extract_job_on_complete_facets_backfills_phrases(p: Any, settings: Settings) -> None:
    # A picture extracted before phrases were kept: facets complete, no phrase rows.
    a = await faceted(p, settings, 1)
    async with p.connection() as conn:
        await conn.execute("delete from offers_needs")
    emb = await extract_job(p, settings, a, no_call)
    assert emb.calls == [OFFERS + NEEDS]
    assert set(await phrases(p, a)) == want(OFFERS, NEEDS)


# -- the sweep --------------------------------------------------------------------


async def test_sweep_enqueues_stale_phrases_once(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)  # extract, no phrases
    b = await live(p, settings, 2)  # extract, phrases current
    c = await live(p, settings, 3)  # no extract
    d = await live(p, settings, 4)  # extract, not live
    e = await live(p, settings, 5)  # extract with nothing to offer or need
    for image_id in (a, b, d):
        await stored_extract(p, image_id)
    await stored_extract(p, e, {**REPLY, "offers": [], "needs": []})
    await backfill(p, settings, b)
    async with p.connection() as conn:
        await conn.execute("update images set status = 'unpublished' where id = %s", (d,))
        await conn.execute("delete from jobs")

    assert await enqueue_unphrased(p, settings) == 1
    rows = await q(p, "select type, payload, dedupe_key, run_after > now() as later from jobs")
    assert rows == [
        {"type": JOB, "payload": {"image_id": a}, "dedupe_key": f"{JOB}:{a}", "later": True}
    ]
    # Deduped while it waits.
    assert await enqueue_unphrased(p, settings) == 0

    await backfill(p, settings, a)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    assert await enqueue_unphrased(p, settings) == 0

    # A new embed model makes every stored phrase stale, in id order on a grid.
    other = settings.model_copy(update={"embed_model": "test/other"})
    assert await enqueue_unphrased(p, other) == 2
    rows = await q(p, "select payload, run_after from jobs order by run_after")
    assert [r["payload"]["image_id"] for r in rows] == [a, b]
    assert rows[1]["run_after"] - rows[0]["run_after"] == phrase_jobs.STEP
    assert c not in [r["payload"]["image_id"] for r in rows]
