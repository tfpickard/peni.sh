"""The filer's judge: what it is told, what counts as a judgement, filing by it with the
scores to fall back on, its cache, and vector re-sorts that leave its books alone."""

# ruff: noqa: F811  -- shared fixtures are imported, then named as parameters.
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from penish import runtime
from penish.collection import judge, store
from penish.collection.judge import Failed, Judgement, ShownBook, Texts
from penish.config import Settings
from penish.worker import collection_jobs
from penish.worker.jobs import Job
from tests.test_collections_worker import (  # noqa: F401
    anchors,
    collection,
    e,
    file_job,
    history,
    leaves,
    p,
    picture,
    settings,
)
from tests.test_ingest import FakeEmbedder, q

BOOKS = [
    ShownBook(1, None, "Animals and composite creatures presented one at a time."),
    ShownBook(4, "Of the Sky", "The heavens and what they foretell."),
    ShownBook(7, None, "What people are and what they do."),
]
TEXTS = Texts(
    caption="the signs move in",
    description="Each sign of the zodiac has taken a room in the old man.",
    analysis="Subject: an old man whose limbs are labelled with the twelve signs.",
)
GOOD = '{"book": "iv", "runner_up": "vii", "reason": "A man whose body the signs govern."}'
REFUSE = object()


class FakeJudge:
    """Answers in turn: text, an exception to raise, REFUSE, or a coroutine function to
    await. With a pool it notes, at each call, how many advisory locks are held anywhere."""

    def __init__(self, *replies: Any, pool: Any = None) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.locks: list[int] = []
        self.pool = pool
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.pool is not None:
            self.locks.append(await advisory_locks(self.pool))
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        if r is REFUSE:
            return SimpleNamespace(content=[], stop_reason="refusal", usage=None)
        if callable(r):
            return await r()
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=r)],
            usage=SimpleNamespace(input_tokens=300, output_tokens=40),
        )


async def advisory_locks(pool: Any) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*)::int as n from pg_locks where locktype = 'advisory' and granted"
        )
        row = await cur.fetchone()
    return int(row["n"])


def reply(book: str, runner_up: str, reason: str = "A man the signs govern.") -> str:
    return f'{{"book": "{book}", "runner_up": "{runner_up}", "reason": "{reason}"}}'


# -- the prompt and the question -------------------------------------------------------


def test_prompt_holds_the_rules() -> None:
    text = runtime.default("filer.prompt")
    for want in (
        "Never choose by its medium, technique, style or layout.",
        "A woodcut, an engraving, a manuscript page, a chart",
        "a mosaic may belong to any book",
        "What heaven does is vi.",
        "What people do is vii, even when they do it in heaven's name.",
        "What people do in baths is vii, unless the point of the picture is religious;"
        " then it is vi.",
        "runner-up",
        "names the subject",
        "never the medium",
        '{"book": "iv", "runner_up": "vii", "reason": "..."}',
    ):
        assert want in text, want
    assert "\u2014" not in text
    for word in ("invented", "fictional", "imaginary", "made up"):
        assert word not in text.lower()


def test_render_shows_the_books_and_the_three_texts() -> None:
    text = judge.render(TEXTS, BOOKS)
    for want in (
        "book i\nAnimals and composite creatures presented one at a time.",
        "book iv: Of the Sky\nThe heavens and what they foretell.",
        "book vii\nWhat people are and what they do.",
        "Caption:\nthe signs move in",
        "Description:\nEach sign of the zodiac has taken a room in the old man.",
        "Analysis:\nSubject: an old man whose limbs are labelled with the twelve signs.",
    ):
        assert want in text, want
    assert "The leaf goes into one of them" in text
    # Only the books given.
    assert "book ii" not in text and "book v\n" not in text
    # The books come before the leaf, so the leaf is read against them.
    assert text.index("book vii") < text.index("Caption:")
    # The enum offers exactly the books given.
    props = judge.schema(BOOKS)["properties"]
    assert props["book"]["enum"] == ["i", "iv", "vii"] == props["runner_up"]["enum"]


def test_a_missing_text_is_left_out() -> None:
    text = judge.render(Texts(caption="a cup"), BOOKS)
    assert "Caption:\na cup" in text and "Description" not in text and "Analysis" not in text


# -- what counts as a judgement --------------------------------------------------------


