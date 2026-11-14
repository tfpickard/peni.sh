"""Unpublish and republish: files held and restored, nothing republishes by accident, and
every public route treats an unpublished picture as not here."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from psycopg.types.json import Jsonb

from penish.config import Settings, get_settings
from penish.worker import facet_jobs, unpublish
from penish.worker.describe import DescribeError
from penish.worker.jobs import HardError, Job, SoftError
from tests.test_admin_jobs import Redescriber, crop, live, regen, row
from tests.test_admin_pictures import XHR, add, sql
from tests.test_admin_pictures import client as client  # noqa: F401
from tests.test_api_search import fake_embed
from tests.test_ingest import DIM, FakeDescriber, FakeEmbedder, drop, png, pub_files, q, run

OLD = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


def job(kind: str, image_id: int, id: int = 0) -> Job:
    return Job(id=id, type=kind, payload={"image_id": image_id}, attempts=1, last_error=None)


async def down(p: Any, settings: Settings, image_id: int, id: int = 0) -> None:
    await unpublish.handle_unpublish(
        job(unpublish.UNPUBLISH, image_id, id), pool=p, settings=settings
    )


async def up(p: Any, settings: Settings, image_id: int, id: int = 0) -> None:
    await unpublish.handle_republish(
        job(unpublish.REPUBLISH, image_id, id), pool=p, settings=settings
    )


def held(settings: Settings, sha: str) -> list[str]:
    d = settings.media_held
    return sorted(f.name for f in d.iterdir() if f.name.startswith(sha)) if d.is_dir() else []


async def status(p: Any, image_id: int) -> str:
    return str((await row(p, image_id))["status"])


async def clean_graph(p: Any) -> None:
    async with p.connection() as conn:
        await conn.execute("update mycelium_state set graph_dirty = false")


async def dirty(p: Any) -> bool:
    return bool((await q(p, "select graph_dirty from mycelium_state"))[0]["graph_dirty"])


async def stats(p: Any) -> dict[tuple[str, str], int]:
    rows = await q(p, "select kind, model, n from corpus_stats")
    return {(r["kind"], r["model"]): r["n"] for r in rows}


# -- the jobs ----------------------------------------------------------------


async def test_unpublish_holds_files_and_republish_restores(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    r = await row(p, image_id)
    sha, published_at = r["pub_sha"], r["published_at"]
    shown = pub_files(settings, sha)
    assert shown and not held(settings, sha)
    async with p.connection() as conn:
        # A facet vector and an image vector, so every kind's mean must follow.
        await conn.execute(
            "insert into embeddings (image_id, kind, vec, model, dim)"
            " select image_id, k, vec, m, dim from embeddings,"
            " (values ('facet:mood/split', 'test/embed'), ('image', 'img/model')) v(k, m)"
            " where image_id = %s and kind = 'description'",
            (image_id,),
        )
        await conn.execute(
            "update picture_attention set last_viewed_at = %s where image_id = %s",
            (OLD, image_id),
        )
    await clean_graph(p)

    await down(p, settings, image_id)
    assert await status(p, image_id) == "unpublished"
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown
    assert await dirty(p)
    assert await stats(p) == {
        ("description", "test/embed"): 0,
        ("facet:mood/split", "test/embed"): 0,
        ("image", "img/model"): 0,
    }

    await clean_graph(p)
    async with p.connection() as conn:
        await conn.execute("delete from jobs")
    await up(p, settings, image_id)
    r = await row(p, image_id)
    assert r["status"] == "live" and r["published_at"] == published_at
    assert pub_files(settings, sha) == shown and held(settings, sha) == []
    assert await dirty(p)
    assert set((await stats(p)).values()) == {1}
    seen = await q(p, "select last_viewed_at from picture_attention where image_id = %s", image_id)
    assert seen[0]["last_viewed_at"] > OLD
    queued = await q(p, "select type from jobs order by type")
    # Ingest's own categorize job was dropped above, so nothing is filed yet.
    assert [j["type"] for j in queued] == [
        "categorize",
        facet_jobs.EXTRACT_JOB,
        facet_jobs.SPLIT_JOB,
    ]


async def test_reruns_finish_a_partial_move(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    shown = pub_files(settings, sha)
    # A crash after the status flip and half the moves.
    async with p.connection() as conn:
        await conn.execute("update images set status = 'unpublished' where id = %s", (image_id,))
    settings.media_held.mkdir(parents=True)
    for name in shown[::2]:
        (settings.media_pub / name).rename(settings.media_held / name)
    await down(p, settings, image_id)
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown
    await down(p, settings, image_id)
    assert held(settings, sha) == shown and await status(p, image_id) == "unpublished"

    # A republish that died after moving some files back but before the status flip.
    for name in shown[1::2]:
        (settings.media_held / name).rename(settings.media_pub / name)
    await up(p, settings, image_id)
    assert pub_files(settings, sha) == shown and held(settings, sha) == []
    assert await status(p, image_id) == "live"
    await up(p, settings, image_id)
    assert pub_files(settings, sha) == shown and await status(p, image_id) == "live"


async def test_shared_pub_sha_stays_while_another_is_shown(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    shown = pub_files(settings, sha)
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, pub_sha, width, status) values (%s, %s, 600, 'live')"
            " returning id",
            ("f" * 64, sha),
        )
        twin = int((await cur.fetchone())["id"])

    await down(p, settings, image_id)
    assert await status(p, image_id) == "unpublished"
    assert pub_files(settings, sha) == shown and held(settings, sha) == []

    # Once the twin goes down too nothing shows them, so they are held.
    await down(p, settings, twin)
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown
    await up(p, settings, twin)
    assert pub_files(settings, sha) == shown and await status(p, image_id) == "unpublished"


@pytest.mark.parametrize("twin_status", ["queued", "processing", "pending_enrichment"])
async def test_a_twin_still_in_ingest_keeps_the_files_in_pub(
    p: Any, settings: Settings, twin_status: str
) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    shown = pub_files(settings, sha)
    async with p.connection() as conn:
        await conn.execute(
            "insert into images (sha256, pub_sha, width, status) values (%s, %s, 1200, %s)",
            ("f" * 64, sha, twin_status),
        )
    await down(p, settings, image_id)
    # Its publish gate looks in pub only, so holding the files would strand it in line.
    assert await status(p, image_id) == "unpublished"
    assert pub_files(settings, sha) == shown and held(settings, sha) == []


async def test_status_flip_marks_the_graph_dirty_itself(
    p: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus refresh after the lock marks dirty too; a tick between the two must
    already see the mark the flip committed."""

    async def no_refresh(pool: Any, spaces: Any) -> None:
        return None

    monkeypatch.setattr(unpublish, "_refresh", no_refresh)
    image_id = await live(p, settings)
    await clean_graph(p)
    await down(p, settings, image_id)
    assert await status(p, image_id) == "unpublished" and await dirty(p)
    await clean_graph(p)
    await up(p, settings, image_id)
    assert await status(p, image_id) == "live" and await dirty(p)


