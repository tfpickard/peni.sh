# ruff: noqa: F811  -- shared fixtures are imported, then named as parameters.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import jsonschema
import pytest

from penish import runtime
from penish.config import Settings
from penish.worker import nudity
from penish.worker.admin_jobs import handle_regen
from penish.worker.categorize import enqueue_uncategorized, handle_categorize
from penish.worker.describe import DescribeError
from penish.worker.ingest import handle_haiku
from penish.worker.jobs import HardError, SoftError
from penish.worker.nudity import KIND_NAMES, Nudity, clean, detect, prompt
from tests.test_admin_jobs import (  # noqa: F401
    FakeEmbedder,
    Redescriber,
    crop,
    job,
    live,
    p,
    row,
    settings,
    tag_rows,
)
from tests.test_admin_pictures import URL, XHR, add, client, sql  # noqa: F401
from tests.test_ingest import TAGS, FakeDescriber, FakeDetector, drop, png, q, run

NUDE = ("topless", "bare-buttocks")


# -- the call -----------------------------------------------------------------


def test_prompt_lists_every_kind() -> None:
    text = prompt(runtime.default("nudity.prompt"))
    assert "{kinds}" not in text
    for kind in KIND_NAMES:
        assert f"`{kind}`" in text
    assert "—" not in text


def test_clean_orders_and_validates() -> None:
    got = clean({"seen": "a nude — seen", "kinds": ["bare-buttocks", "topless", "topless"]}, "m")
    assert got == Nudity(kinds=("topless", "bare-buttocks"), seen="a nude -- seen", model="m")
    assert got.nude and got.tags == ["nudity", "topless", "bare-buttocks"]
    none = clean({"seen": "nothing", "kinds": []}, "m")
    assert not none.nude and none.tags == []
    with pytest.raises(jsonschema.ValidationError):
        clean({"seen": "x", "kinds": ["cleavage"]}, "m")
    with pytest.raises(jsonschema.ValidationError):
        clean({"kinds": []}, "m")


class FakeClient:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        text = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=None)


async def test_detect_retries_once_then_gives_up(settings: Settings) -> None:
    c = FakeClient("not json", '{"seen": "a bare back", "kinds": ["bare-buttocks"]}')
    got = await detect(b"x", "image/webp", settings=settings, client=c)
    assert got.kinds == ("bare-buttocks",) and len(c.calls) == 2
    assert c.calls[0]["system"] == prompt(runtime.default("nudity.prompt"))
    assert c.calls[0]["messages"][0]["content"][0]["type"] == "image"
    c = FakeClient('{"kinds": []}', "still not json")
    with pytest.raises(DescribeError) as e:
        await detect(b"x", "image/webp", settings=settings, client=c)
    assert e.value.retryable


async def test_describe_and_haiku_are_told(settings: Settings) -> None:
    from penish.worker.describe import describe
    from penish.worker.haiku import haiku_from

    reply = (
        '{"analysis": "Subject: a.\\n\\nSetting: b.\\n\\nStyle: c.\\n\\nMood: d.",'
        ' "angle": "x", "caption": "a cup", "description": "A cup."}'
    )
    c = FakeClient(reply, reply)
    await describe(b"x", "image/webp", want={"caption", "description"}, settings=settings, client=c)
    await describe(
        b"x",
        "image/webp",
        want={"caption", "description"},
        settings=settings,
        client=c,
        nudity=NUDE,
    )
    plain, told = (call["messages"][0]["content"][1]["text"] for call in c.calls)
    assert "Nudity" not in plain
    assert told.startswith("Nudity in this picture: topless, bare-buttocks.")
    assert "Nudity." in c.calls[0]["system"]

    lines = '{"drafts": ["a / b / c"], "lines": ["a", "b", "c"]}'
    c = FakeClient(lines, lines)
    await haiku_from("A cup.", settings=settings, client=c, examples=[])
    await haiku_from("A cup.", settings=settings, client=c, examples=[], nudity=NUDE)
    assert [call["messages"][0]["content"] for call in c.calls] == [
        "A cup.",
        "A cup.\n\nNudity in this picture: topless, bare-buttocks.",
    ]