def test_check_takes_a_good_answer() -> None:
    assert judge.check(
        {"book": "IV", "runner_up": "vii", "reason": "  A man the  signs govern. "}, BOOKS
    ) == (4, 7, "A man the signs govern.")


def test_check_makes_an_em_dash_two_hyphens() -> None:
    # The answer is paid for; the dash is converted, not a reason to ask again.
    raw = {"book": "iv", "runner_up": "i", "reason": "A man \u2014 governed."}
    assert judge.check(raw, BOOKS) == (4, 1, "A man -- governed.")


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ({"book": "ix", "runner_up": "i", "reason": "x"}, "no book 9"),
        ({"book": "book iv", "runner_up": "i", "reason": "x"}, "no book"),
        # A book that is not among those given.
        ({"book": "ii", "runner_up": "i", "reason": "x"}, "no book 2"),
        ({"book": "iv", "runner_up": "ii", "reason": "x"}, "no book 2"),
        ({"book": "iv", "runner_up": "iv", "reason": "x"}, "the same"),
        ({"book": "iv", "runner_up": "i", "reason": "   "}, "empty reason"),
        ({"book": "iv", "runner_up": "i", "reason": "a" * 201}, "201 characters"),
    ],
)
def test_check_refuses(raw: dict[str, str], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        judge.check(raw, BOOKS)


@pytest.mark.parametrize(
    "bad",
    [
        reply("ix", "i"),
        reply("ii", "i"),
        reply("iv", "iv"),
        reply("iv", "i", ""),
        reply("iv", "i", "a" * 300),
        '{"book": "iv", "runner_up": "i"}',
        '{"book": "iv", "runner_up": "i", "reason": "x", "medium": "woodcut"}',
        "the fourth book",
    ],
)
async def test_ask_turns_a_bad_answer_into_a_failed_judgement(bad: str) -> None:
    v = await judge.ask(TEXTS, BOOKS, model="m", prompt="p", client=FakeJudge(bad))
    assert isinstance(v, Failed) and v.error == "invalid" and v.model == "m"


async def test_ask_sends_one_text_only_call() -> None:
    c = FakeJudge(GOOD)
    v = await judge.ask(TEXTS, BOOKS, model="m", prompt="the prompt", client=c)
    assert v == Judgement(4, 7, "A man whose body the signs govern.", "m")
    (call,) = c.calls
    assert call["model"] == "m" and call["system"] == "the prompt"
    (msg,) = call["messages"]
    assert [b["type"] for b in msg["content"]] == ["text"]
    assert msg["content"][0]["text"] == judge.render(TEXTS, BOOKS)
    assert call["output_config"]["format"]["schema"] == judge.schema(BOOKS)


async def test_ask_never_raises() -> None:
    async def slow() -> Any:
        await asyncio.sleep(5)

    for answer, error in (
        (REFUSE, "refused"),
        (RuntimeError("boom"), "error"),
        (slow, "timeout"),
    ):
        v = await judge.ask(
            TEXTS, BOOKS, model="m", prompt="p", client=FakeJudge(answer), within=0.05
        )
        assert isinstance(v, Failed) and v.error == error, answer


def test_input_hash_names_the_question() -> None:
    h = judge.input_hash("m", "p", TEXTS, BOOKS)
    assert h == judge.input_hash("m", "p", TEXTS, list(reversed(BOOKS)))
    others = [
        judge.input_hash("m2", "p", TEXTS, BOOKS),
        judge.input_hash("m", "p2", TEXTS, BOOKS),
        judge.input_hash("m", "p", Texts(TEXTS.caption, TEXTS.description, "x"), BOOKS),
        judge.input_hash("m", "p", TEXTS, [*BOOKS[:2], ShownBook(7, None, "changed")]),
        judge.input_hash("m", "p", TEXTS, [*BOOKS[:2], ShownBook(7, "Of Man", BOOKS[2].anchor)]),
        judge.input_hash("m", "p", TEXTS, BOOKS[:2]),
    ]
    assert h not in others and len(set(others)) == len(others)


def test_settings_are_listed() -> None:
    assert {"filer.model", "filer.judge", "filer.prompt"} <= set(runtime.KEYS)
    assert runtime.default("filer.model") == "claude-haiku-5-5"
    assert runtime.default("filer.judge") is True
    assert "filer" in runtime.PURPOSES
    assert runtime.validate("filer.judge", False) is False
    for bad in ("off", 0, None):
        with pytest.raises(ValueError):
            runtime.validate("filer.judge", bad)
    assert runtime.validate("filer.model", " claude-sonnet-5-5 ") == "claude-sonnet-5-5"


# -- filing ------------------------------------------------------------------------------


async def texts(p: Any, image_id: int, t: Texts = TEXTS) -> None:
    for field in ("caption", "description", "analysis"):
        await q(
            p,
            "insert into image_texts (image_id, field, body, source, model)"
            " values (%s, %s, %s, 'model', 'm') returning 1",
            image_id,
            field,
            getattr(t, field),
        )


async def requested(p: Any, settings: Settings, n: int, vec: list[float], book: Any = None) -> int:
    """A live picture with text, asked for by the collection; book i by its vector."""
    c = await collection(p)
    image_id = await picture(p, settings, n, vec)
    await texts(p, image_id)
    async with p.connection() as conn:
        await store.request(conn, c, image_id, book, None)
    return image_id


async def file_with(p: Any, settings: Settings, image_id: int, client: Any) -> None:
    c = await collection(p)
    await collection_jobs.handle_file(
        file_job(image_id, c["id"]),
        pool=p,
        settings=settings,
        embedder=FakeEmbedder(),
        client=client,
    )


async def filed_row(p: Any, image_id: int) -> dict[str, Any]:
    rows = [r for r in await history(p) if r["image_id"] == image_id and r["from_book"] is None]
    assert len(rows) == 1
    return rows[0]


async def test_the_judge_chooses_the_book(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    a = await requested(p, settings, 1, e(0))
    client = FakeJudge(GOOD, pool=p)
    await file_with(p, settings, a, client)
    assert (await leaves(p))[a] == (4, 1, False, False)
    row = await filed_row(p, a)
    assert (row["chosen_by"], row["to_book"], row["reason"]) == ("judge", 4, "auto")
    s = row["scores"]
    # The vector would have chosen book i; its weighing is kept beside the judgement.
    assert s["best"] == 1 and len(s["books"]) == 7
    assert s["judge"] == {
        "model": "claude-haiku-5-5",
        "book": 4,
        "runner_up": 7,
        "reason": "A man whose body the signs govern.",
    }
    # The call was made with no lock held anywhere, the filing's included.
    assert client.locks == [0]
    usage = await q(p, "select purpose, model, input_tokens from api_usage")
    assert usage == [{"purpose": "filer", "model": "claude-haiku-5-5", "input_tokens": 300}]
    # Asked what the open books are, every book being empty.
    told = client.calls[0]["messages"][0]["content"][0]["text"]
    assert all(f"book {x}\n" in told for x in ("i", "ii", "iii", "iv", "v", "vi", "vii"))


async def test_the_lock_probe_sees_a_lock(p: Any, settings: Settings) -> None:
    # The probe above would see the filing's lock if the call were made under it.
    c = await collection(p)
    async with p.connection() as conn, conn.transaction():
        await store.lock(conn, c["id"])
        assert await advisory_locks(p) == 1
    assert await advisory_locks(p) == 0


async def test_choose_is_handed_the_judgement(p: Any, settings: Settings) -> None:
    """The filing step takes an answer, never a client: it cannot call a model."""
    await anchors(p, settings)
    c = await collection(p)
    async with p.connection() as conn:
        state = await store.load(conn, c, settings)
    vec = np.asarray(e(0))
    got = store.choose(state, 99, vec, frozenset(), None, Judgement(6, 7, "r", "m"))
    assert (got.book, got.chosen_by, got.scores and got.scores["judge"]["book"]) == (6, "judge", 6)
    got = store.choose(state, 99, vec, frozenset(), None, None)
    assert (got.book, got.chosen_by) == (1, "score") and "judge" not in (got.scores or {})
    got = store.choose(state, 99, vec, frozenset(), 3, Judgement(6, 7, "r", "m"))
    assert (got.book, got.chosen_by) == (3, "owner") and "judge" not in (got.scores or {})


async def slow() -> Any:
    await asyncio.sleep(5)


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (REFUSE, "refused"),
        (RuntimeError("boom"), "error"),
        (reply("iv", "iv"), "invalid"),
        (reply("viii", "i"), "invalid"),
        (slow, "timeout"),
    ],
)
async def test_a_failed_judge_files_by_score(
    p: Any, settings: Settings, answer: Any, error: str
) -> None:
    await anchors(p, settings)
    a = await requested(p, settings, 1, e(0))
    c = await collection(p)
    client = FakeJudge(answer)
    # As handle_file does it, with a short wait so the slow answer times out.
    verdict = await collection_jobs.judge_filing(p, a, c["id"], settings, client, within=0.05)
    async with p.connection() as conn:
        assert await store.file_request(conn, a, c["id"], settings, verdict) == "filed"
    assert (await leaves(p))[a][0] == 1
    row = await filed_row(p, a)
    assert row["chosen_by"] == "score"
    assert row["scores"]["judge"]["error"] == error
    assert row["scores"]["best"] == 1


