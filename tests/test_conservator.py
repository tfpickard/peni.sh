"""The conservator's note: the `_note.txt` sidecar, the job filing queues, what the model is
told, what it may say, and that an owner note is never replaced."""

# ruff: noqa: F811  -- shared fixtures are imported, then named as parameters.
from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import pytest

from penish import runtime
from penish.collection import store
from penish.config import Settings
from penish.worker import conservator
from penish.worker.conservator import Note, clean, facts, write
from penish.worker.jobs import Job
from tests.test_collections_worker import collection, file, p, settings  # noqa: F401
from tests.test_ingest import drop, png, q, run
from tests.test_nudity import FakeClient

OWNER = "Vellum. Iron gall ink, faded at the gutter."


def note_job(image_id: int, collection_id: int, rewrite: bool = False) -> Job:
    return Job(
        id=0,
        type=store.NOTE_JOB,
        payload={"image_id": image_id, "collection_id": collection_id, "rewrite": rewrite},
        attempts=1,
        last_error=None,
    )


async def filed(p: Any, settings: Settings, seed: int, line: str, **sidecars: Any) -> int:
    """A picture dropped with a collection line (and whatever else), live and filed."""
    raw = sidecars.pop("raw_note", None)
    job = drop(settings, f"leaf{seed}", png(seed), collection=line, **sidecars)
    if raw is not None:
        (Path(job.payload["dir"]) / f"leaf{seed}_note.txt").write_bytes(raw)
    await run(job, p, settings)
    rows = await q(p, "select id, status::text as status from images order by id desc limit 1")
    assert rows and rows[0]["status"] == "live"
    await file(p, settings, rows[0]["id"])
    return int(rows[0]["id"])


async def leaf_note(p: Any, image_id: int) -> tuple[str | None, str | None, str | None]:
    rows = await q(
        p,
        "select note, note_source, note_model from collection_leaves where image_id = %s",
        image_id,
    )
    return rows[0]["note"], rows[0]["note_source"], rows[0]["note_model"]


async def note_jobs(p: Any) -> list[dict[str, Any]]:
    return await q(p, "select payload, dedupe_key from jobs where type = %s", store.NOTE_JOB)


class Writer:
    """Records what the job hands the model; `then` runs before the note is returned."""

    def __init__(self, text: str = "Paper, machine-ruled.", then: Any = None) -> None:
        self.text = text
        self.then = then
        self.seen: list[store.NoteContext] = []

    async def __call__(self, data: bytes, mime: str, ctx: store.NoteContext, **kw: Any) -> Note:
        self.seen.append(ctx)
        if self.then is not None:
            await self.then()
        return Note(self.text, "test/vision")


# -- what it may say ----------------------------------------------------------------


def test_prompt_is_house_style() -> None:
    text = runtime.default("conservator.prompt")
    assert "\u2014" not in text and "the Pansophicon" in text
    # The names that stay unsaid are not even mentioned to the model.
    for word in ("Rak", "Brethren", "Byzantium"):
        assert word not in text


def test_clean_keeps_flat_fact() -> None:
    got = clean({"note": "Greek, probably fifteenth century.  Marginalia in Polish \u2014 later."})
    assert got == "Greek, probably fifteenth century. Marginalia in Polish -- later."
    four = "Paper. Machine-ruled. Folded once. Retained in sequence pending review."
    assert clean({"note": four}) == four


@pytest.mark.parametrize(
    "text",
    [
        "",
        "A curious leaf.",
        "Vellum. Appears to depict a meal.",
        "Interestingly, the ink is brown.",
        "Paper; this suggests a later date.",
        "Marginalia from Raków.",
        "Written for the Polish Brethren.",
        "Made in Byzantium.",
        "One. Two. Three. Four. Five.",
        "Is it vellum?",
        "x" * 700 + ".",
    ],
)
def test_clean_refuses(text: str) -> None:
    with pytest.raises(ValueError):
        clean({"note": text})


