# ruff: noqa: F811  -- shared fixtures are imported, then named as parameters.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from penish.categories import CATEGORIES, SLUGS
from penish.config import Settings
from penish.worker.categorize import (
    classify,
    clean,
    enqueue_uncategorized,
    handle_categorize,
    prompt,
)
from penish.worker.describe import DescribeError
from penish.worker.jobs import HardError, SoftError
from tests.test_admin_jobs import job, live, p, settings  # noqa: F401
from tests.test_ingest import q


def test_list_includes_the_rooms_asked_for() -> None:
    for slug in ("fine-art", "modern-art", "cartoon", "photography", "photojournalism", "erotica"):
        assert slug in SLUGS
    assert len(set(SLUGS)) == len(SLUGS)
    assert all("\u2014" not in note for _, _, note in CATEGORIES)


def test_prompt_lists_every_category() -> None:
    text = prompt()
    assert "{categories}" not in text
    for slug in SLUGS:
        assert f"`{slug}`" in text


def test_clean() -> None:
    assert clean({"categories": ["cartoon"]}) == ["cartoon"]
    assert clean({"categories": ["poster", "printmaking", "poster"]}) == ["poster", "printmaking"]
    assert clean({"categories": ["fine-art", "drawing", "erotica"]}) == ["fine-art", "erotica"]
    with pytest.raises(ValueError):
        clean({"categories": []})
    with pytest.raises(jsonschema.ValidationError):
        clean({"categories": ["sculpture", "pottery"]})


class FakeClient:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        text = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=None)


async def test_classify_retries_once_then_gives_up(settings: Settings) -> None:
    c = FakeClient('{"categories": []}', '{"categories": ["photojournalism"]}')
    cats, _ = await classify(b"x", "image/webp", settings=settings, client=c)
    assert cats == ["photojournalism"] and len(c.calls) == 2
    assert c.calls[0]["system"] == prompt()
    c = FakeClient('{"categories": []}', "not json")
    with pytest.raises(DescribeError) as e:
        await classify(b"x", "image/webp", settings=settings, client=c)
    assert e.value.retryable


def fixed(cats: list[str], fail: Exception | None = None) -> Any:
    calls: list[bytes] = []

    async def go(data: bytes, mime: str, **kw: Any) -> tuple[list[str], str]:
        calls.append(data)
        if fail:
            raise fail
        return cats, "classifier"

    go.calls = calls  # type: ignore[attr-defined]
    return go


async def cats_of(p: Any, image_id: int) -> list[tuple[str, str]]:
    rows = await q(
        p,
        "select category, source from image_categories where image_id = %s order by category",
        image_id,
    )
    return [(r["category"], r["source"]) for r in rows]


async def test_files_a_picture_and_refiles_it(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await handle_categorize(
        job("categorize", {"image_id": image_id}),
        pool=p,
        settings=settings,
        classifier=fixed(["photography"]),
    )
    assert await cats_of(p, image_id) == [("photography", "model")]
    await handle_categorize(
        job("categorize", {"image_id": image_id}),
        pool=p,
        settings=settings,
        classifier=fixed(["erotica", "fine-art"]),
    )
    assert await cats_of(p, image_id) == [("erotica", "model"), ("fine-art", "model")]


async def test_owner_categories_are_never_replaced(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    async with p.connection() as conn:
        await conn.execute(
            "insert into image_categories (image_id, category, source)"
            " values (%s, 'collage', 'owner')",
            (image_id,),
        )
    clf = fixed(["cartoon"])
    await handle_categorize(
        job("categorize", {"image_id": image_id}), pool=p, settings=settings, classifier=clf
    )
    assert await cats_of(p, image_id) == [("collage", "owner")]
    assert clf.calls == []


async def test_failures_and_missing_images(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    j = job("categorize", {"image_id": image_id})
    with pytest.raises(SoftError):
        await handle_categorize(
            j,
            pool=p,
            settings=settings,
            classifier=fixed([], DescribeError("busy", retryable=True)),
        )
    with pytest.raises(HardError):
        await handle_categorize(
            j,
            pool=p,
            settings=settings,
            classifier=fixed([], DescribeError("no", retryable=False)),
        )
    assert await cats_of(p, image_id) == []
    await handle_categorize(
        job("categorize", {"image_id": 999999}),
        pool=p,
        settings=settings,
        classifier=fixed(["cartoon"]),
    )


async def test_sweep_enqueues_each_uncategorized_live_picture_once(
    p: Any, settings: Settings
) -> None:
    a = await live(p, settings, seed=1)
    b = await live(p, settings, seed=2)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
        await conn.execute(
            "insert into image_categories (image_id, category, source)"
            " values (%s, 'drawing', 'model')",
            (a,),
        )
    assert await enqueue_uncategorized(p) == 1
    assert await enqueue_uncategorized(p) == 0
    rows = await q(p, "select payload from jobs where type = 'categorize'")
    assert rows == [{"payload": {"image_id": b}}]
