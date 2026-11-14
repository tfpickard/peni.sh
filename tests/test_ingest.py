from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import pyvips
from psycopg.types.json import Jsonb

from penish.config import Settings
from penish.worker.derive import rebuild_public
from penish.worker.describe import Described, DescribeError, expand
from penish.worker.ingest import handle_haiku, handle_ingest
from penish.worker.jobs import HardError, Job, SoftError

DIM = 16
TAGS = ["moss", "lamp", "chair", "window", "dust", "evening"]
DESCRIPTION = "The moss has a dry patch near the left edge. A lamp stands in it."
ANALYSIS = (
    "Subject: a brass lamp stands in a bed of moss.\n\nSetting: a forest floor.\n\n"
    "Style: photograph, green and brass.\n\nMood: quiet."
)


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


class FakeDescriber:
    def __init__(self, caption: str = "A lamp in moss", fail: Exception | None = None) -> None:
        self.caption = caption
        self.fail = fail
        self.calls: list[set[str]] = []

    async def __call__(
        self, image_bytes: bytes, mime: str, *, want: set[str], settings: Settings, pool: Any
    ) -> Described:
        self.calls.append(set(want))
        assert image_bytes and mime.startswith("image/")
        if self.fail is not None:
            raise self.fail
        # Same rule as the real describer: a description always comes with an analysis.
        want = expand(want)
        return Described(
            caption=self.caption if "caption" in want else None,
            description=DESCRIPTION if "description" in want else None,
            tags=list(TAGS) if "tags" in want else None,
            model="fake-describer",
            analysis=ANALYSIS if "analysis" in want else None,
        )


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).normal(size=DIM)
            out.append(v / np.linalg.norm(v))
        return np.asarray(out, dtype=np.float32)


def png(seed: int, w: int = 600, h: int = 400) -> bytes:
    arr = np.random.default_rng(seed).integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    img = pyvips.Image.new_from_memory(arr.tobytes(), w, h, 3, "uchar")
    out = img.write_to_buffer(".png")
    assert isinstance(out, bytes)
    return out


def drop(settings: Settings, stem: str, image: bytes, ext: str = "png", **sidecars: str) -> Job:
    d = settings.processing / str(uuid.uuid4())
    d.mkdir(parents=True)
    (d / f"{stem}.{ext}").write_bytes(image)
    for kind, body in sidecars.items():
        suffix = ".json" if kind == "embedding" else ".txt"
        (d / f"{stem}_{kind}{suffix}").write_text(body)
    return Job(
        id=0, type="ingest", payload={"dir": str(d), "stem": stem}, attempts=1, last_error=None
    )


async def run(
    job: Job, p: Any, settings: Settings, describer: Any = None, embedder: Any = None
) -> None:
    await handle_ingest(
        job,
        pool=p,
        settings=settings,
        embedder=embedder or FakeEmbedder(),
        describer=describer or FakeDescriber(),
    )