def test_clean_refuses_the_wrong_shape() -> None:
    with pytest.raises(jsonschema.ValidationError):
        clean({"text": "Vellum."})


# -- the sidecar ----------------------------------------------------------------------


async def test_note_sidecar_sets_an_owner_note(p: Any, settings: Settings) -> None:
    i = await filed(p, settings, 1, "pansophicon iii 14", note=OWNER + "\n")
    assert await leaf_note(p, i) == (OWNER, "owner", None)
    # The owner wrote it, so the conservator is not asked.
    assert await note_jobs(p) == []
    assert await q(p, "select * from collection_requests") == []


@pytest.mark.parametrize("raw", [b"", b"   \n\n", b"\xff\xfe\xfa not utf-8 \xc3"])
async def test_empty_or_malformed_note_does_not_fail_publish(
    p: Any, settings: Settings, raw: bytes
) -> None:
    i = await filed(p, settings, 2, "pansophicon iii", raw_note=raw)
    assert await leaf_note(p, i) == (None, None, None)
    c = await collection(p)
    # No owner note, so filing asked the conservator for one.
    assert [j["payload"] for j in await note_jobs(p)] == [
        {"image_id": i, "collection_id": c["id"], "rewrite": False}
    ]


async def test_note_without_a_collection_is_dropped(p: Any, settings: Settings) -> None:
    await run(drop(settings, "lonely", png(3), note=OWNER), p, settings)
    assert (await q(p, "select status::text as s from images"))[0]["s"] == "live"
    assert await q(p, "select * from collection_requests") == []


async def test_redrop_keeps_a_note_given_earlier(p: Any, settings: Settings) -> None:
    async with p.connection() as conn:
        c = await collection(p)
        # A request with a note, then the same request again without one.
        await q(
            p,
            "insert into images (sha256, status) values (%s, 'processing') returning id",
            "f" * 64,
        )
        iid = (await q(p, "select id from images"))[0]["id"]
        await store.request(conn, c, iid, 3, None, OWNER)
        await store.request(conn, c, iid, 3, 14)
    rows = await q(p, "select position, note from collection_requests")
    assert rows == [{"position": 14, "note": OWNER}]


# -- the job ----------------------------------------------------------------------


async def test_job_writes_a_model_note(p: Any, settings: Settings) -> None:
    i = await filed(p, settings, 4, "pansophicon iii 14")
    c = await collection(p)
    w = Writer("Ledger paper, machine-ruled. Not original to book iii.")
    await conservator.handle_note(note_job(i, c["id"]), pool=p, settings=settings, writer=w)
    assert await leaf_note(p, i) == (w.text, "model", "test/vision")
    # Asked again without a rewrite, it keeps what it has and does not call the model.
    w2 = Writer("Vellum.")
    await conservator.handle_note(note_job(i, c["id"]), pool=p, settings=settings, writer=w2)
    assert w2.seen == [] and (await leaf_note(p, i))[0] == w.text
    # A rewrite replaces a model note.
    await conservator.handle_note(
        note_job(i, c["id"], rewrite=True), pool=p, settings=settings, writer=w2
    )
    assert await leaf_note(p, i) == ("Vellum.", "model", "test/vision")


async def test_model_never_overwrites_an_owner_note(p: Any, settings: Settings) -> None:
    i = await filed(p, settings, 5, "pansophicon iii 14", note=OWNER)
    c = await collection(p)
    w = Writer()
    await conservator.handle_note(
        note_job(i, c["id"], rewrite=True), pool=p, settings=settings, writer=w
    )
    assert w.seen == [] and await leaf_note(p, i) == (OWNER, "owner", None)
    async with p.connection() as conn:
        assert not await store.store_model_note(conn, c["id"], i, "Paper.", "m", rewrite=True)
        # Nor is one queued for it, asked for or not.
        assert not await store.enqueue_note(conn, i, c["id"], rewrite=True)
        assert await store.enqueue_missing_notes(conn, c["id"]) == 0
    assert await leaf_note(p, i) == (OWNER, "owner", None)


