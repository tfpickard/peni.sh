"""Re-judging over http: the dry run from kept judgements, applying exactly it, what it
never touches, explain, and the settings on the models page."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from penish.config import get_settings
from penish.worker import collection_jobs
from penish.worker.jobs import Job
from tests import test_admin_pictures
from tests.test_admin_pictures import XHR, add, sql
from tests.test_collections_api import ADM, SLUG, anchored, cid, leaf, places

client = test_admin_pictures.client

REFUSE = "refuse"


class ByCaption:
    """Answers by the leaf's caption, since a whole collection is judged out of order."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        text = kw["messages"][0]["content"][0]["text"]
        caption = text.split("Caption:\n", 1)[1].split("\n", 1)[0]
        self.asked.append(caption)
        answer = self.answers[caption]
        if answer == REFUSE:
            return SimpleNamespace(content=[], stop_reason="refusal", usage=None)
        book, runner_up = answer.split()
        body = (
            f'{{"book": "{book}", "runner_up": "{runner_up}",'
            f' "reason": "The {caption} belongs in {book}."}}'
        )
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)], usage=None)


async def judging(pool: Any, dsn: str, answers: dict[str, str]) -> ByCaption:
    """The worker's job, run here as the queue would run it."""
    fake = ByCaption(answers)
    job = Job(
        id=0,
        type="collection_judge",
        payload={"collection_id": cid(dsn)},
        attempts=1,
        last_error=None,
    )
    await collection_jobs.handle_judge(
        job, pool=pool, settings=get_settings(), embedder=None, client=fake
    )
    return fake


def pictured(dsn: str, n: int, caption: str, vec: list[float]) -> int:
    return add(
        dsn,
        n,
        caption=caption,
        description=f"A {caption}, as the locals keep it.",
        analysis=f"Subject: a {caption}.",
        vec=vec,
    )


async def dry(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{ADM}/{SLUG}/judge", headers=XHR, json={"dry_run": True})


async def apply(client: httpx.AsyncClient, plan: dict[str, Any]) -> httpx.Response:
    body = {"dry_run": False, "plan_id": plan["plan_id"], "moves": plan["moves"]}
    return await client.post(f"{ADM}/{SLUG}/judge", headers=XHR, json=body)


ANSWERS = {
    "zodiac": "iv vii",
    "bath": "vii iii",
    "beast": "i vii",
    "sky": "iv ii",
    "chart": "iv ii",
    "refusing": REFUSE,
    "pinned": "vi vii",
}


def collection_of_seven(dsn: str) -> dict[str, int]:
    """Two leaves to a book. Book iv has room for one more and two are judged into it."""
    sql(dsn, "update collections set leaves_per_book = 2")
    anchored(dsn)
    ids = {}
    for n, (caption, book, pos) in enumerate(
        [
            ("zodiac", 1, 1),
            ("beast", 1, 2),
            ("bath", 3, 1),
            ("sky", 4, 1),
            ("chart", 5, 1),
            ("refusing", 5, 2),
            ("pinned", 2, 1),
        ],
        start=1,
    ):
        ids[caption] = pictured(dsn, n, caption, [0.0, 0.0, 0.0, 1.0])
        leaf(dsn, ids[caption], book, pos)
    sql(dsn, "update collection_leaves set book_pinned = true where image_id = %s", ids["pinned"])
    return ids


async def test_rejudge_plans_and_applies(
    client: httpx.AsyncClient, clean_db: str, pool: Any
) -> None:
    ids = collection_of_seven(clean_db)
    # Nothing judged yet: the worker is asked, once however often the owner presses.
    for _ in range(2):
        r = await dry(client)
        assert (r.status_code, r.json()) == (409, {"detail": "judging"})
    assert sql(clean_db, "select type, payload from jobs") == [
        ("collection_judge", {"collection_id": cid(clean_db)})
    ]
    fake = await judging(pool, clean_db, ANSWERS)
    # The leaf the owner holds to its book is never asked about.
    assert sorted(fake.asked) == sorted(set(ANSWERS) - {"pinned"})

    r = await dry(client)
    assert r.status_code == 200
    plan = r.json()
    assert isinstance(plan["plan_id"], str) and len(plan["plan_id"]) == 64
    by = {m["image_id"]: m for m in plan["moves"]}
    crossing = {i: m for i, m in by.items() if m["before"][0] != m["after"][0]}
    assert {i: m["after"][0] for i, m in crossing.items()} == {ids["zodiac"]: 4, ids["bath"]: 7}
    z = crossing[ids["zodiac"]]
    assert (z["reason"], z["runner_up"]) == ("The zodiac belongs in iv.", 7)
    assert z["before"] == [1, 1] and z["thumb"] and z["slug"] == "img-1"
    # Anything else in the plan only shifts within its book, which no judge decided.
    assert all(m["reason"] is None for i, m in by.items() if i not in crossing)
    held = {h["image_id"]: (h["book"], h["why"]) for h in plan["held"]}
    assert held == {ids["chart"]: (4, "full"), ids["refusing"]: (None, "refused")}
    assert ids["pinned"] not in by and ids["pinned"] not in held
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]
    # The same state plans the same plan, and asks nobody again.
    assert (await dry(client)).json() == plan

    r = await apply(client, plan)
    assert r.status_code == 200
    got = places(clean_db)
    for m in plan["moves"]:
        assert got[m["image_id"]] == tuple(m["after"])
    assert got[ids["pinned"]] == (2, 1) and got[ids["chart"]] == (5, 1)
    hist = sql(
        clean_db,
        "select image_id, reason, chosen_by, scores from collection_leaf_history"
        " where chosen_by is not null order by image_id",
    )
    assert [(i, why, by) for i, why, by, _ in hist] == [
        (ids["zodiac"], "resort", "judge"),
        (ids["bath"], "resort", "judge"),
    ]
    scores = hist[0][3]
    assert scores["judge"] == {
        "model": "claude-haiku-5-5",
        "book": 4,
        "runner_up": 7,
        "reason": "The zodiac belongs in iv.",
    }
    # The weighing stays beside it: every book open to the leaf, the full v not among them.
    assert [b["number"] for b in scores["books"]] == [1, 2, 3, 4, 6, 7]
    # A leaf the judge agreed with has no history; nothing was pinned.
    assert sql(
        clean_db, "select count(*) from collection_leaf_history where image_id = %s", ids["beast"]
    ) == [(0,)]
    assert sql(clean_db, "select count(*) from collection_leaves where book_pinned") == [(1,)]
    # A re-sort by the scores leaves the judge's books where they are.
    r = await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": True})
    for m in r.json()["moves"]:
        if m["image_id"] in (ids["zodiac"], ids["bath"]):
            assert m["before"][0] == m["after"][0]
    # Applied once; the same plan again is for a collection that is no longer there.
    assert (await apply(client, plan)).json() == {"detail": "changed"}