# -- ingest -------------------------------------------------------------------


async def nude_rows(p: Any, image_id: int) -> list[dict[str, Any]]:
    return await q(p, "select kinds, seen, model from image_nudity where image_id = %s", image_id)


async def cats(p: Any, image_id: int) -> list[tuple[str, str]]:
    rows = await q(
        p,
        "select category, source from image_categories where image_id = %s order by category",
        image_id,
    )
    return [(r["category"], r["source"]) for r in rows]


async def test_ingest_checks_before_describing(p: Any, settings: Settings) -> None:
    d = FakeDescriber()
    det = FakeDetector(kinds=NUDE)
    await run(drop(settings, "nude", png(1)), p, settings, describer=d, detector=det)
    image_id = int((await q(p, "select id from images"))[0]["id"])
    assert (await row(p, image_id))["status"] == "live"
    assert det.calls == 1
    # Told before it wrote, and the detector's tags did not stop it from tagging.
    assert d.told == [NUDE] and "tags" in d.calls[0]
    assert await nude_rows(p, image_id) == [{"kinds": list(NUDE), "seen": "a nude", "model": "d1"}]
    tags = await tag_rows(p, image_id)
    assert {t for t, s in tags if s == "detect"} == {"nudity", *NUDE}
    assert {t for t, s in tags if s == "model"} == set(TAGS)
    assert await cats(p, image_id) == [("erotica", "detect")]

    # A re-drop keeps the result, as it keeps the text.
    await run(drop(settings, "nude", png(1)), p, settings, describer=d, detector=det)
    assert det.calls == 1


async def test_ingest_clean_picture(p: Any, settings: Settings) -> None:
    d = FakeDescriber()
    await run(drop(settings, "c", png(2)), p, settings, describer=d, detector=FakeDetector())
    image_id = int((await q(p, "select id from images"))[0]["id"])
    assert d.told == [()]
    assert await nude_rows(p, image_id) == [{"kinds": [], "seen": "nothing", "model": "d1"}]
    assert all(s == "model" for _, s in await tag_rows(p, image_id))
    assert await cats(p, image_id) == []


async def test_ingest_detector_failures(p: Any, settings: Settings) -> None:
    d = FakeDescriber()
    busy = FakeDetector(fail=DescribeError("busy", retryable=True))
    j = drop(settings, "f", png(3))
    with pytest.raises(SoftError):
        await run(j, p, settings, describer=d, detector=busy)
    image_id = int((await q(p, "select id from images"))[0]["id"])
    assert (await row(p, image_id))["status"] == "pending_enrichment"
    assert d.calls == []

    # A refusal does not hold the picture back; it goes up unchecked.
    no = FakeDetector(fail=DescribeError("refused", retryable=False))
    await run(j, p, settings, describer=d, detector=no)
    assert (await row(p, image_id))["status"] == "live"
    assert d.told == [()]
    assert await nude_rows(p, image_id) == []


async def test_haiku_job_is_told(p: Any, settings: Settings) -> None:
    await run(drop(settings, "h", png(4)), p, settings, detector=FakeDetector(kinds=("sex-act",)))
    image_id = int((await q(p, "select id from images"))[0]["id"])
    told: list[tuple[str, ...]] = []

    async def writer(
        description: str, *, settings: Settings, pool: Any, nudity: Any = ()
    ) -> tuple[str, str, str]:
        told.append(tuple(nudity))
        return "a", "b", "c"

    await handle_haiku(
        job("haiku", {"image_id": image_id}), pool=p, settings=settings, writer=writer
    )
    assert told == [("sex-act",)]


# -- rerun --------------------------------------------------------------------


def fixed(kinds: tuple[str, ...], fail: Exception | None = None) -> Any:
    async def go(data: bytes, mime: str, **kw: Any) -> Nudity:
        if fail:
            raise fail
        return Nudity(kinds=kinds, seen="seen", model="d2")

    return go