async def test_owner_note_saved_while_the_model_ran_stands(p: Any, settings: Settings) -> None:
    i = await filed(p, settings, 6, "pansophicon iii 14")
    c = await collection(p)

    async def owner_writes() -> None:
        async with p.connection() as conn:
            assert await store.set_note(conn, c["id"], i, OWNER)

    w = Writer("Paper.", then=owner_writes)
    await conservator.handle_note(
        note_job(i, c["id"], rewrite=True), pool=p, settings=settings, writer=w
    )
    assert len(w.seen) == 1
    assert await leaf_note(p, i) == (OWNER, "owner", None)
    # Clearing an owner note hands the leaf back to the conservator.
    async with p.connection() as conn:
        assert await store.set_note(conn, c["id"], i, "   ")
        # The job filing queued is still waiting, so this one folds into it.
        assert await store.enqueue_missing_notes(conn, c["id"]) == 0
    assert await leaf_note(p, i) == (None, None, None)
    keys = [j["dedupe_key"] for j in await note_jobs(p)]
    assert f"{store.NOTE_JOB}:{i}:{c['id']}" in keys


async def test_job_reads_the_record_and_the_books_notes(p: Any, settings: Settings) -> None:
    a = await filed(p, settings, 7, "pansophicon iii 9", note="Vellum. Hand A, brown ink.")
    b = await filed(p, settings, 8, "pansophicon iii 20")
    other = await filed(p, settings, 9, "pansophicon v 3", note="Panel, gesso ground.")
    c = await collection(p)
    # b once sat elsewhere.
    await q(
        p,
        "insert into collection_leaf_history (image_id, collection_id, from_book, from_position,"
        " to_book, to_position, reason) values (%s, %s, 5, 7, 3, 20, 'resort') returning id",
        b,
        c["id"],
    )
    w = Writer()
    await conservator.handle_note(note_job(b, c["id"]), pool=p, settings=settings, writer=w)
    ctx = w.seen[0]
    assert (ctx.book, ctx.position, ctx.leaves_per_book, ctx.previous) == (3, 20, 77, (5, 7))
    assert ctx.book_notes == [(9, "Vellum. Hand A, brown ink.")]
    labels = [label for label, _ in ctx.record]
    assert labels == [
        "extent",
        "languages",
        "support",
        "condition",
        "provenance",
        "exhibited",
        "literature",
    ]
    assert a != other

    # And all of it reaches the model, beside the picture.
    client = FakeClient("not json", '{"note": "Paper, machine-ruled."}')
    got = await write(b"x", "image/webp", ctx, settings=settings, client=client)
    assert got.text == "Paper, machine-ruled." and len(client.calls) == 2
    call = client.calls[0]
    assert call["system"] == runtime.default("conservator.prompt")
    image, told = call["messages"][0]["content"]
    assert image["type"] == "image"
    assert told["text"] == facts(ctx)
    for want in (
        "languages: Greek. Marginalia in Latin and Polish.",
        "provenance:\n  Said to have been at a monastery in Epirus before 1913.",
        "  Private collection, Ioannina, by 1931 (label on a box).",
        "This leaf: book iii, leaf 20 of 77.",
        "Earlier place in the sequence: book v, leaf 7.",
        "leaf 9: Vellum. Hand A, brown ink.",
    ):
        assert want in told["text"]
    # Another book's notes are not this book's business.
    assert "Panel, gesso ground." not in told["text"]


async def test_unfiled_leaf_is_a_quiet_no_op(p: Any, settings: Settings) -> None:
    c = await collection(p)
    w = Writer()
    await conservator.handle_note(note_job(999, c["id"]), pool=p, settings=settings, writer=w)
    assert w.seen == []
