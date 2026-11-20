"""The uploads record: what the form and the worker each write, and where `stages` says
an upload has got to."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from penish import uploads
from penish.config import Settings
from tests.test_ingest import DIM, q

KEYS = {
    "stem",
    "sha256",
    "filename",
    "bytes",
    "collection_line",
    "source",
    "stage",
    "error",
    "image_id",
    "slug",
    "created_at",
}


@pytest.fixture
def p(pool: object) -> Any:
    return pool


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


def sha(n: int) -> str:
    return f"{n:064x}"


async def record(
    p: Any,
    stem: str,
    n: int,
    *,
    source: str = "rsync",
    filename: str | None = None,
    line: str | None = None,
    size: int | None = 10,
) -> None:
    async with p.connection() as conn:
        await uploads.record(
            conn,
            stem=stem,
            sha256=sha(n),
            filename=filename,
            size=size,
            collection_line=line,
            source=source,
        )


async def rows(p: Any) -> list[dict[str, Any]]:
    return await q(p, "select * from uploads order by id")


async def stages(p: Any, settings: Settings, **kw: Any) -> list[dict[str, Any]]:
    async with p.connection() as conn:
        return await uploads.stages(conn, settings, **kw)


async def one(p: Any, settings: Settings, **kw: Any) -> dict[str, Any]:
    got = await stages(p, settings, **kw)
    assert len(got) == 1
    assert set(got[0]) == KEYS
    return got[0]


async def picture(p: Any, n: int, status: str) -> int:
    got = await q(
        p,
        "insert into images (sha256, pub_sha, slug, width, height, status, published_at)"
        " values (%s, %s, %s, 10, 10, %s, now()) returning id",
        sha(n),
        sha(n),
        f"img-{n}",
        status,
    )
    return int(got[0]["id"])


async def leaf(p: Any, image_id: int, slug: str = "pansophicon") -> None:
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " select %s, id, 1, 1 from collections where slug = %s returning image_id",
        image_id,
        slug,
    )


def place(root: Path, *names: str, sub: str | None = None) -> Path:
    d = root / sub if sub else root
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_text("x")
    return d


# -- record, fail, clear -------------------------------------------------------------


async def test_worker_never_downgrades_a_form_row(p: Any) -> None:
    await record(p, "cat-1a2b3c", 1, source="form", filename="IMG_0001.JPG", line="pansophicon")
    await record(p, "cat-1a2b3c", 1, source="rsync", filename="cat-1a2b3c.jpg", line=None)
    [r] = await rows(p)
    assert (r["source"], r["filename"], r["collection_line"], r["bytes"]) == (
        "form",
        "IMG_0001.JPG",
        "pansophicon",
        10,
    )


async def test_form_arriving_second_still_names_the_file(p: Any) -> None:
    await record(p, "cat-1a2b3c", 1, filename="cat-1a2b3c.jpg", line="pansophicon iii")
    await record(p, "cat-1a2b3c", 1, source="form", filename="IMG_0001.JPG", line=None)
    [r] = await rows(p)
    assert (r["source"], r["filename"], r["collection_line"]) == (
        "form",
        "IMG_0001.JPG",
        "pansophicon iii",
    )


async def test_rsync_rows_stay_rsync_and_keep_what_they_knew(p: Any) -> None:
    await record(p, "x", 1, filename="x.png", line="pansophicon")
    await record(p, "x", 1, filename=None, line=None, size=None)
    [r] = await rows(p)
    assert (r["source"], r["filename"], r["collection_line"], r["bytes"]) == (
        "rsync",
        "x.png",
        "pansophicon",
        10,
    )


async def test_error_survives_record_until_cleared(p: Any) -> None:
    await record(p, "x", 1)
    async with p.connection() as conn:
        await uploads.fail(conn, stem="x", sha256=sha(1), error="x.png: not a readable image")
    await record(p, "x", 1)
    [r] = await rows(p)
    assert r["error"] == "x.png: not a readable image"

    async with p.connection() as conn:
        await uploads.clear(conn, stem="x", sha256=sha(1))
    [r] = await rows(p)
    assert r["error"] is None


async def test_fail_without_a_row_makes_one_and_keeps_it_short(p: Any) -> None:
    async with p.connection() as conn:
        await uploads.fail(conn, stem="y", sha256=sha(2), error="e" * 5000)
    [r] = await rows(p)
    assert (r["stem"], r["source"], len(r["error"])) == ("y", "rsync", uploads.ERROR_MAX)


async def test_clear_touches_only_its_own_row(p: Any) -> None:
    for stem in ("a", "a-r1f2e"):
        async with p.connection() as conn:
            await uploads.fail(conn, stem=stem, sha256=sha(1), error="no")
    async with p.connection() as conn:
        await uploads.clear(conn, stem="a-r1f2e", sha256=sha(1))
    assert [(r["stem"], r["error"]) for r in await rows(p)] == [("a", "no"), ("a-r1f2e", None)]


# -- stages ---------------------------------------------------------------------------


async def test_newest_row_speaks_for_its_bytes(p: Any, settings: Settings) -> None:
    await record(p, "a", 1, source="form", filename="A.PNG", line="pansophicon")
    await record(p, "b", 2)
    # A failed folder put back into a busy inbox comes round again under a new stem.
    await record(p, "a-r1f2e", 1, filename="a-r1f2e.png", line="pansophicon")
    got = await stages(p, settings)
    assert [(g["stem"], g["sha256"]) for g in got] == [("a-r1f2e", sha(1)), ("b", sha(2))]
    assert all(set(g) == KEYS for g in got)
    assert got[0]["filename"] == "a-r1f2e.png" and got[0]["source"] == "rsync"


async def test_received(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, source="form", filename="X.PNG")
    got = await one(p, settings)
    assert (got["stage"], got["error"], got["image_id"], got["slug"]) == (
        "received",
        None,
        None,
        None,
    )
    assert (got["filename"], got["bytes"], got["source"]) == ("X.PNG", 10, "form")


async def test_inbox(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, source="form")
    place(settings.inbox, "x_collection.txt", "x.png", ".y.png.abcd1234")
    assert (await one(p, settings))["stage"] == "inbox"


async def test_ingesting_from_processing(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    place(settings.processing, "x.png", "x_caption.txt", sub=str(uuid.uuid4()))
    # A stale copy in the inbox does not pull it back a stage.
    place(settings.inbox, "x.png")
    assert (await one(p, settings))["stage"] == "ingesting"


async def test_ingesting_from_a_waiting_job(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    await q(
        p,
        "insert into jobs (type, payload) values ('ingest', %s::jsonb) returning id",
        '{"dir": "/nowhere", "stem": "x"}',
    )
    assert (await one(p, settings))["stage"] == "ingesting"
    await q(p, "update jobs set status = 'done' returning id")
    assert (await one(p, settings))["stage"] == "received"


@pytest.mark.parametrize("status", ["processing", "pending_enrichment"])
async def test_ingesting_from_the_image_row(p: Any, settings: Settings, status: str) -> None:
    await record(p, "x", 1)
    image_id = await picture(p, 1, status)
    got = await one(p, settings)
    assert (got["stage"], got["image_id"]) == ("ingesting", image_id)


async def test_failed_from_the_row(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, line="pansophicon")
    async with p.connection() as conn:
        await uploads.fail(conn, stem="x", sha256=sha(1), error="x.png: not a readable image")
    got = await one(p, settings)
    assert (got["stage"], got["error"]) == ("failed", "x.png: not a readable image")


async def test_work_under_way_outranks_a_stored_error(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, line="pansophicon")
    async with p.connection() as conn:
        await uploads.fail(conn, stem="x", sha256=sha(1), error="x.png: not a readable image")
    place(settings.processing, "x.png", sub=str(uuid.uuid4()))
    assert (await one(p, settings))["stage"] == "ingesting"


async def test_back_in_the_inbox_outranks_a_stored_error(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    async with p.connection() as conn:
        await uploads.fail(conn, stem="x", sha256=sha(1), error="x.png: not a readable image")
    place(settings.inbox, "x.png")
    got = await one(p, settings)
    assert got["stage"] == "inbox"
    # The reason it failed last time is kept for the page, whatever the stage now.
    assert got["error"] == "x.png: not a readable image"


async def test_a_stale_failed_folder_does_not_outrank_processing(
    p: Any, settings: Settings
) -> None:
    await record(p, "x", 1)
    d = place(settings.failed, "x.png", sub="x")
    (d / "x.err").write_text("x.png: an older picture of the same name\n")
    place(settings.processing, "x.png", sub=str(uuid.uuid4()))
    got = await one(p, settings)
    assert (got["stage"], got["error"]) == ("ingesting", None)


async def test_a_stale_failed_folder_does_not_outrank_a_waiting_job(
    p: Any, settings: Settings
) -> None:
    await record(p, "x", 1)
    place(settings.failed, "x.png", "x.err", sub="x")
    await q(
        p,
        "insert into jobs (type, payload) values ('ingest', %s::jsonb) returning id",
        '{"dir": "/nowhere", "stem": "x"}',
    )
    assert (await one(p, settings))["stage"] == "ingesting"


async def test_failed_from_the_folder_reads_the_err(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    # `x` belongs to an earlier picture of the same name; this one landed in `x-2`.
    d = place(settings.failed, "x.png", sub="x-2")
    (d / "x.err").write_text("x.png: " + "y" * 900 + "\n")
    got = await one(p, settings)
    assert got["stage"] == "failed"
    assert got["error"] == ("x.png: " + "y" * 900)[: uploads.ERROR_MAX]


async def test_failed_from_the_image_row(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    await picture(p, 1, "failed")
    got = await one(p, settings)
    assert (got["stage"], got["error"]) == ("failed", None)


async def test_unpublished_is_down(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, line="pansophicon")
    image_id = await picture(p, 1, "unpublished")
    got = await one(p, settings)
    assert (got["stage"], got["image_id"]) == ("down", image_id)


async def test_live(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    image_id = await picture(p, 1, "live")
    # A failure left behind by an earlier drop of the same name does not outrank it.
    place(settings.failed, "x.png", "x.err", sub="x")
    got = await one(p, settings)
    assert (got["stage"], got["image_id"], got["slug"]) == ("live", image_id, "img-1")


async def test_unfiled_then_filed(p: Any, settings: Settings) -> None:
    await record(p, "x", 1, line="pansophicon iii 14")
    image_id = await picture(p, 1, "live")
    assert (await one(p, settings))["stage"] == "unfiled"
    assert (await one(p, settings, only_unfiled=True))["stage"] == "unfiled"

    await leaf(p, image_id)
    assert (await one(p, settings))["stage"] == "filed"
    assert await stages(p, settings, only_unfiled=True) == []


async def test_a_leaf_elsewhere_is_not_the_one_asked_for(p: Any, settings: Settings) -> None:
    await q(
        p,
        "insert into collections (slug, title, abbrev, abbrev_latin, book_count, leaves_per_book)"
        " values ('bestiary', 'Bestiary', 'B', 'B', 1, 9) returning id",
    )
    await q(
        p,
        "insert into collection_books (collection_id, number)"
        " select id, 1 from collections where slug = 'bestiary' returning number",
    )
    await record(p, "x", 1, line="pansophicon")
    image_id = await picture(p, 1, "live")
    await leaf(p, image_id, "bestiary")
    assert (await one(p, settings))["stage"] == "unfiled"


async def test_a_leaf_with_no_line_is_filed(p: Any, settings: Settings) -> None:
    await record(p, "x", 1)
    image_id = await picture(p, 1, "live")
    await leaf(p, image_id)
    assert (await one(p, settings))["stage"] == "filed"


async def test_collection_filter_reads_bad_lines_too(p: Any, settings: Settings) -> None:
    await record(p, "a", 1, line="pansophicon")
    await record(p, "b", 2, line="Pansophicon, iii, 14")
    # The filer refuses these, but they were still meant for the Pansophicon.
    await record(p, "c", 3, line="pansophicon iiii")
    await record(p, "d", 4, line="pansophicon iii 14 and more")
    await record(p, "e", 5, line="pansophicon\nbestiary")
    await record(p, "f", 6, line="bestiary")
    await record(p, "g", 7, line="!! ??")
    await record(p, "h", 8, line="")
    await record(p, "i", 9)
    got = await stages(p, settings, collection="pansophicon")
    assert [g["stem"] for g in got] == ["e", "d", "c", "b", "a"]
    assert [g["stem"] for g in await stages(p, settings, collection="bestiary")] == ["f"]
    assert len(await stages(p, settings)) == 9
    assert await stages(p, settings, collection="nothing") == []


def test_collection_of() -> None:
    assert uploads.collection_of("pansophicon iii 14") == "pansophicon"
    assert uploads.collection_of("  PANSOPHICON  ") == "pansophicon"
    assert uploads.collection_of("pansophicon 0") == "pansophicon"
    assert uploads.collection_of("!!") is None
    assert uploads.collection_of("") is None
    assert uploads.collection_of(None) is None