async def test_rejudge_apply_refuses_a_changed_collection(
    client: httpx.AsyncClient, clean_db: str, pool: Any
) -> None:
    ids = collection_of_seven(clean_db)
    await dry(client)
    await judging(pool, clean_db, ANSWERS)

    async def refused_after(change: str, *args: object) -> None:
        plan = (await dry(client)).json()
        assert plan["moves"]
        sql(clean_db, change, *args)
        before = places(clean_db)
        r = await apply(client, plan)
        assert (r.status_code, r.json()) == (409, {"detail": "changed"}), change
        assert places(clean_db) == before

    # A leaf moved inside its book; a picture's text changed; a book retitled.
    await refused_after("update collection_leaves set position = 2 where image_id = %s", ids["sky"])
    sql(clean_db, "update collection_leaves set position = 1 where image_id = %s", ids["sky"])
    await refused_after(
        "update image_texts set body = 'the zodiac, again' where image_id = %s"
        " and field = 'description'",
        ids["zodiac"],
    )
    # The changed text is a new question; the dry run waits for its answer.
    assert (await dry(client)).json() == {"detail": "judging"}
    await judging(pool, clean_db, ANSWERS)
    await refused_after("update collection_books set title = 'Of the Sky' where number = 3")
    await judging(pool, clean_db, ANSWERS)
    # A plan altered on the way back is not the plan that was made.
    plan = (await dry(client)).json()
    plan["moves"][0]["after"] = [6, 2]
    assert (await apply(client, plan)).json() == {"detail": "changed"}
    plan = (await dry(client)).json()
    plan["plan_id"] = "0" * 64
    assert (await apply(client, plan)).json() == {"detail": "changed"}
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]
    r = await client.post(f"{ADM}/{SLUG}/judge", headers=XHR, json={"dry_run": False})
    assert r.status_code == 422


async def test_rejudge_waits_for_anchors(client: httpx.AsyncClient, clean_db: str) -> None:
    collection_of_seven(clean_db)
    sql(clean_db, "update collection_books set anchor_embedding = null where number = 4")
    r = await dry(client)
    assert (r.status_code, r.json()) == (409, {"detail": "anchors"})
    assert [t for (t,) in sql(clean_db, "select type from jobs")] == ["collection_anchor"]


async def test_explain_shows_the_judge(client: httpx.AsyncClient, clean_db: str, pool: Any) -> None:
    ids = collection_of_seven(clean_db)
    await dry(client)
    await judging(pool, clean_db, ANSWERS)
    a = ids["zodiac"]
    sql(
        clean_db,
        "insert into collection_leaf_history (image_id, collection_id, to_book, to_position,"
        " reason, chosen_by, scores) values (%s, %s, 1, 1, 'auto', 'judge', %s)",
        a,
        cid(clean_db),
        '{"best": 1, "books": [], "judge": {"model": "m", "book": 1, "runner_up": 4,'
        ' "reason": "A man."}}',
    )
    got = (await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": a})).json()
    assert got["judged"] is True
    assert got["filed"]["chosen_by"] == "judge"
    assert got["filed"]["scores"]["judge"]["reason"] == "A man."
    assert got["judgement"] == {
        "model": "claude-haiku-5-5",
        "book": 4,
        "runner_up": 7,
        "reason": "The zodiac belongs in iv.",
    }
    got = (await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": ids["pinned"]})).json()
    assert got["judged"] is False and got["judgement"] is None
    got = (await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": ids["refusing"]})).json()
    assert got["judgement"] == {"error": "refused", "model": "claude-haiku-5-5"}


async def test_models_page_lists_the_filer(client: httpx.AsyncClient) -> None:
    got = (await client.get("/api/admin/models")).json()
    assert got["current"]["filer.model"] == got["default"]["filer.model"] == "claude-haiku-5-5"
    assert got["current"]["filer.judge"] is True
    assert got["default"]["filer.prompt"].startswith("You keep the order of the Pansophicon")
    assert "filer" in got["purposes"]
    r = await client.put("/api/admin/models", headers=XHR, json={"key": "filer.judge", "value": 1})
    assert r.status_code == 422
    r = await client.put(
        "/api/admin/models", headers=XHR, json={"key": "filer.judge", "value": False}
    )
    assert r.json() == {"key": "filer.judge", "value": False}
    r = await client.delete("/api/admin/models/filer.judge", headers=XHR)
    assert r.json() == {"key": "filer.judge", "value": True}