async def test_served_ladders_are_recorded(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    r = await row(p, image_id)
    first, width = r["pub_sha"], r["width"]

    async def served() -> list[tuple[str, int]]:
        rows = await q(
            p,
            "select pub_sha, width from served_media where image_id = %s order by created_at",
            image_id,
        )
        return [(x["pub_sha"], x["width"]) for x in rows]

    assert await served() == []
    await down(p, settings, image_id)
    assert await served() == [(first, width)]
    # A crop while down renames the files to URLs never served; the record stays.
    await crop(p, settings, image_id, {"x": 100, "y": 50, "w": 600, "h": 500})
    hidden = await row(p, image_id)
    assert hidden["pub_sha"] != first and await served() == [(first, width)]
    # Back up and cropped while live: the ladder it replaced was served.
    await up(p, settings, image_id)
    await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 400, "h": 400})
    assert await served() == [(first, width), (hidden["pub_sha"], hidden["width"])]
    # A rerun of the unpublish records nothing twice.
    await down(p, settings, image_id)
    await down(p, settings, image_id)
    third = await row(p, image_id)
    assert await served() == [
        (first, width),
        (hidden["pub_sha"], hidden["width"]),
        (third["pub_sha"], third["width"]),
    ]


async def test_newest_press_wins(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    ids = []
    async with p.connection() as conn:
        for kind in (unpublish.UNPUBLISH, unpublish.REPUBLISH):
            cur = await conn.execute(
                "insert into jobs (type, payload, status) values (%s, %s, 'running') returning id",
                (kind, Jsonb({"image_id": image_id})),
            )
            ids.append(int((await cur.fetchone())["id"]))
    # The older unpublish skips itself; a failed newer one would not count.
    await down(p, settings, image_id, id=ids[0])
    assert await status(p, image_id) == "live"
    async with p.connection() as conn:
        await conn.execute("update jobs set status = 'failed' where id = %s", (ids[1],))
    await down(p, settings, image_id, id=ids[0])
    assert await status(p, image_id) == "unpublished"


async def test_quiet_noops_and_bad_payloads(p: Any, settings: Settings) -> None:
    await down(p, settings, 999)
    await up(p, settings, 999)
    with pytest.raises(HardError):
        await unpublish.handle_unpublish(
            Job(id=0, type="unpublish", payload={"image_id": "1"}, attempts=1, last_error=None),
            pool=p,
            settings=settings,
        )
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, status) values (%s, 'queued') returning id", ("e" * 64,)
        )
        queued = int((await cur.fetchone())["id"])
    # Never published: neither direction touches it.
    await down(p, settings, queued)
    await up(p, settings, queued)
    assert await status(p, queued) == "queued"


