"""Collections against the database: the sidecar through ingest, the filing job, owner moves
and pins, unfiling, re-sorts and the history they leave."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from penish import runtime
from penish.collection import store
from penish.config import Settings
from penish.worker import collection_jobs, unpublish
from penish.worker.jobs import Job, SoftError, claim, enqueue
from tests.test_ingest import DIM, FakeEmbedder, drop, png, q, run

SLUG = "pansophicon"


@pytest.fixture
def p(pool: object) -> Any:
    return pool


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # As test_ingest's, so its drop and run helpers find the same dirs and model.
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


def e(*ix: int) -> list[float]:
    v = np.zeros(DIM)
    for i in ix:
        v[i] = 1.0
    return [float(x) for x in v / np.linalg.norm(v)]


async def collection(p: Any) -> dict[str, Any]:
    async with p.connection() as conn:
        c = await store.collection_by_slug(conn, SLUG)
    assert c is not None
    return c


async def anchors(p: Any, settings: Settings) -> None:
    """Book n's anchor along axis n - 1, so where a vector belongs is obvious."""
    c = await collection(p)
    for n in range(1, 8):
        await q(
            p,
            "update collection_books set anchor_embedding = %s::vector, anchor_model = %s,"
            " anchor_dim = %s where collection_id = %s and number = %s returning number",
            str(e(n - 1)),
            settings.embed_model,
            DIM,
            c["id"],
            n,
        )


async def picture(
    p: Any, settings: Settings, n: int, vec: list[float], status: str = "live"
) -> int:
    rows = await q(
        p,
        "insert into images (sha256, pub_sha, slug, width, height, status, published_at)"
        " values (%s, %s, %s, 10, 10, %s, now()) returning id",
        f"{n:064x}",
        f"{n:064x}",
        f"img-{n}",
        status,
    )
    image_id = int(rows[0]["id"])
    await q(
        p,
        "insert into embeddings (image_id, kind, vec, model, dim)"
        " values (%s, 'description', %s::vector, %s, %s) returning image_id",
        image_id,
        str(vec),
        settings.embed_model,
        DIM,
    )
    return image_id


async def leaves(p: Any) -> dict[int, tuple[int, int, bool, bool]]:
    rows = await q(p, "select image_id, book, position, book_pinned, pinned from collection_leaves")
    return {r["image_id"]: (r["book"], r["position"], r["book_pinned"], r["pinned"]) for r in rows}


async def history(p: Any) -> list[dict[str, Any]]:
    return await q(p, "select * from collection_leaf_history order by id")


def file_job(image_id: int, collection_id: int) -> Job:
    return Job(
        id=0,
        type=store.FILE_JOB,
        payload={"image_id": image_id, "collection_id": collection_id},
        attempts=1,
        last_error=None,
    )


async def file(p: Any, settings: Settings, image_id: int, embedder: Any = None) -> None:
    c = await collection(p)
    await collection_jobs.handle_file(
        file_job(image_id, c["id"]), pool=p, settings=settings, embedder=embedder or FakeEmbedder()
    )


# -- the sidecar ---------------------------------------------------------------------


async def test_sidecar_pins_book_and_leaf(p: Any, settings: Settings) -> None:
    await run(drop(settings, "drolly", png(1), collection="pansophicon iii 14"), p, settings)
    img = (await q(p, "select id, status from images"))[0]
    assert img["status"] == "live"
    reqs = await q(p, "select book, position, error from collection_requests")
    assert reqs == [{"book": 3, "position": 14, "error": None}]
    jobs = await q(p, "select payload from jobs where type = %s", store.FILE_JOB)
    c = await collection(p)
    assert [j["payload"] for j in jobs] == [{"image_id": img["id"], "collection_id": c["id"]}]

    emb = FakeEmbedder()
    await file(p, settings, img["id"], emb)
    assert await leaves(p) == {img["id"]: (3, 14, True, True)}
    assert await q(p, "select * from collection_requests") == []
    h = await history(p)
    assert [(r["from_book"], r["to_book"], r["to_position"], r["reason"]) for r in h] == [
        (None, 3, 14, "owner")
    ]
    # The filing embedded the seven seed anchors on the way.
    assert len(emb.calls) == 1 and len(emb.calls[0]) == 7


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("pansophicon viii", "no book viii"),
        ("pansophicon iii 78", "no leaf 78"),
        ("the whole pansophicon please", "too many words"),
        ("pansophicon iiii", "not a roman numeral"),
        ("bestiary", "no collection 'bestiary'"),
        ("", "empty"),
    ],
)
async def test_malformed_sidecar_does_not_fail_publish(
    p: Any, settings: Settings, line: str, why: str
) -> None:
    await run(drop(settings, "drolly", png(2), collection=line), p, settings)
    img = (await q(p, "select id, status from images"))[0]
    assert img["status"] == "live"
    assert await q(p, "select * from collection_requests") == []
    failed = await q(
        p, "select status::text, payload, last_error from jobs where type = %s", store.FILE_JOB
    )
    assert len(failed) == 1 and failed[0]["status"] == "failed"
    assert failed[0]["payload"]["image_id"] == img["id"]
    assert (
        failed[0]["last_error"].startswith("collection sidecar: ")
        and why in failed[0]["last_error"]
    )