async def q(p: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    async with p.connection() as conn:
        cur = await conn.execute(sql, args)
        return cast(list[dict[str, Any]], await cur.fetchall())


async def image(p: Any) -> dict[str, Any]:
    rows = await q(p, "select * from images")
    assert len(rows) == 1
    return rows[0]


async def texts(p: Any, image_id: int) -> dict[str, dict[str, Any]]:
    rows = await q(p, "select * from image_texts where image_id = %s", image_id)
    return {r["field"]: r for r in rows}


def present(job: Job) -> bool:
    return Path(job.payload["dir"]).is_dir()


def pub_files(settings: Settings, sha: str) -> list[str]:
    return sorted(f.name for f in settings.media_pub.iterdir() if f.name.startswith(sha))


async def test_lone_png_goes_live(p: Any, settings: Settings) -> None:
    job = drop(settings, "lamp", png(1))
    desc = FakeDescriber()
    emb = FakeEmbedder()
    await run(job, p, settings, desc, emb)

    img = await image(p)
    assert img["status"] == "live" and img["slug"] == "a-lamp-in-moss"
    assert img["published_at"] is not None and img["width"] == 600 and img["mime"] == "image/png"
    assert img["blurhash"] and len(img["palette"]) == 5
    assert desc.calls == [{"caption", "description", "analysis", "tags"}]
    t = await texts(p, img["id"])
    assert {f: r["source"] for f, r in t.items()} == {
        "caption": "model",
        "description": "model",
        "analysis": "model",
    }
    assert t["description"]["body"] == DESCRIPTION and t["analysis"]["body"] == ANALYSIS
    # The vector comes from the faithful analysis, never from the public description.
    assert emb.calls == [[ANALYSIS]]
    assert t["caption"]["model"] == "fake-describer"
    tags = await q(p, "select tag, source from tags where image_id = %s order by tag", img["id"])
    assert {r["tag"] for r in tags} == set(TAGS) and {r["source"] for r in tags} == {"model"}
    emb = await q(p, "select model, dim from embeddings")
    assert emb == [{"model": "test/embed", "dim": DIM}]
    assert img["pub_sha"] == img["sha256"]
    assert f"{img['sha256']}-480.webp" in pub_files(settings, img["sha256"])
    assert (settings.media_orig / f"{img['sha256']}.png").is_file()
    assert not present(job)
    jobs = await q(p, "select type, payload, dedupe_key from jobs order by type")
    assert jobs == [
        {
            "type": kind,
            "payload": {"image_id": img["id"]},
            "dedupe_key": f"{kind}:{img['id']}",
        }
        # Facets ride the go-live transaction too; neither delays the publish.
        for kind in ("categorize", "haiku", "mycelium_extract", "mycelium_split")
    ]
    stats = await q(p, "select n from corpus_stats where kind = 'description'")
    assert stats == [{"n": 1}]


async def test_redrop_of_cropped_image_keeps_crop(p: Any, settings: Settings) -> None:
    data = png(30, 1200, 800)
    await run(drop(settings, "c", data), p, settings)
    img = await image(p)
    # What the crop job will do: point pub_sha at a cropped ladder and drop the old one.
    orig = (settings.media_orig / f"{img['sha256']}.png").read_bytes()
    crop = {"x": 100, "y": 100, "w": 600, "h": 500}
    pub = rebuild_public(orig, crop, False)
    async with p.connection() as conn:
        await conn.execute(
            "update images set crop = %s, pub_sha = %s, width = %s, height = %s where id = %s",
            (Jsonb(crop), pub.pub_sha, pub.width, pub.height, img["id"]),
        )
    for f in settings.media_pub.iterdir():
        f.unlink()

    # The cropped files are gone too, so this re-drop must heal them, not the full frame.
    await run(drop(settings, "c", data), p, settings)
    again = await image(p)
    assert again["pub_sha"] == pub.pub_sha and again["width"] == 600
    assert pub_files(settings, img["sha256"]) == []
    assert f"{pub.pub_sha}-480.webp" in pub_files(settings, pub.pub_sha)


async def test_owner_sidecars_skip_describe(p: Any, settings: Settings) -> None:
    job = drop(
        settings,
        "x",
        png(2),
        caption="Owner Caption",
        description="Owner words.",
        analysis="Subject: owner analysis.",
        tags="Alpha\nbeta\n\nalpha\n",
    )
    desc = FakeDescriber()
    emb = FakeEmbedder()
    await run(job, p, settings, desc, emb)

    assert desc.calls == []
    assert emb.calls == [["Subject: owner analysis."]]
    img = await image(p)
    assert img["status"] == "live" and img["slug"] == "owner-caption"
    t = await texts(p, img["id"])
    assert t["caption"]["body"] == "Owner Caption" and t["description"]["body"] == "Owner words."
    assert {r["source"] for r in t.values()} == {"owner"}
    assert all(r["model"] is None for r in t.values())
    tags = await q(p, "select tag, source from tags order by tag")
    assert tags == [{"tag": "alpha", "source": "owner"}, {"tag": "beta", "source": "owner"}]


async def test_owner_description_gets_model_analysis(p: Any, settings: Settings) -> None:
    desc = FakeDescriber()
    emb = FakeEmbedder()
    job = drop(settings, "o", png(16), caption="Mine", description="Owner words.", tags="a\nb")
    await run(job, p, settings, desc, emb)

    assert desc.calls == [{"analysis"}]
    img = await image(p)
    assert img["status"] == "live"
    t = await texts(p, img["id"])
    assert (t["description"]["body"], t["description"]["source"]) == ("Owner words.", "owner")
    assert (t["analysis"]["body"], t["analysis"]["source"]) == (ANALYSIS, "model")
    assert emb.calls == [[ANALYSIS]]


async def test_owner_description_never_embedded(p: Any, settings: Settings) -> None:
    emb = FakeEmbedder()
    down = FakeDescriber(fail=DescribeError("overloaded", retryable=True))
    job = drop(settings, "o", png(17), caption="Mine", description="Owner words.", tags="a\nb")
    with pytest.raises(SoftError):
        await run(job, p, settings, down, emb)

    img = await image(p)
    assert img["status"] == "pending_enrichment" and img["slug"] is None
    assert emb.calls == [] and await q(p, "select 1 from embeddings") == []
    assert present(job)


async def test_redrop_dedupes_and_owner_caption_reslugs(p: Any, settings: Settings) -> None:
    data = png(3)
    desc = FakeDescriber()
    await run(drop(settings, "a", data), p, settings, desc)
    first = await image(p)
    assert first["slug"] == "a-lamp-in-moss"

    await run(drop(settings, "a-again", data), p, settings, desc)
    again = await image(p)
    assert again["id"] == first["id"] and again["slug"] == first["slug"]
    assert again["published_at"] == first["published_at"]
    assert len(desc.calls) == 1

    job = drop(settings, "a", data, caption="Something else entirely")
    await run(job, p, settings, desc)
    img = await image(p)
    assert img["slug"] == "something-else-entirely"
    assert img["surprise"] == first["surprise"]
    t = await texts(p, img["id"])
    assert t["caption"]["source"] == "owner" and t["caption"]["body"] == "Something else entirely"
    aliases = await q(p, "select slug, image_id from slug_aliases")
    assert aliases == [{"slug": "a-lamp-in-moss", "image_id": img["id"]}]
    assert len(await q(p, "select 1 from jobs where type = 'haiku'")) == 1
    assert not present(job)


async def test_slug_collision_gets_suffix(p: Any, settings: Settings) -> None:
    await run(drop(settings, "a", png(4), caption="Same"), p, settings)
    await run(drop(settings, "b", png(5), caption="Same"), p, settings)
    rows = await q(p, "select slug, surprise from images order by id")
    assert [r["slug"] for r in rows] == ["same", "same-2"]
    # The first image had nothing to be compared against; the second did.
    assert rows[0]["surprise"] is None and rows[1]["surprise"] is not None


async def test_corrupt_file_goes_to_failed(p: Any, settings: Settings) -> None:
    job = drop(settings, "bad", b"\x89PNG\r\n\x1a\nnot really", caption="hi")
    with pytest.raises(HardError):
        await run(job, p, settings)
    dest = settings.failed / "bad"
    assert sorted(f.name for f in dest.iterdir()) == ["bad.err", "bad.png", "bad_caption.txt"]
    assert "not a readable image" in (dest / "bad.err").read_text()
    assert not present(job)
    assert await q(p, "select 1 from images") == []

    # A second failure with the same stem must not clobber the first.
    with pytest.raises(HardError):
        await run(drop(settings, "bad", b"junk"), p, settings)
    assert (settings.failed / "bad-2" / "bad.err").is_file()


async def test_bad_group_goes_to_failed(p: Any, settings: Settings) -> None:
    job = drop(settings, "two", png(6))
    d = Path(job.payload["dir"])
    (d / "other.png").write_bytes(png(7))
    (d / "notes.md").write_text("?")
    with pytest.raises(HardError):
        await run(job, p, settings)
    err = (settings.failed / "two" / "two.err").read_text()
    assert "more than one image" in err and "notes.md" in err


async def test_retryable_describe_error_then_success(p: Any, settings: Settings) -> None:
    job = drop(settings, "slow", png(8))
    with pytest.raises(SoftError):
        await run(job, p, settings, FakeDescriber(fail=DescribeError("overloaded", retryable=True)))
    img = await image(p)
    assert img["status"] == "pending_enrichment" and img["slug"] is None
    assert present(job)

    await run(job, p, settings)
    img = await image(p)
    assert img["status"] == "live"
    assert not present(job)


async def test_permanent_describe_error_goes_to_failed(p: Any, settings: Settings) -> None:
    job = drop(settings, "nope", png(9))
    with pytest.raises(HardError):
        await run(job, p, settings, FakeDescriber(fail=DescribeError("400", retryable=False)))
    assert (await image(p))["status"] == "failed"
    assert not present(job)
    dest = settings.failed / "nope"
    assert (dest / "nope.png").is_file()
    assert "describe: 400" in (dest / "nope.err").read_text()


async def test_crash_mid_job_then_rerun(p: Any, settings: Settings) -> None:
    job = drop(settings, "crash", png(10, w=1000, h=700))
    with pytest.raises(RuntimeError):
        await run(job, p, settings, FakeDescriber(fail=RuntimeError("killed")))
    img = await image(p)
    sha = img["sha256"]
    files = pub_files(settings, sha)
    assert f"{sha}-480.webp" in files and f"{sha}-960.webp" in files
    mtimes = {f: (settings.media_pub / f).stat().st_mtime_ns for f in files}

    emb = FakeEmbedder()
    await run(job, p, settings, embedder=emb)
    img = await image(p)
    assert img["status"] == "live"
    assert pub_files(settings, sha) == files
    assert {f: (settings.media_pub / f).stat().st_mtime_ns for f in files} == mtimes
    assert not [f for f in settings.media_pub.iterdir() if f.name.startswith(".")]
    assert len(await q(p, "select 1 from image_texts")) == 3
    assert len(await q(p, "select 1 from embeddings")) == 1
    assert len(emb.calls) == 1

    # Re-running a finished job (dir gone) is a no-op.
    await run(job, p, settings)
    # One haiku, categorize, extract and split job, not two of each.
    assert len(await q(p, "select 1 from jobs")) == 4


async def test_embedding_sidecar_wrong_model_ignored(p: Any, settings: Settings) -> None:
    side = json.dumps({"model": "someone/else", "dim": DIM, "vector": [1.0] * DIM})
    emb = FakeEmbedder()
    await run(drop(settings, "e", png(11), embedding=side), p, settings, embedder=emb)
    assert len(emb.calls) == 1
    rows = await q(p, "select model from embeddings")
    assert rows == [{"model": "test/embed"}]


async def test_embedding_sidecar_right_model_used(p: Any, settings: Settings) -> None:
    side = json.dumps({"model": "test/embed", "dim": DIM, "vector": [2.0] + [0.0] * (DIM - 1)})
    emb = FakeEmbedder()
    await run(drop(settings, "e", png(12), embedding=side), p, settings, embedder=emb)
    assert emb.calls == []
    rows = await q(p, "select vec from embeddings")
    vec = rows[0]["vec"].to_numpy()
    assert vec[0] == pytest.approx(1.0) and np.allclose(vec[1:], 0)


async def test_owner_caption_survives_reprocess(p: Any, settings: Settings) -> None:
    data = png(13)
    desc = FakeDescriber()
    await run(drop(settings, "h", data, caption="Mine"), p, settings, desc)
    assert desc.calls == [{"description", "analysis", "tags"}]
    img = await image(p)
    t = await texts(p, img["id"])
    assert t["caption"]["source"] == "owner" and t["description"]["source"] == "model"

    await run(drop(settings, "h", data), p, settings, desc)
    assert len(desc.calls) == 1
    img = await image(p)
    t = await texts(p, img["id"])
    assert t["caption"]["source"] == "owner" and t["caption"]["body"] == "Mine"
    assert img["slug"] == "mine"


async def test_owner_analysis_change_reembeds(p: Any, settings: Settings) -> None:
    data = png(14)
    emb = FakeEmbedder()
    await run(drop(settings, "d", data), p, settings, embedder=emb)
    before = (await q(p, "select vec from embeddings"))[0]["vec"].to_numpy()

    # The public description is not what the vector is made from.
    await run(drop(settings, "d", data, description="Something new."), p, settings, embedder=emb)
    assert len(emb.calls) == 1

    await run(drop(settings, "d", data, analysis="Subject: new."), p, settings, embedder=emb)
    after = (await q(p, "select vec from embeddings"))[0]["vec"].to_numpy()
    assert emb.calls[-1] == ["Subject: new."] and not np.allclose(before, after)
    # A model caption never renames a live image.
    assert (await image(p))["slug"] == "a-lamp-in-moss"


async def test_haiku_job(p: Any, settings: Settings) -> None:
    await run(drop(settings, "k", png(15)), p, settings)
    img = await image(p)
    seen: list[str] = []

    async def writer(description: str, *, settings: Settings, pool: Any) -> tuple[str, str, str]:
        seen.append(description)
        return "moss holds the lamp", "brass remembers nothing", "evening, still"

    job = Job(id=0, type="haiku", payload={"image_id": img["id"]}, attempts=1, last_error=None)
    await handle_haiku(job, pool=p, settings=settings, writer=writer)
    await handle_haiku(job, pool=p, settings=settings, writer=writer)
    rows = await q(p, "select l1, source, image_id from haikus where image_id = %s", img["id"])
    assert rows == [{"l1": "moss holds the lamp", "source": "model", "image_id": img["id"]}]
    assert seen[0] == DESCRIPTION

    async def flaky(description: str, *, settings: Settings, pool: Any) -> tuple[str, str, str]:
        raise DescribeError("overloaded", retryable=True)

    with pytest.raises(SoftError):
        await handle_haiku(job, pool=p, settings=settings, writer=flaky)
