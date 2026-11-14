"""Facet jobs (split and extract), the daily cap, the sweeps, invalidation, and the admin
facet routes. The splitter itself is in test_facet_split.py; the graph in
test_mycelium_graph_facets.py and test_mycelium_facet_layers.py."""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from psycopg.types.json import Jsonb

from penish import runtime
from penish.config import Settings
from penish.mycelium import facets, genesis, store
from penish.worker import facet_jobs, jobs, providers
from penish.worker.describe import DescribeError
from penish.worker.facet_jobs import (
    EXTRACT_JOB,
    EXTRACT_STEP,
    EXTRACT_WAIT,
    SPLIT_JOB,
    SPLIT_STEP,
    Extracted,
    clean,
    enqueue_unextracted,
    enqueue_unsplit,
    extract,
    handle_extract,
    handle_split,
    invalidate_extract,
    invalidate_split,
)
from tests.test_admin_jobs import ANALYSIS2, Redescriber, crop, live, owner, regen
from tests.test_admin_mycelium import anon, app, client, post  # noqa: F401
from tests.test_describe import FakeClient
from tests.test_ingest import ANALYSIS, DIM, FakeEmbedder, q
from tests.test_mycelium_facet_layers import give
from tests.test_mycelium_tick import T0, sql
from tests.test_mycelium_tick import add as add_live

URL = "/api/admin/mycelium"
REPLY = {
    "subject": "a brass lamp stands in moss \u2014 switched on",
    "setting": "a forest floor at dusk",
    "style": "a photograph, green and brass",
    "mood": "quiet",
    "offers": [
        "A Working Lamp.",
        "a working lamp",
        "light",
        "dry moss",
        "a floor",
        "shade",
        "a sixth thing",
    ],
    "needs": ["  an   outlet "],
}
CLEAN = {
    "subject": "a brass lamp stands in moss -- switched on",
    "setting": "a forest floor at dusk",
    "style": "a photograph, green and brass",
    "mood": "quiet",
}
UNLABELLED = "A brass lamp stands in moss on a forest floor, photographed at dusk."


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The cap's clock, pinned for the test: a run that crossed UTC midnight would see
    yesterday's calls drop out and the reset move a day."""
    at = datetime.now(UTC)
    real = providers.check_daily_cap
    monkeypatch.setattr(providers, "check_daily_cap", functools.partial(real, now=at))
    return at


def job(type: str, image_id: int, id: int = 0) -> jobs.Job:
    return jobs.Job(id=id, type=type, payload={"image_id": image_id}, attempts=1, last_error=None)


def fake(*replies: dict[str, Any] | str) -> tuple[FakeClient, Any]:
    c = FakeClient(*(r if isinstance(r, str) else json.dumps(r) for r in replies))
    return c, functools.partial(extract, client=c)


async def no_call(*_: Any, **__: Any) -> Extracted:
    raise AssertionError("no model call expected")


async def split(p: Any, settings: Settings, image_id: int, **kw: Any) -> FakeEmbedder:
    emb = kw.pop("embedder", None) or FakeEmbedder()
    await handle_split(
        job(SPLIT_JOB, image_id),
        pool=p,
        settings=settings,
        embedder=emb,
        extractor=kw.pop("extractor", no_call),
    )
    return emb


async def extract_job(p: Any, settings: Settings, image_id: int, extractor: Any) -> FakeEmbedder:
    emb = FakeEmbedder()
    await handle_extract(
        job(EXTRACT_JOB, image_id), pool=p, settings=settings, embedder=emb, extractor=extractor
    )
    return emb


async def facet_rows(p: Any, image_id: int, recipe: str) -> dict[str, tuple[str, str | None]]:
    rows = await q(
        p,
        "select facet, body, model from image_facets where image_id = %s and recipe = %s",
        image_id,
        recipe,
    )
    return {r["facet"]: (r["body"], r["model"]) for r in rows}


async def kinds(p: Any, image_id: int) -> set[str]:
    rows = await q(p, "select kind from embeddings where image_id = %s", image_id)
    return {r["kind"] for r in rows}


async def dirty(p: Any) -> bool:
    rows = await q(p, "select graph_dirty from mycelium_state")
    return bool(rows[0]["graph_dirty"])


async def clean_mark(p: Any) -> None:
    async with p.connection() as conn:
        await conn.execute("update mycelium_state set graph_dirty = false")