async def test_sidecar_on_a_retried_ingest_fails_once(p: Any, settings: Settings) -> None:
    await run(drop(settings, "a", png(3), collection="pansophicon x"), p, settings)
    await run(drop(settings, "a", png(3), collection="pansophicon x"), p, settings)
    n = await q(p, "select count(*) as n from jobs where status = 'failed'")
    assert n[0]["n"] == 1


async def test_auto_filing_from_a_bare_sidecar(p: Any, settings: Settings) -> None:
    # The ingest wrote the picture's text, so the judge would be asked; these are the
    # scores' tests, and the judge has its own.
    await runtime.put(p, "filer.judge", False)
    await run(drop(settings, "drolly", png(4), collection="pansophicon"), p, settings)
    img = (await q(p, "select id from images"))[0]
    await file(p, settings, img["id"])
    got = await leaves(p)
    book, pos, book_pinned, pinned = got[img["id"]]
    assert 1 <= book <= 7 and pos == 1 and not book_pinned and not pinned
    assert [r["reason"] for r in await history(p)] == ["auto"]


# -- the filing job ------------------------------------------------------------------


async def test_filing_waits_for_publish(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    image_id = await picture(p, settings, 1, e(2), status="pending_enrichment")
    c = await collection(p)
    async with p.connection() as conn:
        await store.request(conn, c, image_id, None, None)
    # Not live: nothing is queued, and a job run anyway waits.
    assert await q(p, "select * from jobs where type = %s", store.FILE_JOB) == []
    with pytest.raises(SoftError):
        await file(p, settings, image_id)
    assert await leaves(p) == {}


async def test_empty_books_file_by_anchor(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    ids = [await picture(p, settings, n, e(n % 7)) for n in range(1, 8)]
    async with p.connection() as conn:
        for i in ids:
            await store.request(conn, c, i, None, None)
    for i in ids:
        await file(p, settings, i)
    got = await leaves(p)
    assert {i: got[i][0] for i in ids} == {
        i: (n % 7) + 1 for n, i in zip(range(1, 8), ids, strict=True)
    }


async def test_full_book_refuses_a_pin_and_keeps_the_request(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    await q(p, "update collections set leaves_per_book = 2 where id = %s returning id", c["id"])
    a, b, x = [await picture(p, settings, n, e(0)) for n in (1, 2, 3)]
    async with p.connection() as conn:
        for i in (a, b):
            await store.request(conn, c, i, 1, None)
    await file(p, settings, a)
    await file(p, settings, b)
    async with p.connection() as conn:
        await store.request(conn, c, x, 1, None)
    await file(p, settings, x)
    assert x not in await leaves(p)
    reqs = await q(p, "select error from collection_requests where image_id = %s", x)
    assert reqs == [{"error": "book i is full"}]
    # Unpinned, it goes to the next book with room instead.
    async with p.connection() as conn:
        await store.request(conn, c, x, None, None)
    await file(p, settings, x)
    assert (await leaves(p))[x][0] == 2


async def test_pin_onto_an_auto_leaf_moves_it_aside(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    async with p.connection() as conn:
        await store.request(conn, c, a, None, None)
    await file(p, settings, a)
    assert (await leaves(p))[a][:2] == (1, 1)
    b = await picture(p, settings, 2, e(0, 1))
    async with p.connection() as conn:
        await store.request(conn, c, b, 1, 1)
    await file(p, settings, b)
    got = await leaves(p)
    assert got[b] == (1, 1, True, True)
    assert got[a][0] == 1 and got[a][1] != 1
    h = [(r["image_id"], r["reason"]) for r in await history(p)]
    assert (b, "owner") in h and h.count((a, "auto")) == 2


async def test_pin_onto_a_pinned_leaf_is_refused(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    a, b = [await picture(p, settings, n, e(0)) for n in (1, 2)]
    async with p.connection() as conn:
        await store.request(conn, c, a, 2, 5)
        await store.request(conn, c, b, 2, 5)
    await file(p, settings, a)
    await file(p, settings, b)
    assert b not in await leaves(p)
    reqs = await q(p, "select error from collection_requests")
    assert reqs == [{"error": "book ii leaf 5 is taken"}]


# -- owner actions, re-sorts and history -------------------------------------------------


async def _filed(p: Any, settings: Settings, vecs: list[list[float]]) -> list[int]:
    await anchors(p, settings)
    c = await collection(p)
    ids = [await picture(p, settings, n + 1, v) for n, v in enumerate(vecs)]
    async with p.connection() as conn:
        for i in ids:
            await store.request(conn, c, i, None, None)
    for i in ids:
        await file(p, settings, i)
    return ids


async def test_owner_move_pins_and_is_recorded(p: Any, settings: Settings) -> None:
    a, b = await _filed(p, settings, [e(0), e(0, 3)])
    before = (await leaves(p))[a]
    c = await collection(p)
    async with p.connection() as conn:
        moves = await store.place(conn, c, a, 5, 3, settings)
    assert (await leaves(p))[a] == (5, 3, True, True)
    assert [m.reason for m in moves if m.image_id == a] == ["owner"]
    last = (await history(p))[-1]
    assert (last["from_book"], last["from_position"], last["to_book"], last["to_position"]) == (
        before[0],
        before[1],
        5,
        3,
    )
    async with p.connection() as conn:
        leaf = await store.leaf_for_image(conn, a)
    # Unpublished: the public never hears of it.
    assert leaf is None
    await q(p, "update collections set published = true returning id")
    async with p.connection() as conn:
        leaf = await store.leaf_for_image(conn, a)
    assert leaf is not None
    assert (leaf["book"], leaf["position"]) == (5, 3)
    assert leaf["previous"] == {"book": before[0], "position": before[1]}
    assert leaf["collection"]["abbrev_latin"] == "PTP"


async def test_previous_is_null_for_a_leaf_that_never_moved(p: Any, settings: Settings) -> None:
    (a,) = await _filed(p, settings, [e(1)])
    await q(p, "update collections set published = true returning id")
    async with p.connection() as conn:
        leaf = await store.leaf_for_image(conn, a)
    assert leaf is not None and leaf["previous"] is None


async def test_unfile_leaves_the_gap_and_the_history(p: Any, settings: Settings) -> None:
    a, b, x = await _filed(p, settings, [e(0), e(0, 1), e(1, 0, 0)])
    got = await leaves(p)
    c = await collection(p)
    async with p.connection() as conn:
        assert await store.unfile(conn, c["id"], b)
        assert not await store.unfile(conn, c["id"], b)
    after = await leaves(p)
    assert b not in after and after[a] == got[a]
    last = (await history(p))[-1]
    assert (last["image_id"], last["to_book"], last["reason"]) == (b, None, "owner")
    # The picture is still on the site.
    assert (await q(p, "select status::text from images where id = %s", b))[0]["status"] == "live"


async def test_unpin_hands_the_leaf_back(p: Any, settings: Settings) -> None:
    a, b = await _filed(p, settings, [e(0), e(0, 1)])
    c = await collection(p)
    async with p.connection() as conn:
        await store.place(conn, c, a, 1, 40, settings)
        await store.set_pin(conn, c, a, False, settings)
    got = await leaves(p)
    assert got[a][2:] == (False, False)
    assert got[a][1] != 40


async def test_resort_respects_pins_and_records_moves(p: Any, settings: Settings) -> None:
    ids = await _filed(p, settings, [e(n % 3) for n in range(9)])
    c = await collection(p)
    async with p.connection() as conn:
        # Wrong books on purpose, one pinned there, one only held to its book.
        await store.place(conn, c, ids[0], 6, 9, settings)
        await store.place(conn, c, ids[1], 6, None, settings)
        await store.place(conn, c, ids[2], 7, None, settings)
        await store.set_pin(conn, c, ids[2], False, settings)
    before = await leaves(p)
    n_hist = len(await history(p))
    async with p.connection() as conn:
        dry = await store.resort(conn, c, settings, dry_run=True)
    assert await leaves(p) == before and len(await history(p)) == n_hist
    async with p.connection() as conn:
        moves = await store.resort(conn, c, settings, dry_run=False)
    assert {(m.image_id, m.after) for m in moves} == {(m.image_id, m.after) for m in dry}
    after = await leaves(p)
    assert after[ids[0]] == (6, 9, True, True)
    assert after[ids[1]][0] == 6
    # The unpinned stray went home to the book its axis anchors.
    assert after[ids[2]][0] == (2 % 3) + 1
    new = (await history(p))[n_hist:]
    assert len(new) == len(moves) and {r["reason"] for r in new} == {"resort"}
    positions = [(b, pos) for b, pos, *_ in after.values()]
    assert len(set(positions)) == len(positions)
    # Twice in a row: the second moves no leaf to another book.
    async with p.connection() as conn:
        again = await store.resort(conn, c, settings, dry_run=False)
    assert all(m.before and m.after and m.before[0] == m.after[0] for m in again)
    assert (await q(p, "select last_resort_at from collections"))[0]["last_resort_at"]


async def test_deleting_a_picture_takes_its_leaf(p: Any, settings: Settings) -> None:
    a, b = await _filed(p, settings, [e(0), e(1)])
    await q(p, "delete from images where id = %s returning id", a)
    assert set(await leaves(p)) == {b}


async def test_worker_start_sweeps(p: Any, settings: Settings) -> None:
    c = await collection(p)
    image_id = await picture(p, settings, 1, e(0))
    await q(
        p,
        "insert into collection_requests (image_id, collection_id) values (%s, %s) returning 1",
        image_id,
        c["id"],
    )
    assert await collection_jobs.enqueue_unfiled(p) == 1
    assert await collection_jobs.enqueue_unfiled(p) == 0
    assert await collection_jobs.enqueue_anchors(p, settings) == 1
    await anchors(p, settings)
    await q(p, "delete from jobs returning 1")
    assert await collection_jobs.enqueue_anchors(p, settings) == 0


async def test_weekly_resort_only_when_asked(p: Any, settings: Settings) -> None:
    assert await collection_jobs.enqueue_weekly(p) == 0
    await q(p, """update collections set tuning = '{"weekly_resort": true}' returning id""")
    assert await collection_jobs.enqueue_weekly(p) == 1
    await q(p, "delete from jobs returning 1")
    await q(p, "update collections set last_resort_at = now() returning id")
    assert await collection_jobs.enqueue_weekly(p) == 0


async def test_anchor_edit_race_keeps_the_new_text(p: Any, settings: Settings) -> None:
    c = await collection(p)
    async with p.connection() as conn:
        stale = await store.stale_anchors(conn, c["id"], settings)
        assert len(stale) == 7
        await conn.execute(
            "update collection_books set anchor_text = 'new' where collection_id = %s"
            " and number = 1",
            (c["id"],),
        )
        stored = await store.store_anchor(
            conn, c["id"], 1, stale[0]["anchor_text"], np.asarray(e(0)), settings
        )
    assert not stored


async def test_republish_files_a_request_made_while_down(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    image_id = await picture(p, settings, 1, e(2), status="unpublished")
    c = await collection(p)
    async with p.connection() as conn:
        await store.request(conn, c, image_id, None, None)
    assert await q(p, "select * from jobs where type = %s", store.FILE_JOB) == []
    # Media present where an unpublished picture keeps it, so the republish goes through.
    settings.media_held.mkdir(parents=True, exist_ok=True)
    for ext in ("avif", "webp"):
        (settings.media_held / f"{1:064x}-10.{ext}").write_bytes(b"x")
    await unpublish.handle_republish(
        Job(
            id=0,
            type=unpublish.REPUBLISH,
            payload={"image_id": image_id},
            attempts=1,
            last_error=None,
        ),
        pool=p,
        settings=settings,
    )
    rows = await q(p, "select payload from jobs where type = %s", store.FILE_JOB)
    assert [r["payload"] for r in rows] == [{"image_id": image_id, "collection_id": c["id"]}]
    await file(p, settings, image_id)
    assert (await leaves(p))[image_id][0] == 3


# -- why a leaf went where it did ------------------------------------------------------


async def test_filing_by_score_keeps_the_scores(p: Any, settings: Settings) -> None:
    a, b = await _filed(p, settings, [e(2), e(2, 4)])
    rows = {r["image_id"]: r for r in await history(p) if r["from_book"] is None}
    got = rows[a]
    assert (got["chosen_by"], got["to_book"]) == ("score", 3)
    s = got["scores"]
    assert (s["best"], s["runner_up"]) == (3, 1)
    assert s["margin"] == pytest.approx(1.0)
    assert s["tuning"]["anchor_k"] == 10.0
    assert [x["number"] for x in s["books"]] == list(range(1, 8))
    for x in s["books"]:
        assert x["cos"] + x["tag"] + x["fill"] == pytest.approx(x["total"])
    # The second saw the first in book iii, and its best is the book it went to.
    assert rows[b]["scores"]["best"] == rows[b]["to_book"]
    # A leaf pushed along inside its book decides no book.
    assert all(r["chosen_by"] is None for r in await history(p) if r["from_book"] is not None)


async def test_a_book_the_owner_gave_is_owner_and_still_scored(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    async with p.connection() as conn:
        await store.request(conn, c, a, 5, None)
    await file(p, settings, a)
    (row,) = await history(p)
    assert (row["chosen_by"], row["to_book"], row["scores"]["best"]) == ("owner", 5, 1)
    # A move of a filed leaf by the owner says so too.
    async with p.connection() as conn:
        await store.place(conn, c, a, 6, None, settings)
    assert (await history(p))[-1]["chosen_by"] == "owner"


async def test_resort_records_the_scores_of_a_book_move(p: Any, settings: Settings) -> None:
    (a,) = await _filed(p, settings, [e(0)])
    c = await collection(p)
    async with p.connection() as conn:
        await store.place(conn, c, a, 4, None, settings)
        await store.set_pin(conn, c, a, False, settings)
        moves = await store.resort(conn, c, settings, dry_run=False)
    assert [(m.image_id, m.after[0] if m.after else None) for m in moves] == [(a, 1)]
    last = (await history(p))[-1]
    assert (last["reason"], last["chosen_by"], last["scores"]["best"]) == ("resort", "score", 1)


# -- the note sweep ------------------------------------------------------------------


async def test_note_sweep(p: Any, settings: Settings) -> None:
    c = await collection(p)
    a, b, m = [await picture(p, settings, n, e(n)) for n in (1, 2, 3)]
    for i, pos in ((a, 1), (b, 2), (m, 3)):
        await q(
            p,
            "insert into collection_leaves (image_id, collection_id, book, position)"
            " values (%s, %s, 1, %s) returning 1",
            i,
            c["id"],
            pos,
        )
    await q(
        p,
        "update collection_leaves set note = 'Vellum.', note_source = 'owner'"
        " where image_id = %s returning 1",
        b,
    )
    await q(
        p,
        "update collection_leaves set note = 'Rag paper.', note_source = 'model',"
        " note_model = 'm' where image_id = %s returning 1",
        m,
    )
    assert await collection_jobs.enqueue_unnoted(p) == 1
    rows = await q(p, "select payload, dedupe_key from jobs where type = %s", store.NOTE_JOB)
    assert rows == [
        {
            "payload": {"image_id": a, "collection_id": c["id"], "rewrite": False},
            "dedupe_key": f"{store.NOTE_JOB}:{a}:{c['id']}",
        }
    ]
    # A restart while it waits queues nothing more.
    assert await collection_jobs.enqueue_unnoted(p) == 0
    # Nor once it has run and the model came back with nothing to keep: that leaf waits
    # for the owner, not for the next restart.
    await q(p, "update jobs set status = 'failed' returning 1")
    assert await collection_jobs.enqueue_unnoted(p) == 0
    # The owner's note and the model's are never touched.
    notes = await q(p, "select image_id, note, note_source from collection_leaves order by 1")
    assert [(r["note"], r["note_source"]) for r in notes] == [
        (None, None),
        ("Vellum.", "owner"),
        ("Rag paper.", "model"),
    ]


async def test_note_sweep_waits_behind_the_queue(p: Any, settings: Settings) -> None:
    """The backlog is due later and spaced out, so work queued meanwhile is claimed first."""
    c = await collection(p)
    ids = [await picture(p, settings, n, e(n)) for n in (1, 2, 3)]
    for pos, i in enumerate(ids, start=1):
        await q(
            p,
            "insert into collection_leaves (image_id, collection_id, book, position)"
            " values (%s, %s, 1, %s) returning 1",
            i,
            c["id"],
            pos,
        )
    assert await collection_jobs.enqueue_unnoted(p) == 3
    rows = await q(
        p,
        "select (payload->>'image_id')::bigint as image_id,"
        " extract(epoch from run_after - now())::float as wait,"
        " extract(epoch from run_after - min(run_after) over ())::float as after"
        " from jobs where type = %s order by run_after",
        store.NOTE_JOB,
    )
    assert [r["image_id"] for r in rows] == ids
    start = collection_jobs.NOTE_SWEEP_START.total_seconds()
    step = collection_jobs.NOTE_SWEEP_STEP.total_seconds()
    assert start - 60 < rows[0]["wait"] <= start
    assert [r["after"] for r in rows] == [0, step, 2 * step]
    # An upload queued now is claimed before any of them.
    queued = await enqueue(p, "ingest", {"path": "x"})
    job = await claim(p)
    assert job is not None and job.id == queued