async def test_republish_refuses_without_media(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    await down(p, settings, image_id)
    for name in held(settings, sha):
        if name.endswith(".webp"):
            (settings.media_held / name).unlink()
    with pytest.raises(HardError, match="media missing"):
        await up(p, settings, image_id)
    assert await status(p, image_id) == "unpublished" and pub_files(settings, sha) == []

    # A re-drop heals the ladder into the holding dir, never into pub.
    await run(drop(settings, "s1", png(1, 1200, 800)), p, settings, FakeDescriber())
    assert await status(p, image_id) == "unpublished" and pub_files(settings, sha) == []
    await up(p, settings, image_id)
    assert await status(p, image_id) == "live" and f"{sha}-480.webp" in pub_files(settings, sha)


async def test_settle_job_drops_files_nobody_uses(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    await down(p, settings, image_id)
    assert held(settings, sha)
    async with p.connection() as conn:
        await conn.execute("delete from images where id = %s", (image_id,))
    settle = Job(id=0, type=unpublish.SETTLE, payload={"pub_sha": sha}, attempts=1, last_error=None)
    await unpublish.handle_settle(settle, pool=p, settings=settings)
    assert held(settings, sha) == [] and pub_files(settings, sha) == []
    with pytest.raises(HardError):
        await unpublish.handle_settle(
            Job(
                id=0,
                type=unpublish.SETTLE,
                payload={"pub_sha": "../x"},
                attempts=1,
                last_error=None,
            ),
            pool=p,
            settings=settings,
        )


# -- nothing republishes by accident ----------------------------------------------


async def test_redrop_leaves_it_unpublished(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    r = await row(p, image_id)
    sha = r["pub_sha"]
    await down(p, settings, image_id)
    shown = held(settings, sha)

    # The same bytes again, with an owner caption that renames it.
    await run(drop(settings, "s1", png(1, 1200, 800), caption="A new name"), p, settings)
    r2 = await row(p, image_id)
    assert r2["status"] == "unpublished" and r2["slug"] == "a-new-name"
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown

    # Pub emptied by hand as well as the holding dir: the heal still goes to held.
    for name in shown:
        (settings.media_held / name).unlink()
    await run(drop(settings, "s1", png(1, 1200, 800)), p, settings)
    assert await status(p, image_id) == "unpublished"
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown
    assert len(await q(p, "select 1 from jobs where type = 'haiku'")) == 1


async def test_failed_reprocess_leaves_it_unpublished(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await down(p, settings, image_id)
    async with p.connection() as conn:
        await conn.execute("delete from image_texts where image_id = %s", (image_id,))
    fail = FakeDescriber(fail=DescribeError("busy", retryable=True))
    with pytest.raises(SoftError):
        await run(drop(settings, "s1", png(1, 1200, 800)), p, settings, fail)
    assert await status(p, image_id) == "unpublished"


async def test_crop_of_unpublished_stays_held(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    old = (await row(p, image_id))["pub_sha"]
    await down(p, settings, image_id)
    await crop(p, settings, image_id, {"x": 100, "y": 50, "w": 600, "h": 500})
    r = await row(p, image_id)
    new = r["pub_sha"]
    assert new != old and r["status"] == "unpublished"
    assert pub_files(settings, new) == [] and f"{new}-480.webp" in held(settings, new)
    assert held(settings, old) == [] and pub_files(settings, old) == []

    await up(p, settings, image_id)
    assert f"{new}-480.webp" in pub_files(settings, new) and held(settings, new) == []


async def test_crop_of_a_live_twin_holds_files_left_to_unpublished(
    p: Any, settings: Settings
) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    shown = pub_files(settings, sha)
    async with p.connection() as conn:
        await conn.execute(
            "insert into images (sha256, pub_sha, width, status)"
            " values (%s, %s, 1200, 'unpublished')",
            ("f" * 64, sha),
        )
    await crop(p, settings, image_id, {"x": 0, "y": 0, "w": 400, "h": 400})
    # Only an unpublished picture uses the old files now, so they leave pub but stay.
    assert pub_files(settings, sha) == [] and held(settings, sha) == shown


async def test_regen_and_reembed_leave_it_unpublished(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    sha = (await row(p, image_id))["pub_sha"]
    await down(p, settings, image_id)
    await regen(p, settings, image_id, ["caption", "description", "tags", "embedding"])
    await regen(p, settings, image_id, ["embedding"], describer=Redescriber())
    assert await status(p, image_id) == "unpublished" and pub_files(settings, sha) == []


async def test_facet_jobs_skip_unpublished(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await down(p, settings, image_id)
    async with p.connection() as conn:
        await conn.execute("delete from image_facets")
    emb = FakeEmbedder()
    await facet_jobs.handle_split(
        job(facet_jobs.SPLIT_JOB, image_id), pool=p, settings=settings, embedder=emb
    )
    assert emb.calls == [] and await q(p, "select 1 from image_facets") == []


# -- public routes ------------------------------------------------------------------


def _down(dsn: str, image_id: int) -> None:
    # What the unpublish job's flip writes, without moving files.
    sql(dsn, "update images set status = 'unpublished' where id = %s", image_id)
    sql(
        dsn,
        "insert into served_media (image_id, pub_sha, width)"
        " select id, pub_sha, width from images where id = %s",
        image_id,
    )


async def test_public_routes_treat_it_as_not_here(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = add(clean_db, 1, vec=[1.0, 0.0, 0.0, 0.0])
    b = add(clean_db, 2, vec=[0.0, 1.0, 0.0, 0.0])
    gone = add(clean_db, 3, vec=[0.9, 0.1, 0.0, 0.0], caption="gone", description="gone")
    sql(clean_db, "insert into slug_aliases (slug, image_id) values ('old-name', %s)", gone)
    for image_id in (a, b, gone):
        sql(
            clean_db,
            "insert into tags (image_id, tag, source) values (%s, 'moss', 'model')",
            image_id,
        )
        sql(
            clean_db,
            "insert into image_categories (image_id, category, source)"
            " values (%s, 'drawing', 'model')",
            image_id,
        )
    sql(
        clean_db,
        "insert into edge_state (layer, src, dst, strength, latched, fatigue, fused, idle)"
        " values ('whole', %s, %s, 0.9, false, 0, false, 0)",
        a,
        gone,
    )
    assert (await client.get("/api/stats")).json()["pictures"] == 3
    assert (await client.get("/api/images/img-1")).json()["hyphae"]
    _down(clean_db, gone)

    for path in ("/api/images/img-3", "/api/images/old-name"):
        r = await client.get(path)
        assert r.status_code == 404 and r.json() == {"detail": "not here"}
    for path in ("/i/img-3", "/i/old-name"):
        for method in ("GET", "HEAD"):
            r = await client.request(method, path)
            assert r.status_code == 404
        assert "gone" not in (await client.get(path)).text
    r = await client.post("/api/images/img-3/view", headers=XHR)
    assert r.status_code == 404
    r = await client.post("/api/images/img-3/vote", json={"value": 1}, headers=XHR)
    assert r.status_code == 404

    def slugs(body: dict[str, Any]) -> set[str]:
        return {i["slug"] for i in body["items"]}

    assert slugs((await client.get("/api/images")).json()) == {"img-1", "img-2"}
    assert slugs((await client.get("/api/images", params={"sort": "oldest"})).json()) == {
        "img-1",
        "img-2",
    }
    by_tag = (await client.get("/api/images", params={"tag": "moss"})).json()
    assert slugs(by_tag) == {"img-1", "img-2"} and by_tag["count"] == 2
    assert (await client.get("/api/tags")).json()["tags"] == [{"tag": "moss", "n": 2}]
    cats = {c["slug"]: c for c in (await client.get("/api/categories")).json()["categories"]}
    assert cats["drawing"]["n"] == 2 and cats["drawing"]["cover"]["slug"] == "img-2"
    assert (await client.get("/api/stats")).json()["pictures"] == 2

    fake_embed(monkeypatch, [1.0, 0.0, 0.0, 0.0])
    found = (await client.get("/api/search", params={"q": "moss"})).json()
    assert "img-3" not in slugs(found)

    detail = (await client.get("/api/images/img-1")).json()
    strips = [n["slug"] for k in ("similar", "dissimilar", "hyphae") for n in detail[k]]
    assert "img-3" not in strips and detail["hyphae"] == []


async def test_media_urls_go_with_the_files(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    p: Any,
) -> None:
    image_id = add(clean_db, 1)
    s = get_settings()
    url = f"/media/pub/{1:064x}-200.webp"
    assert (await client.get(url)).status_code == 200
    await down(p, s, image_id)
    assert (await client.get(url)).status_code == 404
    assert (await client.get("/api/images/img-1")).status_code == 404
    await up(p, s, image_id)
    assert (await client.get(url)).status_code == 200
    assert (await client.get("/api/images/img-1")).status_code == 200


# -- admin ----------------------------------------------------------------------------

URL = "/api/admin/pictures"


async def test_admin_enqueues_and_the_newest_press_wins(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    image_id = add(clean_db, 1)
    r = await client.post(f"{URL}/{image_id}/unpublish", headers=XHR)
    assert r.status_code == 200 and r.json()["queued"] is True
    # A second press collapses into the queued one.
    assert (await client.post(f"{URL}/{image_id}/unpublish", headers=XHR)).json()["queued"] is False
    await client.post(f"{URL}/{image_id}/republish", headers=XHR)
    jobs = sql(clean_db, "select type from jobs order by id")
    assert jobs == [("republish",)]
    await client.post(f"{URL}/{image_id}/unpublish", headers=XHR)
    assert sql(clean_db, "select type from jobs") == [("unpublish",)]

    assert (await client.post(f"{URL}/999/unpublish", headers=XHR)).status_code == 404
    queued = add(clean_db, 2, status="queued")
    r = await client.post(f"{URL}/{queued}/republish", headers=XHR)
    assert r.status_code == 409 and r.json() == {"detail": "not live"}
    assert (await client.post(f"{URL}/{image_id}/unpublish")).status_code == 403


async def test_a_press_behind_running_jobs_still_counts(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    p: Any,
) -> None:
    image_id = add(clean_db, 1)
    s = get_settings()
    payload = json.dumps({"image_id": image_id})
    # An unpublish already past its check, and a republish waiting on the sha lock, both
    # holding the keys presses used to carry.
    ran, waiting = (
        sql(
            clean_db,
            "insert into jobs (type, payload, dedupe_key, status)"
            " values (%s, %s::jsonb, %s, 'running') returning id",
            kind,
            payload,
            f"{kind}:{image_id}",
        )[0][0]
        for kind in (unpublish.UNPUBLISH, unpublish.REPUBLISH)
    )
    await down(p, s, image_id, id=ran)
    assert await status(p, image_id) == "live"
    _down(clean_db, image_id)

    r = (await client.post(f"{URL}/{image_id}/unpublish", headers=XHR)).json()
    assert r["queued"] is True and r["job_id"] > waiting
    # The waiting republish finds the newer press and stands down; the press runs.
    await up(p, s, image_id, id=waiting)
    assert await status(p, image_id) == "unpublished"
    await down(p, s, image_id, id=r["job_id"])
    assert await status(p, image_id) == "unpublished"

    # Pressing republish now withdraws that queued unpublish, and the running republish
    # is the newest job again, so it is the one that acts.
    r2 = (await client.post(f"{URL}/{image_id}/republish", headers=XHR)).json()
    assert r2 == {"job_id": None, "queued": False}
    assert sql(clean_db, "select 1 from jobs where id = %s", r["job_id"]) == []


async def test_admin_list_detail_and_purge(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    image_id = add(clean_db, 1, caption="a lamp")
    add(clean_db, 2)
    sql(clean_db, "insert into slug_aliases (slug, image_id) values ('older', %s)", image_id)
    body = (await client.get(f"{URL}/{image_id}")).json()
    assert body["purge"] is None

    _down(clean_db, image_id)
    items = (await client.get(URL, params={"status": "unpublished"})).json()["items"]
    assert [(i["id"], i["status"]) for i in items] == [(image_id, "unpublished")]
    assert items[0]["thumb"] == f"{URL}/{image_id}/original"
    assert (await client.get(items[0]["thumb"])).status_code == 200

    body = (await client.get(f"{URL}/{image_id}")).json()
    assert body["status"] == "unpublished"
    pub = f"https://t/media/pub/{1:064x}-200"
    assert body["purge"] == [
        "https://t/i/img-1",
        "https://t/i/older",
        f"{pub}.avif",
        f"{pub}.webp",
    ]

    # Cropped while down: the new files were never served, the old ones are still cached.
    sql(
        clean_db, "update images set pub_sha = %s, width = 300 where id = %s", f"{9:064x}", image_id
    )
    assert (await client.get(f"{URL}/{image_id}")).json()["purge"] == body["purge"]
    # A ladder replaced while it was live is cached as well.
    sql(
        clean_db,
        "insert into served_media (image_id, pub_sha, width, created_at)"
        " values (%s, %s, 400, now() + interval '1 minute')",
        image_id,
        f"{8:064x}",
    )
    old = f"https://t/media/pub/{8:064x}"
    assert (await client.get(f"{URL}/{image_id}")).json()["purge"] == [
        *body["purge"],
        f"{old}-400.avif",
        f"{old}-400.webp",
    ]

    # Files another shown picture still serves are not this picture's to purge.
    shared = add(clean_db, 3, pub_n=4)
    add(clean_db, 4)
    _down(clean_db, shared)
    body = (await client.get(f"{URL}/{shared}")).json()
    assert body["purge"] == ["https://t/i/img-3"]


async def test_delete_of_unpublished_hands_held_files_to_the_worker(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    image_id = add(clean_db, 1)
    other = add(clean_db, 2)
    _down(clean_db, image_id)
    assert (await client.delete(f"{URL}/{image_id}", headers=XHR)).status_code == 200
    assert sql(clean_db, "select type, payload->>'pub_sha' from jobs") == [
        (unpublish.SETTLE, f"{1:064x}")
    ]
    # A live picture with files of its own needs nothing from the worker.
    sql(clean_db, "delete from jobs")
    assert (await client.delete(f"{URL}/{other}", headers=XHR)).status_code == 200
    assert sql(clean_db, "select 1 from jobs") == []


# -- migration -----------------------------------------------------------------------


def test_migration_down_keeps_it_off_the_site(clean_db: str) -> None:
    mig = Path(__file__).resolve().parent.parent / "migrations" / "009_unpublished.sql"
    up_sql, down_sql = mig.read_text().split("-- migrate:down")
    a = sql(
        clean_db,
        "insert into images (sha256, status) values (%s, 'unpublished') returning id",
        "a" * 64,
    )[0][0]
    try:
        sql(clean_db, down_sql)
        assert sql(clean_db, "select status::text from images where id = %s", a) == [("failed",)]
        labels = sql(clean_db, "select unnest(enum_range(null::image_status))::text")
        assert [r[0] for r in labels] == [
            "queued",
            "processing",
            "pending_enrichment",
            "live",
            "failed",
        ]
    finally:
        sql(clean_db, up_sql.split("-- migrate:up", 1)[1])
    assert sql(clean_db, "select 'unpublished'::image_status::text") == [("unpublished",)]