async def test_a_book_that_fills_meanwhile_files_by_score(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    await q(p, "update collections set leaves_per_book = 1 returning 1")
    a = await requested(p, settings, 1, e(0))
    c = await collection(p)
    verdict = await collection_jobs.judge_filing(p, a, c["id"], settings, FakeJudge(GOOD))
    assert isinstance(verdict, Judgement) and verdict.book == 4
    # Book iv takes a leaf between the answer and the lock.
    other = await picture(p, settings, 2, e(3))
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " values (%s, %s, 4, 1) returning 1",
        other,
        c["id"],
    )
    async with p.connection() as conn:
        assert await store.file_request(conn, a, c["id"], settings, verdict) == "filed"
    row = await filed_row(p, a)
    assert (row["chosen_by"], row["to_book"]) == ("score", 1)
    assert row["scores"]["judge"]["error"] == "full"


async def test_judge_off_is_never_asked(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    await runtime.put(p, "filer.judge", False)
    a = await requested(p, settings, 1, e(0))
    client = FakeJudge()
    await file_with(p, settings, a, client)
    assert client.calls == []
    row = await filed_row(p, a)
    assert (row["chosen_by"], row["to_book"]) == ("score", 1)
    assert row["scores"]["judge"] == {"error": "off"}


async def test_an_owner_book_is_never_judged(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    a = await requested(p, settings, 1, e(0), book=5)
    client = FakeJudge()
    await file_with(p, settings, a, client)
    assert client.calls == []
    row = await filed_row(p, a)
    assert (row["chosen_by"], row["to_book"]) == ("owner", 5)
    assert "judge" not in row["scores"]


async def test_a_picture_with_no_text_files_by_score(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    async with p.connection() as conn:
        await store.request(conn, c, a, None, None)
    client = FakeJudge()
    await file_with(p, settings, a, client)
    assert client.calls == []
    assert (await filed_row(p, a))["scores"]["judge"] == {"error": "no text"}


# -- the cache -----------------------------------------------------------------------


async def test_the_same_question_is_asked_once(p: Any, settings: Settings) -> None:
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    client = FakeJudge(GOOD, reply("i", "iv"), reply("vii", "iv"), REFUSE)

    async def ask(books: list[ShownBook] = BOOKS) -> judge.Verdict:
        return await judge.judged(p, c["id"], a, TEXTS, books, settings=settings, client=client)

    first = await ask()
    assert await ask() == first and len(client.calls) == 1
    # Another anchor text is another question.
    changed = [*BOOKS[:2], ShownBook(7, None, "What people do.")]
    assert (await ask(changed)).book == 1  # type: ignore[union-attr]
    assert await ask(changed) == Judgement(1, 4, "A man the signs govern.", "claude-haiku-5-5")
    assert len(client.calls) == 2
    # So is another model.
    await runtime.put(p, "filer.model", "claude-sonnet-5-5")
    assert (await ask()).book == 7  # type: ignore[union-attr]
    assert client.calls[-1]["model"] == "claude-sonnet-5-5" and len(client.calls) == 3
    # A refusal is kept and not paid for again; a judgement is never replaced.
    await runtime.put(p, "filer.model", "claude-opus-5-5")
    assert await ask() == Failed("refused", "claude-opus-5-5", "model refused (claude-opus-5-5)")
    assert await ask() == Failed("refused", "claude-opus-5-5")
    assert len(client.calls) == 4
    rows = await q(p, "select model, book, error from collection_judgements order by id")
    assert rows == [
        {"model": "claude-haiku-5-5", "book": 4, "error": None},
        {"model": "claude-haiku-5-5", "book": 1, "error": None},
        {"model": "claude-sonnet-5-5", "book": 7, "error": None},
        {"model": "claude-opus-5-5", "book": None, "error": "refused"},
    ]


async def test_a_passing_failure_is_asked_again(p: Any, settings: Settings) -> None:
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    client = FakeJudge(RuntimeError("down"), GOOD, reply("i", "iv"))

    async def ask() -> judge.Verdict:
        return await judge.judged(p, c["id"], a, TEXTS, BOOKS, settings=settings, client=client)

    assert isinstance(await ask(), Failed)
    assert isinstance(await ask(), Judgement)
    assert (await ask()).book == 4  # type: ignore[union-attr]
    assert len(client.calls) == 2
    rows = await q(p, "select book, error from collection_judgements")
    assert rows == [{"book": 4, "error": None}]


# -- re-sorts keep the judge's books ---------------------------------------------------


async def test_a_resort_keeps_a_judged_book(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    judged = await requested(p, settings, 1, e(0))
    await file_with(p, settings, judged, FakeJudge(GOOD))
    # A leaf the scores chose, sitting in the wrong book.
    stray = await requested(p, settings, 2, e(1))
    await runtime.put(p, "filer.judge", False)
    await file_with(p, settings, stray, FakeJudge())
    async with p.connection() as conn:
        await store.place(conn, c, stray, 6, None, settings)
        await store.set_pin(conn, c, stray, False, settings)
    assert (await leaves(p))[judged] == (4, 1, False, False)

    async with p.connection() as conn:
        state = await store.load(conn, c, settings)
        plan = await store.plan(conn, c, settings)
    assert [x.judged for x in state.leaves] == [True, False]
    assert {m.image_id: m.after[0] for m in plan.moves if m.after} == {stray: 2}
    # A plan that tries anyway is refused.
    moved = [(judged, (4, 1), (1, 1))]
    async with p.connection() as conn:
        given = store.plan_id(await store._digest(conn, c), moved)
        with pytest.raises(store.Refused, match="bad plan"):
            await store.apply(conn, c, settings, given, moved)
        await store.resort(conn, c, settings, dry_run=False)
    got = await leaves(p)
    assert got[judged][0] == 4 and got[stray][0] == 2
    # Not pinned: the owner's view of pins is the owner's.
    assert got[judged][2:] == (False, False)


async def test_the_judge_job_asks_once_per_leaf(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    b = await picture(p, settings, 2, e(1))
    mute = await picture(p, settings, 3, e(2))
    held = await picture(p, settings, 4, e(3))
    for i in (a, b, held):
        await texts(p, i)
    for i, pos in ((a, 1), (b, 2), (mute, 3), (held, 4)):
        await q(
            p,
            "insert into collection_leaves (image_id, collection_id, book, position, book_pinned)"
            " values (%s, %s, 1, %s, %s) returning 1",
            i,
            c["id"],
            pos,
            i == held,
        )
    job = Job(
        id=0,
        type=store.JUDGE_JOB,
        payload={"collection_id": c["id"]},
        attempts=1,
        last_error=None,
    )
    client = FakeJudge(GOOD, GOOD, pool=p)
    for _ in range(2):
        await collection_jobs.handle_judge(
            job, pool=p, settings=settings, embedder=FakeEmbedder(), client=client
        )
    # The leaf with no text and the one the owner holds to its book are never asked.
    assert len(client.calls) == 2 and client.locks == [0, 0]
    rows = await q(p, "select image_id from collection_judgements order by image_id")
    assert [r["image_id"] for r in rows] == [a, b]


async def test_the_judge_job_asks_nothing_when_off(p: Any, settings: Settings) -> None:
    """Queued while the judge was on, run after it was turned off: nothing is paid for."""
    await anchors(p, settings)
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    await texts(p, a)
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " values (%s, %s, 1, 1) returning 1",
        a,
        c["id"],
    )
    await runtime.put(p, "filer.judge", False)
    job = Job(
        id=0,
        type=store.JUDGE_JOB,
        payload={"collection_id": c["id"]},
        attempts=1,
        last_error=None,
    )
    client = FakeJudge()
    await collection_jobs.handle_judge(
        job, pool=p, settings=settings, embedder=FakeEmbedder(), client=client
    )
    assert client.calls == []
    assert await q(p, "select id from collection_judgements") == []


# -- fullness is the filing's business, not the question's ------------------------------


async def test_a_book_filling_leaves_every_question_alone(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    await q(p, "update collections set leaves_per_book = 1 returning 1")
    c = await collection(p)
    a = await requested(p, settings, 1, e(0))
    filed = await picture(p, settings, 2, e(1))
    await texts(p, filed)
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " values (%s, %s, 1, 1) returning 1",
        filed,
        c["id"],
    )

    async def hashes() -> tuple[str, str]:
        async with p.connection() as conn:
            inputs = await store.filing_inputs(conn, a, c["id"], settings)
            assert inputs is not None
            asks, _ = await store.questions(conn, await store.load(conn, c, settings))
        (ask,) = asks
        assert len(inputs[1]) == len(ask.books) == 7
        return (
            judge.input_hash("m", "p", *inputs),
            judge.input_hash("m", "p", ask.texts, ask.books),
        )

    before = await hashes()
    # Book iv fills, then empties again.
    other = await picture(p, settings, 3, e(3))
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " values (%s, %s, 4, 1) returning 1",
        other,
        c["id"],
    )
    assert await hashes() == before
    await q(p, "delete from collection_leaves where image_id = %s returning 1", other)
    assert await hashes() == before


async def test_a_full_book_is_shown_and_its_judgement_falls_back(
    p: Any, settings: Settings
) -> None:
    await anchors(p, settings)
    await q(p, "update collections set leaves_per_book = 1 returning 1")
    c = await collection(p)
    other = await picture(p, settings, 2, e(3))
    await q(
        p,
        "insert into collection_leaves (image_id, collection_id, book, position)"
        " values (%s, %s, 4, 1) returning 1",
        other,
        c["id"],
    )
    a = await requested(p, settings, 1, e(0))
    client = FakeJudge(GOOD)
    await file_with(p, settings, a, client)
    told = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "book iv" in told
    assert (await leaves(p))[a][0] == 1
    row = await filed_row(p, a)
    assert (row["chosen_by"], row["to_book"]) == ("score", 1)
    assert row["scores"]["judge"]["error"] == "full"


async def test_rejudging_holds_a_leaf_judged_into_a_full_book(p: Any, settings: Settings) -> None:
    await anchors(p, settings)
    await q(p, "update collections set leaves_per_book = 1 returning 1")
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    b = await picture(p, settings, 2, e(3))
    for i, book in ((a, 1), (b, 4)):
        await texts(p, i)
        await q(
            p,
            "insert into collection_leaves (image_id, collection_id, book, position)"
            " values (%s, %s, %s, 1) returning 1",
            i,
            c["id"],
            book,
        )
    job = Job(
        id=0,
        type=store.JUDGE_JOB,
        payload={"collection_id": c["id"]},
        attempts=1,
        last_error=None,
    )
    client = FakeJudge(GOOD, GOOD)
    await collection_jobs.handle_judge(
        job, pool=p, settings=settings, embedder=FakeEmbedder(), client=client
    )
    assert all("book iv" in x["messages"][0]["content"][0]["text"] for x in client.calls)
    async with p.connection() as conn:
        plan = await store.judge_plan(conn, c, settings, runtime.getter(p, settings))
    assert [(h.image_id, h.before, h.book, h.why) for h in plan.held] == [(a, (1, 1), 4, "full")]
    assert all(m.image_id != a or m.after == (1, 1) for m in plan.moves)


# -- the guard against paying from a test ------------------------------------------------


async def test_the_guard_notes_a_real_call(
    p: Any, settings: Settings, request: pytest.FixtureRequest
) -> None:
    """The judge swallows the guard's exception, so the guard counts calls instead."""
    calls = request.getfixturevalue("_no_real_judge")
    c = await collection(p)
    a = await picture(p, settings, 1, e(0))
    v = await judge.judged(p, c["id"], a, TEXTS, BOOKS, settings=settings)
    assert isinstance(v, Failed) and v.error == "error"
    assert calls == ["claude-haiku-5-5"]
    # Cleared, or this test would fail at teardown as the guard means any other to.
    calls.clear()
