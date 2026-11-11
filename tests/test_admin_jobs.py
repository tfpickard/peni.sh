from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.worker.admin_jobs import (
    HANDLERS,
    handle_crop,
    handle_grow_haikus,
    handle_redescribe_all,
    handle_reembed_all,
    handle_regen,
    make_handlers,
    regen_key,
)
from penish.worker.describe import Described, DescribeError, expand
from penish.worker.grow import Stats
from penish.worker.jobs import HardError, Job, SoftError
from tests.test_ingest import DIM, FakeDescriber, FakeEmbedder, drop, png, pub_files, q, run

TAGS2 = ["fern", "bulb", "stool", "pane", "ash", "night"]
ANALYSIS2 = "Subject: a bulb.\n\nSetting: ferns.\n\nStyle: photo.\n\nMood: still."


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


class Redescriber:
    """Answers with text unlike ingest's FakeDescriber, so every write is visible."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[set[str]] = []
        self.sizes: list[tuple[int, int]] = []
        self.fail = fail

    async def __call__(
        self, image_bytes: bytes, mime: str, *, want: set[str], settings: Settings, pool: Any
    ) -> Described:
        import pyvips

        img = pyvips.Image.new_from_buffer(image_bytes, "")
        self.sizes.append((img.width, img.height))
        self.calls.append(set(want))
        if self.fail is not None:
            raise self.fail
        want = expand(want)
        return Described(
            caption="A bulb among ferns" if "caption" in want else None,
            description="A bare bulb hangs over ferns." if "description" in want else None,
            tags=list(TAGS2) if "tags" in want else None,
            model="re-describer",
            analysis=ANALYSIS2 if "analysis" in want else None,
        )


async def writer(description: str, *, settings: Settings, pool: Any) -> tuple[str, str, str]:
    return ("bulb over ferns", "the room keeps its own weather", "nobody asked")


def job(type: str, payload: dict[str, Any], id: int = 0) -> Job:
    return Job(id=id, type=type, payload=payload, attempts=1, last_error=None)


async def live(p: Any, settings: Settings, seed: int = 1, w: int = 1200, h: int = 800) -> int:
    await run(drop(settings, f"s{seed}", png(seed, w, h)), p, settings, FakeDescriber())
    rows = await q(p, "select id from images order by id desc limit 1")
    return int(rows[0]["id"])


async def row(p: Any, image_id: int) -> dict[str, Any]:
    rows = await q(p, "select * from images where id = %s", image_id)
    return rows[0]


async def text_rows(p: Any, image_id: int) -> dict[str, dict[str, Any]]:
    rows = await q(p, "select * from image_texts where image_id = %s", image_id)
    return {r["field"]: r for r in rows}


async def tag_rows(p: Any, image_id: int) -> list[tuple[str, str]]:
    rows = await q(p, "select tag, source from tags where image_id = %s order by tag", image_id)
    return [(r["tag"], r["source"]) for r in rows]


async def vec(p: Any, image_id: int) -> list[float] | None:
    rows = await q(p, "select vec from embeddings where image_id = %s", image_id)
    return list(rows[0]["vec"].to_numpy().tolist()) if rows else None


async def owner(p: Any, image_id: int, field: str, body: str) -> None:
    async with p.connection() as conn:
        await conn.execute(
            "update image_texts set body = %s, source = 'owner', model = null"
            " where image_id = %s and field = %s",
            (body, image_id, field),
        )


async def regen(
    p: Any,
    settings: Settings,
    image_id: int,
    fields: list[str],
    force: bool = False,
    describer: Any = None,
    embedder: Any = None,
) -> None:
    await handle_regen(
        job("regen", {"image_id": image_id, "fields": fields, "force_owner": force}),
        pool=p,
        settings=settings,
        embedder=embedder or FakeEmbedder(),
        describer=describer or Redescriber(),
        writer=writer,
    )


async def crop(
    p: Any, settings: Settings, image_id: int, rect: dict[str, int] | None, **kw: Any
) -> Redescriber:
    d = kw.pop("describer", None) or Redescriber()
    await handle_crop(
        job("crop", {"image_id": image_id, "rect": rect}, kw.pop("id", 0)),
        pool=p,
        settings=settings,
        embedder=FakeEmbedder(),
        describer=d,
        writer=writer,
    )
    return d


async def history(p: Any) -> int:
    return len(await q(p, "select 1 from corpus_stats_history"))


# -- regen ------------------------------------------------------------------


async def test_regen_skips_owner_text(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await owner(p, image_id, "caption", "Mine")
    before_vec, before_hist = await vec(p, image_id), await history(p)
    d, e = Redescriber(), FakeEmbedder()
    await regen(p, settings, image_id, ["caption", "description", "tags"], describer=d, embedder=e)

    assert d.calls == [{"description", "analysis", "tags"}]
    t = await text_rows(p, image_id)
    assert (t["caption"]["body"], t["caption"]["source"]) == ("Mine", "owner")
    assert t["description"]["body"] == "A bare bulb hangs over ferns."
    assert t["description"]["model"] == "re-describer"
    assert (t["analysis"]["body"], t["analysis"]["model"]) == (ANALYSIS2, "re-describer")
    assert await tag_rows(p, image_id) == sorted((x, "model") for x in TAGS2)
    # A new analysis is a new vector and a stats refresh.
    assert e.calls == [[ANALYSIS2]]
    after = await vec(p, image_id)
    assert after is not None and after != before_vec
    assert await history(p) == before_hist + 1


async def test_regen_all_owner_calls_nothing(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await owner(p, image_id, "caption", "Mine")
    await owner(p, image_id, "description", "My words.")
    async with p.connection() as conn:
        await conn.execute("update tags set source = 'owner' where image_id = %s", (image_id,))
    d = Redescriber()
    await regen(p, settings, image_id, ["caption", "description", "tags"], describer=d)
    assert d.calls == []
    assert (await text_rows(p, image_id))["description"]["body"] == "My words."


async def test_force_owner_applies_only_to_listed_fields(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await owner(p, image_id, "caption", "Mine")
    await owner(p, image_id, "description", "My words.")
    d = Redescriber()
    await regen(p, settings, image_id, ["caption"], force=True, describer=d)
    assert d.calls == [{"caption"}]
    t = await text_rows(p, image_id)
    assert (t["caption"]["body"], t["caption"]["source"]) == ("A bulb among ferns", "model")
    assert (t["description"]["body"], t["description"]["source"]) == ("My words.", "owner")


async def test_regen_description_brings_analysis_unless_owner(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    d, e = Redescriber(), FakeEmbedder()
    await regen(p, settings, image_id, ["description"], force=True, describer=d, embedder=e)
    assert d.calls == [{"description", "analysis"}] and e.calls == [[ANALYSIS2]]
    t = await text_rows(p, image_id)
    assert (t["analysis"]["body"], t["analysis"]["source"]) == (ANALYSIS2, "model")

    await owner(p, image_id, "analysis", "Subject: mine.")
    await owner(p, image_id, "description", "My words.")
    before = await vec(p, image_id)
    d, e = Redescriber(), FakeEmbedder()
    await regen(p, settings, image_id, ["description"], force=True, describer=d, embedder=e)
    assert d.calls == [{"description"}] and e.calls == []
    t = await text_rows(p, image_id)
    assert t["description"]["source"] == "model"
    assert (t["analysis"]["body"], t["analysis"]["source"]) == ("Subject: mine.", "owner")
    assert await vec(p, image_id) == before

    # Asked for by name, the owner analysis goes back to the model.
    await regen(p, settings, image_id, ["analysis"], force=True, describer=d, embedder=e)
    assert d.calls[-1] == {"analysis"} and e.calls == [[ANALYSIS2]]
    assert (await text_rows(p, image_id))["analysis"]["source"] == "model"


async def test_force_owner_replaces_owner_tags(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    async with p.connection() as conn:
        await conn.execute("delete from tags where image_id = %s", (image_id,))
        await conn.execute(
            "insert into tags (image_id, tag, source) values (%s, 'mine', 'owner')", (image_id,)
        )
    d = Redescriber()
    await regen(p, settings, image_id, ["tags"], describer=d)
    assert d.calls == [] and await tag_rows(p, image_id) == [("mine", "owner")]
    await regen(p, settings, image_id, ["tags"], force=True, describer=d)
    assert await tag_rows(p, image_id) == sorted((x, "model") for x in TAGS2)


async def test_regen_embedding_uses_current_analysis(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await owner(p, image_id, "description", "Owner words now.")
    await owner(p, image_id, "analysis", "Subject: owner analysis now.")
    async with p.connection() as conn:
        await conn.execute("delete from embeddings")
    e = FakeEmbedder()
    before = await history(p)
    await regen(p, settings, image_id, ["embedding"], embedder=e)
    assert e.calls == [["Subject: owner analysis now."]]
    assert await vec(p, image_id) is not None
    assert await history(p) == before + 1


async def test_regen_haiku_links_image(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await regen(p, settings, image_id, ["haiku"])
    rows = await q(p, "select l1, image_id, source from haikus where image_id = %s", image_id)
    assert rows == [{"l1": "bulb over ferns", "image_id": image_id, "source": "model"}]


async def test_regen_bad_payload_and_missing_image(p: Any, settings: Settings) -> None:
    with pytest.raises(HardError):
        await regen(p, settings, 1, ["nope"])
    with pytest.raises(HardError):
        await regen(p, settings, 1, [])
    # Deleted while queued: nothing to do, not an error.
    await regen(p, settings, 999, ["caption"])


async def test_regen_describe_errors(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    soft = Redescriber(fail=DescribeError("overloaded", retryable=True))
    with pytest.raises(SoftError):
        await regen(p, settings, image_id, ["caption"], describer=soft)
    hard = Redescriber(fail=DescribeError("bad request", retryable=False))
    with pytest.raises(HardError):
        await regen(p, settings, image_id, ["caption"], describer=hard)


# -- crop -------------------------------------------------------------------


async def test_crop_then_uncrop(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["sha256"]
    await owner(p, image_id, "caption", "Mine")
    assert pub_files(settings, sha)

    d = await crop(p, settings, image_id, {"x": 100, "y": 50, "w": 600, "h": 500})
    r = await row(p, image_id)
    assert r["crop"] == {"x": 100, "y": 50, "w": 600, "h": 500}
    assert (r["width"], r["height"]) == (600, 500) and r["pub_sha"] != sha
    assert r["blurhash"] and len(r["palette"]) == 5
    assert pub_files(settings, sha) == []
    assert f"{r['pub_sha']}-480.webp" in pub_files(settings, r["pub_sha"])
    # Described from the cropped pixels, model fields only.
    assert d.calls == [{"description", "analysis", "tags"}] and d.sizes == [(600, 500)]
    t = await text_rows(p, image_id)
    assert t["caption"]["body"] == "Mine" and t["description"]["model"] == "re-describer"
    assert await vec(p, image_id) is not None

    cropped = r["pub_sha"]
    await crop(p, settings, image_id, None)
    r = await row(p, image_id)
    # Rebuilt names come from the pixels, so the full frame is not back under sha256.
    assert r["crop"] is None and (r["width"], r["height"]) == (1200, 800)
    assert r["pub_sha"] not in (sha, cropped)
    assert pub_files(settings, cropped) == []
    names = pub_files(settings, r["pub_sha"])
    assert f"{r['pub_sha']}-960.webp" in names and f"{r['pub_sha']}-1600.webp" not in names


async def test_crop_keeps_shared_pub_files(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["sha256"]
    async with p.connection() as conn:
        await conn.execute(
            "insert into images (sha256, pub_sha, status) values (%s, %s, 'live')", ("f" * 64, sha)
        )
    await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 400, "h": 400})
    assert f"{sha}-480.webp" in pub_files(settings, sha)


async def test_crop_full_frame_is_uncropped(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["sha256"]
    await crop(p, settings, image_id, {"x": -5, "y": 0, "w": 5000, "h": 5000})
    r = await row(p, image_id)
    assert r["crop"] is None and (r["width"], r["height"]) == (1200, 800)
    assert pub_files(settings, sha) == []
    assert f"{r['pub_sha']}-480.webp" in pub_files(settings, r["pub_sha"])


async def test_crop_failures(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    with pytest.raises(HardError):
        await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 10, "h": 10})
    with pytest.raises(HardError):
        await crop(p, settings, image_id, cast(Any, [1, 2]))
    for f in settings.media_orig.iterdir():
        f.unlink()
    with pytest.raises(HardError):
        await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 400, "h": 400})
    await crop(p, settings, 999, None)


async def test_crop_superseded_by_newer(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["sha256"]
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into jobs (type, payload) values ('crop', %s) returning id",
            (Jsonb({"image_id": image_id, "rect": None}),),
        )
        newer = (await cur.fetchone())["id"]
    d = await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 400, "h": 400}, id=newer - 1)
    assert d.calls == [] and (await row(p, image_id))["pub_sha"] == sha


async def test_crop_describe_soft_error_retries_cleanly(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    rect = {"x": 0, "y": 0, "w": 500, "h": 400}
    with pytest.raises(SoftError):
        await crop(
            p, settings, image_id, rect, describer=Redescriber(DescribeError("x", retryable=True))
        )
    first = (await row(p, image_id))["pub_sha"]
    # The retry lands on the same deterministic pub_sha and keeps its files.
    await crop(p, settings, image_id, rect)
    assert (await row(p, image_id))["pub_sha"] == first
    assert f"{first}-480.webp" in pub_files(settings, first)


# -- bulk and grow ----------------------------------------------------------


async def test_reembed_all_enqueues_each_once(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    b = await live(p, settings, 2)
    async with p.connection() as conn:
        await conn.execute("insert into images (sha256, status) values (%s, 'queued')", ("e" * 64,))
    await handle_reembed_all(job("reembed_all", {}), pool=p)
    await handle_reembed_all(job("reembed_all", {}), pool=p)
    rows = await q(p, "select payload, dedupe_key from jobs where type = 'regen' order by id")
    assert rows == [
        {"payload": {"image_id": i, "fields": ["embedding"]}, "dedupe_key": f"regen:{i}:embedding"}
        for i in (a, b)
    ]
    assert rows[0]["dedupe_key"] == regen_key(a, ["embedding"])


async def test_redescribe_all_live_only(p: Any, settings: Settings) -> None:
    a = await live(p, settings, 1)
    async with p.connection() as conn:
        await conn.execute("insert into images (sha256, status) values (%s, 'failed')", ("e" * 64,))
    await handle_redescribe_all(job("redescribe_all", {}), pool=p)
    rows = await q(p, "select payload, dedupe_key from jobs where type = 'regen'")
    assert rows == [
        {
            "payload": {
                "image_id": a,
                "fields": ["caption", "description", "analysis", "tags"],
            },
            "dedupe_key": f"regen:{a}:caption,description,analysis,tags",
        }
    ]


async def test_grow_haikus(p: Any, settings: Settings) -> None:
    seen: list[int] = []

    async def grower(db: Any, n: int, *, settings: Settings, embedder: Any) -> Stats:
        seen.append(n)
        return Stats(accepted=n)

    await handle_grow_haikus(job("grow_haikus", {"n": 7}), pool=p, settings=settings, grower=grower)
    assert seen == [7]
    for bad in (0, 201, "5", True, None):
        with pytest.raises(HardError):
            await handle_grow_haikus(
                job("grow_haikus", {"n": bad}), pool=p, settings=settings, grower=grower
            )

    async def empty(db: Any, n: int, **kw: Any) -> Stats:
        raise ValueError("haiku pool is empty")

    with pytest.raises(HardError):
        await handle_grow_haikus(
            job("grow_haikus", {"n": 1}), pool=p, settings=settings, grower=empty
        )


async def test_make_handlers(p: Any, settings: Settings) -> None:
    handlers = make_handlers(p, settings, FakeEmbedder())
    assert (
        set(handlers)
        == set(HANDLERS)
        == {
            "regen",
            "crop",
            "reembed_all",
            "redescribe_all",
            "grow_haikus",
        }
    )
    await handlers["reembed_all"](job("reembed_all", {}))
    assert await q(p, "select 1 from jobs") == []