async def rerun(p: Any, settings: Settings, image_id: int, kinds: tuple[str, ...]) -> None:
    await nudity.handle_nudity(
        job(nudity.JOB, {"image_id": image_id}),
        pool=p,
        settings=settings,
        detector=fixed(kinds),
    )


async def test_rerun_adds_then_takes_back_only_its_own(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    async with p.connection() as conn:
        # The describer already said nude; that tag is not the detector's to remove.
        await conn.execute(
            "insert into tags (image_id, tag, source, model) values (%s, 'nudity', 'model', 'm')",
            (image_id,),
        )
        await conn.execute(
            "insert into image_categories (image_id, category, source, model)"
            " values (%s, 'fine-art', 'model', 'c')",
            (image_id,),
        )
    before = await tag_rows(p, image_id)

    await rerun(p, settings, image_id, ("fully-nude", "full-frontal"))
    tags = await tag_rows(p, image_id)
    assert ("nudity", "model") in tags
    assert {t for t, s in tags if s == "detect"} == {"full-frontal", "fully-nude"}
    assert await cats(p, image_id) == [("erotica", "detect"), ("fine-art", "model")]
    assert (await nude_rows(p, image_id))[0]["kinds"] == ["fully-nude", "full-frontal"]

    await rerun(p, settings, image_id, ("topless",))
    assert {t for t, s in await tag_rows(p, image_id) if s == "detect"} == {"topless"}

    await rerun(p, settings, image_id, ())
    assert await tag_rows(p, image_id) == before
    assert await cats(p, image_id) == [("fine-art", "model")]
    assert (await nude_rows(p, image_id))[0]["kinds"] == []


async def test_owner_categories_get_no_erotica(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    async with p.connection() as conn:
        await conn.execute(
            "insert into image_categories (image_id, category, source)"
            " values (%s, 'collage', 'owner')",
            (image_id,),
        )
    await rerun(p, settings, image_id, ("topless",))
    assert await cats(p, image_id) == [("collage", "owner")]
    assert ("topless", "detect") in await tag_rows(p, image_id)


async def test_rerun_failures_and_missing_images(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    j = job(nudity.JOB, {"image_id": image_id})
    busy = fixed((), DescribeError("busy", retryable=True))
    with pytest.raises(SoftError):
        await nudity.handle_nudity(j, pool=p, settings=settings, detector=busy)
    no = fixed((), DescribeError("no", retryable=False))
    with pytest.raises(HardError):
        await nudity.handle_nudity(j, pool=p, settings=settings, detector=no)
    await rerun(p, settings, 999999, ("topless",))
    with pytest.raises(HardError):
        await nudity.handle_nudity(job(nudity.JOB, {}), pool=p, settings=settings)


# -- the other writers --------------------------------------------------------


def classifier(found: list[str]) -> Any:
    async def go(data: bytes, mime: str, **kw: Any) -> tuple[list[str], str]:
        return found, "classifier"

    return go


async def test_categorize_keeps_the_detectors_erotica(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await rerun(p, settings, image_id, ("topless",))
    j = job("categorize", {"image_id": image_id})
    await handle_categorize(j, pool=p, settings=settings, classifier=classifier(["drawing"]))
    assert await cats(p, image_id) == [("drawing", "model"), ("erotica", "detect")]
    # A detect row alone does not count as filed: the sweep still files the picture.
    async with p.connection() as conn:
        await conn.execute("delete from image_categories where source = 'model'")
        await conn.execute("delete from jobs")
    assert await enqueue_uncategorized(p) == 1

    # The classifier choosing erotica on its own makes it the classifier's, so a clean
    # rerun leaves it.
    found = classifier(["erotica", "drawing"])
    await handle_categorize(j, pool=p, settings=settings, classifier=found)
    assert await cats(p, image_id) == [("drawing", "model"), ("erotica", "model")]
    await rerun(p, settings, image_id, ())
    assert await cats(p, image_id) == [("drawing", "model"), ("erotica", "model")]


async def test_regen_tells_and_keeps_detect_tags(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await rerun(p, settings, image_id, ("bare-buttocks",))
    d = Redescriber()
    told: list[tuple[str, ...]] = []

    async def writer(
        description: str, *, settings: Settings, pool: Any, nudity: Any = ()
    ) -> tuple[str, str, str]:
        told.append(tuple(nudity))
        return "x", "y", "z"

    await handle_regen(
        job("regen", {"image_id": image_id, "fields": ["tags", "haiku"], "force_owner": True}),
        pool=p,
        settings=settings,
        embedder=FakeEmbedder(),
        describer=d,
        writer=writer,
    )
    assert d.told == [("bare-buttocks",)] and told == [("bare-buttocks",)]
    assert {t for t, s in await tag_rows(p, image_id) if s == "detect"} == {
        "nudity",
        "bare-buttocks",
    }


async def test_crop_queues_a_check(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 600, "h": 400})
    rows = await q(p, "select payload from jobs where type = %s", nudity.JOB)
    assert rows == [{"payload": {"image_id": image_id}}]


# -- sweeps -------------------------------------------------------------------


async def test_sweeps(p: Any, settings: Settings) -> None:
    a = await live(p, settings, seed=1)
    b = await live(p, settings, seed=2)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
        await conn.execute("delete from image_nudity where image_id = %s", (b,))
        await conn.execute("update images set status = 'unpublished' where id = %s", (a,))
    # Only live pictures never checked.
    assert await nudity.enqueue_unchecked(p) == 1
    assert await nudity.enqueue_unchecked(p) == 0
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await nudity.handle_nudity_all(job(nudity.ALL_JOB, {}), pool=p)
    rows = await q(p, "select payload from jobs where type = %s order by id", nudity.JOB)
    assert rows == [{"payload": {"image_id": a}}, {"payload": {"image_id": b}}]


# -- admin --------------------------------------------------------------------


async def test_admin_bulk_all_list_and_detail(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, caption="a")
    b = add(clean_db, 2, caption="b")
    c = add(clean_db, 3, caption="c")
    sql(
        clean_db,
        "insert into image_nudity (image_id, kinds, seen, model) values"
        " (%s, '{topless}', 'a bare chest', 'd'), (%s, '{}', 'nothing', 'd')",
        a,
        b,
    )

    r = await client.post(f"{URL}/bulk", headers=XHR, json={"action": "nudity", "ids": [a, 99]})
    assert r.json() == {"action": "nudity", "done": [a], "queued": [a]}
    r = await client.post(f"{URL}/bulk", headers=XHR, json={"action": "nudity", "ids": [a]})
    assert r.json()["queued"] == []

    r = await client.post(f"{URL}/nudity", headers=XHR)
    assert r.json()["queued"] is True
    assert (await client.post(f"{URL}/nudity", headers=XHR)).json()["queued"] is False
    assert sql(clean_db, "select count(*) from jobs where type = %s", nudity.ALL_JOB) == [(1,)]

    async def ids(nudity: str) -> list[int]:
        r = await client.get(URL, params={"nudity": nudity})
        assert r.status_code == 200
        return [i["id"] for i in r.json()["items"]]

    assert await ids("nude") == [a]
    assert await ids("clean") == [b]
    assert await ids("unchecked") == [c]
    assert sorted(await ids("")) == [a, b, c]
    assert (await client.get(URL, params={"nudity": "maybe"})).status_code == 422
    items = {i["id"]: i["nudity"] for i in (await client.get(URL)).json()["items"]}
    assert items == {a: ["topless"], b: [], c: None}

    got = (await client.get(f"{URL}/{a}")).json()["nudity"]
    assert got["kinds"] == ["topless"] and got["seen"] == "a bare chest" and got["checked_at"]
    assert (await client.get(f"{URL}/{c}")).json()["nudity"] is None