async def usage(p: Any) -> list[str]:
    return [r["purpose"] for r in await q(p, "select purpose from api_usage order by id")]


# -- the extract response ----------------------------------------------------


def test_clean_trims_and_flattens() -> None:
    parts, offers, needs = clean(REPLY)
    assert parts == CLEAN
    # Deduped after lowering, trailing periods gone, five at most.
    assert offers == ["a working lamp", "light", "dry moss", "a floor", "shade"]
    assert needs == ["an outlet"]


@pytest.mark.parametrize(
    "bad",
    [
        {**REPLY, "mood": "  "},
        {**REPLY, "subject": "Subject: a lamp"},
        {k: v for k, v in REPLY.items() if k != "needs"},
        {**REPLY, "extra": "x"},
        {**REPLY, "offers": "a lamp"},
    ],
)
def test_clean_refuses(bad: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017 -- ValueError or a schema error, both retried
        clean(bad)


# -- mycelium_split ----------------------------------------------------------


async def test_split_happy_path(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await clean_mark(p)
    emb = await split(p, settings, a)
    want = facets.split(ANALYSIS)
    assert want is not None
    assert emb.calls == [[want[f] for f in facets.FACETS]]
    assert await facet_rows(p, a, facets.SPLIT) == {f: (want[f], None) for f in facets.FACETS}
    assert await kinds(p, a) == {"description", *facets.kinds(facets.SPLIT)}
    rows = await q(p, "select model, dim from embeddings where kind like 'facet:%%'")
    assert {(r["model"], r["dim"]) for r in rows} == {("test/embed", DIM)}
    # Means for the split kinds, under the embed model, one picture each.
    stats = await q(p, "select kind, n from corpus_stats where kind like 'facet:%%' order by kind")
    assert stats == [{"kind": k, "n": 1} for k in sorted(facets.kinds(facets.SPLIT))]
    assert await dirty(p)
    assert await usage(p) == []

    # Done is done: no second embed.
    again = await split(p, settings, a)
    assert again.calls == []


async def test_split_skips_what_is_not_live(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    async with p.connection() as conn:
        await conn.execute("update images set status = 'processing' where id = %s", (a,))
    emb = await split(p, settings, a)
    assert emb.calls == [] and await facet_rows(p, a, facets.SPLIT) == {}
    await split(p, settings, 999)


async def test_split_reembeds_stored_bodies_for_a_new_model(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await split(p, settings, a)
    other = settings.model_copy(update={"embed_model": "test/other"})
    emb = await split(p, other, a)
    assert len(emb.calls) == 1
    rows = await q(p, "select distinct model from embeddings where kind like 'facet:%%'")
    assert rows == [{"model": "test/other"}]


async def test_split_falls_back_to_one_extract_call(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    async with p.connection() as conn:
        await conn.execute("delete from jobs where type = %s", (EXTRACT_JOB,))
    c, ex = fake(REPLY)
    await split(p, settings, a, extractor=ex)
    assert len(c.messages.calls) == 1
    model = c.messages.calls[0]["model"]
    assert await facet_rows(p, a, facets.SPLIT) == {f: (CLEAN[f], model) for f in facets.FACETS}
    # It was the extract call, so it is stored as that recipe too and not paid for twice.
    assert await facet_rows(p, a, facets.EXTRACT) == {f: (CLEAN[f], model) for f in facets.FACETS}
    raw = await q(p, "select model, body from image_extracts where image_id = %s", a)
    assert raw == [{"model": model, "body": REPLY}]
    assert await kinds(p, a) == {"description", *facets.kinds()}
    assert await usage(p) == ["extract"]

    # The extract job then has nothing to do.
    await extract_job(p, settings, a, no_call)


async def test_split_waits_for_a_queued_extract(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    before = datetime.now(UTC)
    with pytest.raises(jobs.SoftError) as e:
        await split(p, settings, a)
    assert e.value.run_after is not None
    assert before + EXTRACT_WAIT <= e.value.run_after <= datetime.now(UTC) + EXTRACT_WAIT
    assert await facet_rows(p, a, facets.SPLIT) == {}


async def test_split_reuses_a_stored_extract(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    c, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await split(p, settings, a)
    model = c.messages.calls[0]["model"]
    assert await facet_rows(p, a, facets.SPLIT) == {f: (CLEAN[f], model) for f in facets.FACETS}
    assert await usage(p) == ["extract"]


async def test_split_refuses_a_stale_analysis(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)

    class Editing(FakeEmbedder):
        async def embed_documents(self, texts: list[str]) -> Any:
            async with p.connection() as conn:
                await conn.execute(
                    "update image_texts set body = %s where image_id = %s and field = 'analysis'",
                    (ANALYSIS2, a),
                )
            return await super().embed_documents(texts)

    with pytest.raises(jobs.SoftError, match="analysis changed"):
        await split(p, settings, a, embedder=Editing())
    assert await facet_rows(p, a, facets.SPLIT) == {}


async def test_split_refuses_a_borrowed_extract_of_old_pixels(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    _, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)

    class Cropping(FakeEmbedder):
        async def embed_documents(self, texts: list[str]) -> Any:
            # A crop commits between reading the stored extract and writing its copy.
            async with p.connection() as conn:
                await conn.execute("update images set pub_sha = 'elsewhere' where id = %s", (a,))
            return await super().embed_documents(texts)

    with pytest.raises(jobs.SoftError, match="picture changed"):
        await split(p, settings, a, embedder=Cropping())
    assert await facet_rows(p, a, facets.SPLIT) == {}


async def test_split_write_waits_for_the_state_row(p: Any, settings: Settings) -> None:
    # A genesis holds the state row for its whole replay and compares against a fresh
    # read of the facet vectors at the end, so a write must not land in between.
    a = await live(p, settings, 1)
    async with p.connection() as held, held.transaction():
        await held.execute("select 1 from mycelium_state where id = 1 for update")
        task = asyncio.create_task(split(p, settings, a))
        await asyncio.sleep(0.5)
        assert not task.done()
        assert await facet_rows(p, a, facets.SPLIT) == {}
    await task
    assert len(await facet_rows(p, a, facets.SPLIT)) == 4


async def test_an_edit_after_the_write_runs_the_split_again(
    p: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await live(p, settings, 1)
    async with p.connection() as conn:
        await conn.execute("delete from jobs where type <> %s", (SPLIT_JOB,))
    claimed = await jobs.claim(p, [SPLIT_JOB])
    assert claimed is not None and claimed.payload == {"image_id": a}
    real = facet_jobs._refresh

    async def edit_then_refresh(*args: Any, **kw: Any) -> None:
        # The owner edits the analysis after the write commits, while the job still
        # runs, so the invalidation's re-enqueue collapses into this job.
        async with p.connection() as conn, conn.transaction():
            await conn.execute(
                "update image_texts set body = %s, source = 'owner'"
                " where image_id = %s and field = 'analysis'",
                (ANALYSIS2, a),
            )
            await invalidate_split(conn, a)
        await real(*args, **kw)

    monkeypatch.setattr(facet_jobs, "_refresh", edit_then_refresh)

    async def handler(j: jobs.Job) -> None:
        await handle_split(j, pool=p, settings=settings, embedder=FakeEmbedder())

    await jobs._run_one(p, claimed, handler)
    rows = await q(p, "select status::text, type from jobs where id = %s", claimed.id)
    assert rows == [{"status": "queued", "type": SPLIT_JOB}]

    monkeypatch.setattr(facet_jobs, "_refresh", real)
    await split(p, settings, a)
    want = facets.split(ANALYSIS2)
    assert want is not None
    assert await facet_rows(p, a, facets.SPLIT) == {f: (want[f], None) for f in facets.FACETS}


# -- mycelium_extract --------------------------------------------------------


async def test_extract_stores_raw_facets_and_vectors(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await clean_mark(p)
    c, ex = fake(REPLY)
    emb = await extract_job(p, settings, a, ex)
    call = c.messages.calls[0]
    assert call["system"] == runtime.default("extract.prompt", settings)
    schema = call["output_config"]["format"]["schema"]
    assert list(schema["properties"]) == [*facets.FACETS, "offers", "needs"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert call["messages"][0]["content"][0]["source"]["media_type"] == "image/webp"
    # The facets, then the offers and needs (test_mycelium_phrases.py).
    assert emb.calls == [[CLEAN[f] for f in facets.FACETS], clean(REPLY)[1] + clean(REPLY)[2]]

    raw = await q(p, "select model, body from image_extracts where image_id = %s", a)
    assert raw == [{"model": call["model"], "body": REPLY}]
    rows = await facet_rows(p, a, facets.EXTRACT)
    assert rows == {f: (CLEAN[f], call["model"]) for f in facets.FACETS}
    assert await kinds(p, a) == {"description", *facets.kinds(facets.EXTRACT)}
    assert await usage(p) == ["extract"]
    assert await dirty(p)
    # The graph keeps reading split until the owner switches.
    assert (await q(p, "select facet_recipe from mycelium_state"))[0]["facet_recipe"] == "split"

    await extract_job(p, settings, a, no_call)


async def test_extract_retries_bad_output_once(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    c, ex = fake({**REPLY, "mood": ""}, REPLY)
    await extract_job(p, settings, a, ex)
    assert len(c.messages.calls) == 2
    assert await usage(p) == ["extract", "extract"]

    b = await live(p, settings, 2)
    c, ex = fake("not json", "{}")
    with pytest.raises(jobs.SoftError, match="invalid extract output"):
        await extract_job(p, settings, b, ex)
    assert await facet_rows(p, b, facets.EXTRACT) == {}


async def test_extract_refuses_pixels_that_changed(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)

    async def cropped_meanwhile(*_: Any, **__: Any) -> Extracted:
        async with p.connection() as conn:
            await conn.execute("update images set pub_sha = 'elsewhere' where id = %s", (a,))
        return Extracted(facets=dict(CLEAN), offers=[], needs=[], model="m", raw=REPLY)

    with pytest.raises(jobs.SoftError, match="picture changed"):
        await extract_job(p, settings, a, cropped_meanwhile)
    assert await q(p, "select 1 from image_extracts") == []


async def test_a_crop_after_the_write_runs_the_extract_again(
    p: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await live(p, settings, 1)
    real = facet_jobs._refresh

    async def crop_then_refresh(*args: Any, **kw: Any) -> None:
        async with p.connection() as conn, conn.transaction():
            await conn.execute("update images set pub_sha = 'elsewhere' where id = %s", (a,))
            await invalidate_extract(conn, a)
        await real(*args, **kw)

    monkeypatch.setattr(facet_jobs, "_refresh", crop_then_refresh)
    _, ex = fake(REPLY)
    with pytest.raises(jobs.SoftError, match="extract facets invalidated"):
        await extract_job(p, settings, a, ex)
    assert await facet_rows(p, a, facets.EXTRACT) == {}


async def test_extract_reembeds_without_a_call(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    _, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)
    async with p.connection() as conn:
        await conn.execute("delete from embeddings where kind like 'facet:%%'")
    emb = await extract_job(p, settings, a, no_call)
    assert emb.calls == [[CLEAN[f] for f in facets.FACETS]]
    assert await kinds(p, a) == {"description", *facets.kinds(facets.EXTRACT)}


# -- the daily cap -----------------------------------------------------------


async def test_cap_defers_to_midnight_without_spending_an_attempt(
    p: Any, settings: Settings, now: datetime
) -> None:
    a = await live(p, settings, 1)
    b = await live(p, settings, 2)
    await runtime.put(p, "mycelium.daily_calls", 1)
    c, ex = fake(REPLY)

    async def handler(j: jobs.Job) -> None:
        await handle_extract(j, pool=p, settings=settings, embedder=FakeEmbedder(), extractor=ex)

    first = await jobs.claim(p, [EXTRACT_JOB])
    assert first is not None and first.payload == {"image_id": a}
    await jobs._run_one(p, first, handler)
    assert len(c.messages.calls) == 1

    midnight = runtime.next_midnight(now)
    second = await jobs.claim(p, [EXTRACT_JOB])
    assert second is not None and second.payload == {"image_id": b}
    await jobs._run_one(p, second, handler)
    assert len(c.messages.calls) == 1
    rows = await q(
        p, "select status::text, attempts, run_after, last_error from jobs where id = %s", second.id
    )
    assert rows[0]["status"] == "queued" and rows[0]["attempts"] == 0
    assert rows[0]["run_after"] == midnight
    assert "daily cap 1/1" in rows[0]["last_error"]

    # The next one over the cap lines up behind it rather than on top of it.
    cc = await live(p, settings, 3)
    third = await jobs.claim(p, [EXTRACT_JOB])
    assert third is not None and third.payload == {"image_id": cc}
    await jobs._run_one(p, third, handler)
    rows = await q(p, "select run_after from jobs where id = %s", third.id)
    assert rows[0]["run_after"] == midnight + EXTRACT_STEP


async def test_cap_zero_blocks_the_split_fallback(
    p: Any, settings: Settings, now: datetime
) -> None:
    a = await live(p, settings, 1)
    b = await live(p, settings, 2)
    for image_id in (a, b):
        await owner(p, image_id, "analysis", UNLABELLED)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await runtime.put(p, "mycelium.daily_calls", 0)
    c, ex = fake(REPLY)
    with pytest.raises(jobs.SoftError) as e:
        await split(p, settings, a, extractor=ex)
    midnight = runtime.next_midnight(now)
    assert e.value.run_after == midnight
    assert c.messages.calls == [] and await usage(p) == []

    # A split over the cap is one that needs a vision call, so the line behind it is
    # spaced for one, shared with extract jobs waiting for the same reset.
    async with p.connection() as conn:
        await conn.execute(
            "insert into jobs (type, payload, run_after) values (%s, %s, %s)",
            (EXTRACT_JOB, Jsonb({"image_id": a}), midnight),
        )
    with pytest.raises(jobs.SoftError) as e:
        await split(p, settings, b, extractor=ex)
    assert e.value.run_after == midnight + EXTRACT_STEP


# -- sweeps ------------------------------------------------------------------


async def test_split_sweep_is_staggered_and_idempotent(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    b = await live(p, settings, 2)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
        await conn.execute("insert into images (sha256, status) values (%s, 'failed')", ("e" * 64,))
    before = datetime.now(UTC)
    assert await enqueue_unsplit(p, settings) == 2
    rows = await q(
        p, "select payload, dedupe_key, run_after from jobs where type = %s order by id", SPLIT_JOB
    )
    assert [r["payload"] for r in rows] == [{"image_id": a}, {"image_id": b}]
    assert [r["dedupe_key"] for r in rows] == [f"{SPLIT_JOB}:{a}", f"{SPLIT_JOB}:{b}"]
    # Never due now, so an upload dropped mid-sweep goes first.
    assert rows[0]["run_after"] > before
    assert rows[1]["run_after"] - rows[0]["run_after"] == SPLIT_STEP
    assert await enqueue_unsplit(p, settings) == 0

    await split(p, settings, a)
    await split(p, settings, b)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    assert await enqueue_unsplit(p, settings) == 0


async def test_split_sweep_spaces_paid_fallbacks_like_extracts(p: Any, settings: Settings) -> None:
    labelled = await live(p, settings, 1)
    paid = await live(p, settings, 2)
    borrowing = await live(p, settings, 3)
    for image_id in (paid, borrowing):
        await owner(p, image_id, "analysis", UNLABELLED)
    async with p.connection() as conn:
        await conn.execute("delete from jobs where type <> %s", (EXTRACT_JOB,))
        await conn.execute(
            "delete from jobs where type = %s and payload->>'image_id' <> %s",
            (EXTRACT_JOB, str(borrowing)),
        )
    assert await enqueue_unsplit(p, settings) == 3
    rows = await q(p, "select payload, run_after from jobs where type = %s", SPLIT_JOB)
    at = {r["payload"]["image_id"]: r["run_after"] for r in rows}
    # The free ones (a labelled analysis, or an extract to borrow) on the 3 s grid; the
    # one that must buy its facets on the 30 s one.
    assert at[borrowing] - at[labelled] == SPLIT_STEP
    assert at[paid] - at[labelled] == EXTRACT_STEP - SPLIT_STEP


async def test_extract_sweep(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    b = await live(p, settings, 2)
    _, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    assert await enqueue_unextracted(p, settings) == 1
    rows = await q(p, "select type, payload from jobs")
    assert rows == [{"type": EXTRACT_JOB, "payload": {"image_id": b}}]
    assert await enqueue_unextracted(p, settings) == 0


# -- invalidation ------------------------------------------------------------


async def faceted(p: Any, settings: Settings, seed: int) -> int:
    a = await live(p, settings, seed)
    await split(p, settings, a)
    _, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await clean_mark(p)
    return a


async def test_redescribe_invalidates_split_only(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    await regen(p, settings, a, ["description"])
    assert await facet_rows(p, a, facets.SPLIT) == {}
    assert len(await facet_rows(p, a, facets.EXTRACT)) == 4
    assert await kinds(p, a) == {"description", *facets.kinds(facets.EXTRACT)}
    rows = await q(p, "select type, payload from jobs")
    assert rows == [{"type": SPLIT_JOB, "payload": {"image_id": a}}]
    assert await dirty(p)

    # The new analysis splits as the old one did.
    await split(p, settings, a)
    want = facets.split(ANALYSIS2)
    assert want is not None
    assert await facet_rows(p, a, facets.SPLIT) == {f: (want[f], None) for f in facets.FACETS}


async def test_unchanged_analysis_keeps_the_facets(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    await regen(p, settings, a, ["caption"])
    assert len(await facet_rows(p, a, facets.SPLIT)) == 4
    assert await q(p, "select 1 from jobs") == []


async def test_crop_invalidates_extract(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    await crop(p, settings, a, {"x": 100, "y": 100, "w": 600, "h": 500})
    assert await q(p, "select 1 from image_extracts") == []
    assert await facet_rows(p, a, facets.EXTRACT) == {}
    # The redescribe after the crop wrote a new analysis, so split went too.
    assert await facet_rows(p, a, facets.SPLIT) == {}
    assert await kinds(p, a) == {"description"}
    rows = await q(p, "select type from jobs where type like 'mycelium_%%' order by type")
    assert [r["type"] for r in rows] == [EXTRACT_JOB, SPLIT_JOB]
    assert await dirty(p)


async def test_crop_marks_dirty_itself(p: Any, settings: Settings) -> None:
    # The redescribe that follows a crop would mark too; here it fails, so only the
    # crop's own mark can account for the dirty graph.
    a = await faceted(p, settings, 1)
    failing = Redescriber(DescribeError("x", retryable=True))
    with pytest.raises(jobs.SoftError):
        await crop(p, settings, a, {"x": 100, "y": 100, "w": 600, "h": 500}, describer=failing)
    assert await facet_rows(p, a, facets.EXTRACT) == {}
    assert len(await facet_rows(p, a, facets.SPLIT)) == 4
    assert await dirty(p)


async def test_the_same_crop_again_keeps_the_extract(p: Any, settings: Settings) -> None:
    a = await faceted(p, settings, 1)
    rect = {"x": 100, "y": 100, "w": 600, "h": 500}
    await crop(p, settings, a, rect)
    _, ex = fake(REPLY)
    await extract_job(p, settings, a, ex)
    await split(p, settings, a)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    # Same rect, same pixels: nothing read from them is stale, and nothing is paid again.
    await crop(p, settings, a, rect)
    assert len(await q(p, "select 1 from image_extracts where image_id = %s", a)) == 1
    assert len(await facet_rows(p, a, facets.EXTRACT)) == 4
    assert await kinds(p, a) == {"description", *facets.kinds()}
    assert await q(p, "select 1 from jobs where type = %s", EXTRACT_JOB) == []


async def test_crop_drops_split_facets_read_from_old_pixels(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", UNLABELLED)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    _, ex = fake(REPLY)
    await split(p, settings, a, extractor=ex)
    assert len(await facet_rows(p, a, facets.SPLIT)) == 4
    # The owner's analysis survives the crop's redescribe, but these split rows were the
    # fallback call's reading of the uncropped pixels.
    await crop(p, settings, a, {"x": 100, "y": 100, "w": 600, "h": 500})
    assert await facet_rows(p, a, facets.SPLIT) == {}
    assert await kinds(p, a) == {"description"}
    rows = await q(p, "select type from jobs where type like 'mycelium_%%' order by type")
    assert [r["type"] for r in rows] == [EXTRACT_JOB, SPLIT_JOB]


async def test_crop_keeps_split_facets_cut_from_the_analysis(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    await owner(p, a, "analysis", ANALYSIS)
    await split(p, settings, a)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await crop(p, settings, a, {"x": 100, "y": 100, "w": 600, "h": 500})
    want = facets.split(ANALYSIS)
    assert want is not None
    assert await facet_rows(p, a, facets.SPLIT) == {f: (want[f], None) for f in facets.FACETS}
    assert await q(p, "select 1 from jobs where type = %s", SPLIT_JOB) == []


async def test_owner_analysis_edit_at_ingest_invalidates(p: Any, settings: Settings) -> None:
    from penish.worker.ingest import _write_text

    a = await faceted(p, settings, 1)
    async with p.connection() as conn:
        await _write_text(conn, a, "analysis", UNLABELLED, "owner", None, ANALYSIS)
    assert await facet_rows(p, a, facets.SPLIT) == {}
    assert [r["type"] for r in await q(p, "select type from jobs")] == [SPLIT_JOB]


# -- admin -------------------------------------------------------------------


async def test_admin_fresh_facets(client: httpx.AsyncClient) -> None:  # noqa: F811
    body = (await client.get(URL)).json()
    assert body["facets"] == {
        "active": "split",
        "counts": {"split": 0, "extract": 0, "live": 0},
        "queued": {"split": 0, "extract": 0},
        "cap": {"used_today": 0, "daily_calls": runtime.DAILY_CALLS_DEFAULT},
    }


async def test_admin_cap_and_counts(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b = add_live(clean_db, 1), add_live(clean_db, 2)
    give(clean_db, a, 1)
    give(clean_db, b, 2)
    give(clean_db, a, 1, facets.EXTRACT)
    sql(
        clean_db,
        "insert into api_usage (purpose, model) values ('extract', 'm'), ('describe', 'm')",
    )
    sql(clean_db, "insert into settings (key, value) values ('mycelium.daily_calls', '7')")
    f = (await client.get(URL)).json()["facets"]
    assert f["counts"] == {"split": 2, "extract": 1, "live": 2}
    assert f["cap"] == {"used_today": 1, "daily_calls": 7}


async def test_admin_facet_source(app: Any, client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b = add_live(clean_db, 1), add_live(clean_db, 2)
    for i, n in ((a, 1), (b, 2)):
        give(clean_db, i, n)
    give(clean_db, a, 1, facets.EXTRACT)
    sql(clean_db, "update mycelium_state set graph_dirty = false")

    r = await post(client, "facet_source", {"recipe": "extract"})
    assert (
        r.status_code == 400 and r.json()["detail"] == "not every live picture has extract facets"
    )
    r = await post(client, "facet_source", {"recipe": "soup"})
    assert r.status_code == 400 and r.json()["detail"] == "unknown recipe soup"
    assert sql(clean_db, "select facet_recipe, graph_dirty from mycelium_state") == [
        ("split", False)
    ]

    give(clean_db, b, 2, facets.EXTRACT)
    r = await post(client, "facet_source", {"recipe": "extract"})
    assert r.status_code == 200
    body = r.json()
    assert body["facets"]["active"] == "extract" and body["graph_dirty"] is True
    r = await post(client, "facet_source", {"recipe": "split"})
    assert r.status_code == 200 and r.json()["facets"]["active"] == "split"


async def test_admin_extract_all(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b, c = add_live(clean_db, 1), add_live(clean_db, 2), add_live(clean_db, 3)
    give(clean_db, a, 1, facets.EXTRACT)
    r = await post(client, "extract_all")
    assert r.status_code == 200 and r.json()["facets"]["queued"]["extract"] == 2
    rows = sql(
        clean_db, "select payload, run_after from jobs where type = %s order by id", EXTRACT_JOB
    )
    assert [x[0] for x in rows] == [{"image_id": b}, {"image_id": c}]
    assert rows[1][1] - rows[0][1] == EXTRACT_STEP
    assert (await post(client, "extract_all")).json()["facets"]["queued"]["extract"] == 2


async def test_admin_genesis_layers(app: Any, client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    for layers, detail in (
        (["facet:soup"], "unknown layer facet:soup"),
        ([], "no layers"),
        (["whole", "facet:mood"], "whole replays with every layer"),
    ):
        r = await post(client, "genesis", {"layers": layers})
        assert r.status_code == 400 and r.json()["detail"] == detail

    async with app.state.pool.connection() as conn:
        await store.save_state(conn, public_since=T0)
    # Once public only a facet-only replay may run.
    assert (await post(client, "genesis", {})).json()["detail"] == "public"
    every = {"layers": ["whole", *facets.LAYERS]}
    assert (await post(client, "genesis", every)).json()["detail"] == "public"
    r = await post(client, "genesis", {"layers": ["facet:mood", "facet:subject"]})
    assert r.status_code == 200 and r.json()["genesis"]["status"] == "queued"
    assert sql(clean_db, "select type, payload from jobs") == [
        (genesis.JOB, {"layers": ["facet:subject", "facet:mood"]})
    ]

    async with app.state.pool.connection() as conn:
        await store.save_state(conn, running=True)
    r = await post(client, "genesis", {"layers": ["facet:mood"]})
    assert r.status_code == 409 and r.json()["detail"] == "running"
