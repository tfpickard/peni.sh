"""Naming: the switch, the rate, the owner's title, the haiku filed for a gallery.

Colony rows are written by hand (as in test_galleries_api) and the model is a FakeClient,
so every call the naming job makes is counted.
"""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import jsonschema
import pytest

from penish import runtime
from penish.config import get_settings
from penish.mycelium import store
from penish.worker import admin_jobs, grow, haiku, name_jobs
from penish.worker.jobs import Job, SoftError
from tests.test_admin import XHR
from tests.test_admin_mycelium import URL, anon, app, client, post  # noqa: F401
from tests.test_galleries_api import START, colony, public, ticks
from tests.test_mycelium_tick import DIM, MODEL, add, sql


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=None)


def answer(title: str = "jars on a shelf", lines: tuple[str, ...] = ("a", "b", "c")) -> str:
    return json.dumps({"title": title, "haiku": list(lines)})


def namer(fake: FakeClient) -> Any:
    return functools.partial(name_jobs.name_gallery, client=fake)


def pic(
    dsn: str,
    n: int,
    *,
    caption: str | None = None,
    tags: tuple[str, ...] = (),
    status: str = "live",
) -> int:
    image = add(dsn, n, status=status)
    if caption:
        sql(
            dsn,
            "insert into image_texts (image_id, field, body, source, model)"
            " values (%s, 'caption', %s, 'model', 'm')",
            image,
            caption,
        )
    for t in tags:
        sql(
            dsn,
            "insert into tags (image_id, tag, source, model) values (%s, %s, 'model', 'm')",
            image,
            t,
        )
    return image


def need(dsn: str, image: int, phrase: str, satisfaction: float) -> None:
    sql(
        dsn,
        "insert into offers_needs (image_id, role, phrase, vec, model, dim, recipe, satisfaction)"
        " values (%s, 'need', %s, %s::vector, %s, %s, 'extract', %s)",
        image,
        phrase,
        str([1.0] + [0.0] * (DIM - 1)),
        MODEL,
        DIM,
        satisfaction,
    )


def gallery(
    dsn: str, cid: int, serial: int, *, fruited: int = 1, state: str = "alive", **kw: Any
) -> list[int]:
    """A gallery of three captioned live pictures."""
    ids = [pic(dsn, cid * 10 + k, caption=f"a jar {cid}{k}", tags=("jar",)) for k in range(3)]
    colony(dsn, cid, serial, state, members=tuple(ids), founders=tuple(ids), fruited=fruited, **kw)
    return ids


def job(colony_id: int, regenerate: bool = False) -> Job:
    payload: dict[str, Any] = {"colony_id": colony_id}
    if regenerate:
        payload["regenerate"] = True
    return Job(id=0, type=name_jobs.JOB, payload=payload, attempts=1, last_error=None)


def naming(dsn: str, on: bool) -> None:
    sql(dsn, "update mycelium_state set naming = %s where id = 1", on)


def row(dsn: str, cid: int) -> tuple[Any, ...]:
    return sql(dsn, "select title, title_source from colonies where id = %s", cid)[0]


def gallery_haikus(dsn: str, cid: int) -> list[tuple[Any, ...]]:
    return sql(
        dsn,
        "select l1, l2, l3, source::text, image_id, active from haikus where colony_id = %s"
        " order by id",
        cid,
    )


def queued(dsn: str) -> list[int]:
    rows = sql(
        dsn,
        "select (payload->>'colony_id')::bigint from jobs where type = %s and status = 'queued'"
        " order by id",
        name_jobs.JOB,
    )
    return [int(r[0]) for r in rows]


async def run_queued(p: Any, fake: FakeClient) -> None:
    """Run every queued naming job as the job loop would, then mark it done."""
    rows = sql(
        get_settings().database_url,
        "select id, payload from jobs where type = %s and status = 'queued' order by id",
        name_jobs.JOB,
    )
    for jid, payload in rows:
        j = Job(id=jid, type=name_jobs.JOB, payload=payload, attempts=1, last_error=None)
        await name_jobs.handle_name(j, pool=p, settings=get_settings(), namer=namer(fake))
        sql(get_settings().database_url, "update jobs set status = 'done' where id = %s", jid)


# -- the answer ------------------------------------------------------------------


def test_validate_cleans_the_answer() -> None:
    title, lines = name_jobs.validate(
        {"title": ' "Jars \u2014 On A  Shelf." ', "haiku": ["a  jar\u2014lid", " b ", "c"]}
    )
    assert title == "jars -- on a shelf"
    assert lines == ("a jar--lid", "b", "c")
    assert "\u2014" not in title + "".join(lines)


@pytest.mark.parametrize(
    "raw",
    [
        {"title": "", "haiku": ["a", "b", "c"]},
        {"title": '""', "haiku": ["a", "b", "c"]},
        {"title": "x" * 61, "haiku": ["a", "b", "c"]},
        {"title": "one two three four five six seven eight nine", "haiku": ["a", "b", "c"]},
        {"title": "jars", "haiku": ["a", "b"]},
        {"title": "jars", "haiku": ["a", "b", "c", "d"]},
        {"title": "jars", "haiku": ["a", "", "c"]},
        {"title": "jars", "haiku": ["a", "b", "x" * 61]},
        {"title": "jars"},
        {"title": "jars", "haiku": ["a", "b", "c"], "note": "x"},
        {"title": 3, "haiku": ["a", "b", "c"]},
    ],
)
def test_validate_refuses(raw: Any) -> None:
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        name_jobs.validate(raw)


def test_validate_refuses_an_answer_that_repeats_a_need() -> None:
    needs = ["something warm to hold"]
    with pytest.raises(ValueError, match="need"):
        name_jobs.validate({"title": "jars", "haiku": ["a", "Something Warm To Hold", "c"]}, needs)
    # Words of it are fine; the phrase itself is what may not reach a page.
    name_jobs.validate({"title": "warm jars", "haiku": ["a", "b", "c"]}, needs)


def test_prompt_is_in_house_voice() -> None:
    prompt = runtime.default("name.prompt")
    assert "\u2014" not in prompt
    assert "deadpan" in prompt and "{examples}" in prompt
    assert '"title"' in prompt and '"haiku"' in prompt


def test_the_job_is_registered() -> None:
    assert name_jobs.JOB in admin_jobs.HANDLERS


# -- inputs and the call ----------------------------------------------------------


async def test_one_call_from_live_captions_shared_tags_and_fed_needs(app: Any) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    a = pic(dsn, 1, caption="a jar of teeth", tags=("jar", "shelf"))
    b = pic(dsn, 2, caption="a jar of buttons", tags=("jar", "glass"))
    hidden = pic(dsn, 3, caption="a hidden jar", tags=("glass", "shelf"), status="unpublished")
    bare = pic(dsn, 4)
    need(dsn, a, "something to hold", 0.4)
    need(dsn, b, "a lid", 0.0)
    need(dsn, hidden, "a secret drawer", 0.9)
    colony(dsn, 1, 1, "alive", members=(a, b, hidden, bare))

    async with p.connection() as conn:
        r = await name_jobs._colony(conn, 1)
        assert r is not None
        inputs = await name_jobs.gather(conn, r, settings=get_settings())
    # Unpublished pictures say nothing: not their caption, tags or needs.
    assert inputs.captions == ["a jar of teeth", "a jar of buttons"]
    assert inputs.tags == ["jar"]
    assert inputs.needs == ["something to hold"]

    naming(dsn, True)
    fake = FakeClient(answer(lines=("a", "b")), answer())
    named = await name_jobs.name_gallery(inputs, settings=get_settings(), pool=p, client=fake)
    assert named.title == "jars on a shelf" and named.haiku == ("a", "b", "c")
    assert len(fake.calls) == 2, "one retry on a bad answer"
    call = fake.calls[0]
    assert call["model"] == await runtime.get(p, "name.model")
    assert call["output_config"]["format"]["schema"] == name_jobs.SCHEMA
    assert "{examples}" not in call["system"]
    text = call["messages"][0]["content"]
    for want in ("a jar of teeth", "a jar of buttons", "jar", "something to hold"):
        assert want in text
    for never in ("a hidden jar", "a secret drawer", "a lid", "glass"):
        assert never not in text
    assert sql(dsn, "select purpose from api_usage") == [("name",), ("name",)]


async def test_an_answer_repeating_a_fed_need_is_never_stored(app: Any) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    ids = gallery(dsn, 1, 1)
    need(dsn, ids[0], "something to hold", 0.4)
    naming(dsn, True)

    # Both answers repeat it: the job fails and writes nothing.
    fake = FakeClient(
        answer("Something To Hold"), answer(lines=("a", "something to hold here", "c"))
    )
    with pytest.raises(SoftError, match="need"):
        await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=namer(fake))
    assert len(fake.calls) == 2
    assert row(dsn, 1) == (None, None) and gallery_haikus(dsn, 1) == []

    # The first repeats it, the retry does not: only the retry is stored.
    fake = FakeClient(answer("something to hold"), answer("jars", ("a", "b", "c")))
    await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=namer(fake))
    assert len(fake.calls) == 2
    assert row(dsn, 1) == ("jars", "model")
    stored = " ".join(str(x) for h in gallery_haikus(dsn, 1) for x in h[:3])
    assert "something to hold" not in stored


async def test_daily_cap_stops_the_call(app: Any) -> None:  # noqa: F811
    p = app.state.pool
    naming(get_settings().database_url, True)
    await runtime.put(p, "mycelium.daily_calls", 0)
    fake = FakeClient(answer())
    inputs = name_jobs.Inputs(captions=["a jar"], tags=[], needs=[])
    with pytest.raises(SoftError) as e:
        await name_jobs.name_gallery(inputs, settings=get_settings(), pool=p, client=fake)
    assert e.value.run_after is not None
    assert fake.calls == []


# -- the switch -------------------------------------------------------------------


async def test_switch_gates_every_call(app: Any, client: httpx.AsyncClient) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    gallery(dsn, 1, 1)
    fake = FakeClient()

    assert await name_jobs.schedule(p) == 0
    assert queued(dsn) == []
    await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=namer(fake))
    await name_jobs.handle_name(job(1, True), pool=p, settings=get_settings(), namer=namer(fake))
    with pytest.raises(name_jobs.NamingOff):
        await name_jobs.regenerate(p, 1)
    r = await post(client, "galleries/1/regenerate", {})
    assert r.status_code == 400 and r.json()["detail"] == "naming is off"
    assert fake.calls == [] and queued(dsn) == []

    # Switched off while the call was out: the answer is thrown away.
    naming(dsn, True)

    async def switched_off(*args: Any, **kw: Any) -> name_jobs.Named:
        naming(dsn, False)
        return name_jobs.Named(title="jars", haiku=("a", "b", "c"), model="m")

    await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=switched_off)
    assert row(dsn, 1) == (None, None)
    assert gallery_haikus(dsn, 1) == []
    assert sql(dsn, "select count(*) from colony_events where kind = 'named'") == [(0,)]


async def test_switch_off_during_a_call_stops_the_retry(app: Any) -> None:  # noqa: F811
    """The first answer is bad and the switch went off while it was out: the retry would
    be a new paid call, so it is not made."""
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    gallery(dsn, 1, 1)
    naming(dsn, True)

    class OffAfterOne(FakeClient):
        async def create(self, **kw: Any) -> Any:
            got = await super().create(**kw)
            naming(dsn, False)
            return got

    fake = OffAfterOne(answer(lines=("a", "b")), answer())
    await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=namer(fake))
    assert len(fake.calls) == 1
    assert sql(dsn, "select count(*) from api_usage") == [(1,)]
    assert row(dsn, 1) == (None, None)
    assert gallery_haikus(dsn, 1) == []


async def test_admin_switch_queues_what_the_rate_allows(client: httpx.AsyncClient) -> None:  # noqa: F811
    dsn = get_settings().database_url
    public(dsn)
    gallery(dsn, 1, 1)
    r = await post(client, "naming", {"on": True})
    assert r.status_code == 200
    body = r.json()["naming"]
    assert body["on"] and body["queued"] == 1
    assert body["galleries"][0]["serial"] == 1 and body["galleries"][0]["queued"]
    r = await post(client, "naming", {"on": False})
    assert r.json()["naming"]["on"] is False
    assert (await post(client, "naming", {"on": "maybe"})).status_code == 422


# -- the rate ---------------------------------------------------------------------


async def test_rate_per_day_and_batch(app: Any) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    for cid in range(1, 6):
        # Serial order is the reverse of fruiting order, so the oldest fruited goes first.
        gallery(dsn, cid, 6 - cid, fruited=cid)
    gallery(dsn, 6, 6, state="dead", died=9, cause="depopulated")
    colony(dsn, 7, 7, "alive", members=(pic(dsn, 99),))  # nothing to say about it
    # Only an unpublished picture has a caption: nothing it may say either.
    colony(dsn, 8, 8, "alive", members=(pic(dsn, 98, caption="x", status="unpublished"),))
    async with p.connection() as conn:
        await store.set_tunables(conn, {"name_per_day": 3, "name_batch": 2}, "t")
    naming(dsn, True)

    # Before galleries are public a genesis may be run again; nothing is named.
    assert await name_jobs.schedule(p) == 0
    public(dsn)

    assert await name_jobs.schedule(p) == 2
    assert queued(dsn) == [1, 2]
    assert await name_jobs.schedule(p) == 0, "the batch is full"

    fake = FakeClient(*(answer(f"jars {k}", (f"line {k}", "b", "c")) for k in range(10)))
    await run_queued(p, fake)
    assert len(fake.calls) == 2
    async with p.connection() as conn:
        assert await name_jobs.named_today(conn) == 2
    assert await name_jobs.schedule(p) == 1, "3 a day, 2 used"
    assert queued(dsn) == [3]
    await run_queued(p, fake)
    assert await name_jobs.schedule(p) == 0, "the day's names are used"

    tomorrow = datetime.now(UTC) + timedelta(days=1)
    assert await name_jobs.schedule(p, now=tomorrow) == 2
    # The dead gallery and the one with no caption are never queued automatically.
    assert queued(dsn) == [4, 5]
    assert [row(dsn, c)[1] for c in (1, 2, 3)] == ["model"] * 3

    async with p.connection() as conn:
        await store.set_tunables(conn, {"name_per_day": 0}, "t")
    sql(dsn, "delete from jobs")
    assert await name_jobs.schedule(p, now=tomorrow) == 0


async def test_a_recently_failed_gallery_is_left_alone_a_day(app: Any) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    gallery(dsn, 1, 1, fruited=1)
    gallery(dsn, 2, 2, fruited=2)
    for cid, ago in ((1, "1 hour"), (2, "2 days")):
        sql(
            dsn,
            "insert into jobs (type, payload, status, dedupe_key, updated_at)"
            " values (%s, %s, 'failed', %s, now() - %s::interval)",
            name_jobs.JOB,
            json.dumps({"colony_id": cid}),
            f"{name_jobs.JOB}:{cid}",
            ago,
        )
    naming(dsn, True)
    assert await name_jobs.schedule(p) == 1
    assert queued(dsn) == [2]


# -- titles -----------------------------------------------------------------------


async def test_owner_title_is_never_overwritten(
    app: Any,  # noqa: F811
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    ticks(dsn, 3, START)
    gallery(dsn, 1, 1)
    naming(dsn, True)

    r = await post(client, "galleries/1/title", {"title": "Jars I Keep \u2014 Mostly"})
    assert r.status_code == 200
    assert row(dsn, 1) == ("Jars I Keep -- Mostly", "owner")
    gal = next(g for g in r.json()["naming"]["galleries"] if g["serial"] == 1)
    assert gal["title_source"] == "owner"
    assert (await client.get("/api/galleries/1")).json()["title"] == "Jars I Keep -- Mostly"
    assert await name_jobs.schedule(p) == 0

    # A regenerate files a new haiku and leaves the owner's title.
    r = await post(client, "galleries/1/regenerate", {})
    assert r.status_code == 200 and queued(dsn) == [1]
    await run_queued(p, FakeClient(answer("model jars")))
    assert row(dsn, 1) == ("Jars I Keep -- Mostly", "owner")
    assert len(gallery_haikus(dsn, 1)) == 1

    # The owner titles it while an automatic call is out: the owner wins.
    assert (await post(client, "galleries/1/title", {"title": ""})).status_code == 200
    assert row(dsn, 1) == (None, None)
    assert (await client.get("/api/galleries/1")).json()["title"] == "gallery 1"

    async def owner_meanwhile(*args: Any, **kw: Any) -> name_jobs.Named:
        async with p.connection() as conn:
            await name_jobs.set_title(conn, 1, "mine")
        return name_jobs.Named(title="theirs", haiku=("x", "y", "z"), model="m")

    await name_jobs.handle_name(job(1), pool=p, settings=get_settings(), namer=owner_meanwhile)
    assert row(dsn, 1) == ("mine", "owner")

    # Cleared, a regenerate gives it back to the model.
    await post(client, "galleries/1/title", {"title": "  "})
    await post(client, "galleries/1/regenerate", {})
    await run_queued(p, FakeClient(answer("Model Jars")))
    assert row(dsn, 1) == ("model jars", "model")
    assert (await client.get("/api/galleries/1")).json()["title"] == "model jars"
    # A model title is overwritten by a regenerate, unlike the owner's.
    await post(client, "galleries/1/regenerate", {})
    await run_queued(p, FakeClient(answer("other jars")))
    assert row(dsn, 1) == ("other jars", "model")


async def test_title_and_regenerate_refusals(
    app: Any,  # noqa: F811
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    dsn = get_settings().database_url
    gallery(dsn, 1, 1)
    colony(dsn, 2, None, "forming")
    for bad in ("999", "01", "x", "2"):
        r = await post(client, f"galleries/{bad}/title", {"title": "t"})
        assert r.status_code == 404 and r.json()["detail"] == "not here", bad
    r = await post(client, "galleries/1/title", {"title": "x" * 61})
    assert r.status_code == 400 and r.json()["detail"] == "title too long"
    assert (await post(client, "galleries/1/title", {})).status_code == 422
    naming(dsn, True)
    r = await post(client, "galleries/999/regenerate", {})
    assert r.status_code == 404 and r.json()["detail"] == "not here"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as stranger:
        for path in ("naming", "galleries/1/title", "galleries/1/regenerate"):
            r = await stranger.post(f"{URL}/{path}", json={"on": True, "title": "t"}, headers=XHR)
            assert r.status_code in (401, 403), path
    assert row(dsn, 1) == (None, None)
    assert sql(dsn, "select naming from mycelium_state") == [(True,)]
    assert queued(dsn) == []


async def test_regenerate_joins_a_queued_job_and_waits_for_a_running_one(
    app: Any,  # noqa: F811
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    gallery(dsn, 1, 1)
    naming(dsn, True)
    assert await name_jobs.schedule(p) == 1
    (jid,) = sql(dsn, "select id from jobs where type = %s", name_jobs.JOB)[0]

    # Queued: the automatic job becomes the regenerate, so a gallery that dies while it
    # waits is still named.
    assert await name_jobs.regenerate(p, 1) == jid
    rows = sql(dsn, "select id, payload from jobs where type = %s", name_jobs.JOB)
    assert rows == [(jid, {"colony_id": 1, "regenerate": True})]

    # Running: its handler has read its payload; a merge would be lost, so it is refused.
    sql(
        dsn,
        "update jobs set payload = %s, status = 'running' where id = %s",
        json.dumps({"colony_id": 1}),
        jid,
    )
    with pytest.raises(name_jobs.Busy):
        await name_jobs.regenerate(p, 1)
    r = await post(client, "galleries/1/regenerate", {})
    assert r.status_code == 409 and r.json()["detail"] == "already naming"
    rows = sql(dsn, "select id, payload, status::text from jobs where type = %s", name_jobs.JOB)
    assert rows == [(jid, {"colony_id": 1}, "running")]

    # Once it is done a regenerate queues afresh.
    sql(dsn, "update jobs set status = 'done' where id = %s", jid)
    assert await name_jobs.regenerate(p, 1) != jid
    assert queued(dsn) == [1]


# -- the haiku --------------------------------------------------------------------


async def test_gallery_haiku_is_never_a_picture_haiku(
    app: Any,  # noqa: F811
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    ticks(dsn, 3, START)
    gallery(dsn, 1, 1)
    naming(dsn, True)
    assert await name_jobs.schedule(p) == 1
    await run_queued(
        p, FakeClient(answer(lines=("jars stand in a row", "each one full", "the shelf holds")))
    )

    assert gallery_haikus(dsn, 1) == [
        ("jars stand in a row", "each one full", "the shelf holds", "model", None, True)
    ]
    detail = (await client.get("/api/galleries/1")).json()
    assert detail["haiku"]["lines"] == ["jars stand in a row", "each one full", "the shelf holds"]
    listed = (await client.get("/api/admin/haikus?source=model")).json()["items"]
    assert [h["gallery"] for h in listed] == [1]

    # With the deck switched off only the gallery haiku is active, and nothing that shows
    # or learns from the deck finds it.
    sql(dsn, "update haikus set active = false where colony_id is null")
    assert (await client.get("/api/haiku")).json() == {"id": None, "lines": []}
    assert await haiku.load_examples(p) == []
    async with p.connection() as conn:
        assert await grow.load_pool(conn) == []
    home = (await client.get("/")).text
    assert "jars stand in a row" not in home

    # The same lines again bring the old one back; new lines become what the page shows.
    sql(dsn, "update haikus set active = false where colony_id = 1")
    await post(client, "galleries/1/regenerate", {})
    await run_queued(
        p, FakeClient(answer(lines=("jars stand in a row", "each one full", "the shelf holds")))
    )
    assert len(gallery_haikus(dsn, 1)) == 1 and gallery_haikus(dsn, 1)[0][-1] is True
    await post(client, "galleries/1/regenerate", {})
    await run_queued(p, FakeClient(answer(lines=("lids", "on", "jars"))))
    assert len(gallery_haikus(dsn, 1)) == 2
    detail = (await client.get("/api/galleries/1")).json()
    assert detail["haiku"]["lines"] == ["lids", "on", "jars"]

    # A gallery haiku may repeat a deck haiku's text; it is not the deck's.
    deck = sql(dsn, "select l1, l2, l3 from haikus where source = 'seed' limit 1")[0]
    await post(client, "galleries/1/regenerate", {})
    await run_queued(p, FakeClient(answer(lines=deck)))
    assert len(gallery_haikus(dsn, 1)) == 3

    # A full wipe takes gallery haikus with their galleries rather than handing them to
    # the deck.
    async with p.connection() as conn, conn.transaction():
        await store.wipe_network(conn)
    assert sql(dsn, "select count(*) from haikus where colony_id is null and source = 'model'") == [
        (0,)
    ]


async def test_dead_gallery_is_named_from_its_peak_on_regenerate(app: Any) -> None:  # noqa: F811
    dsn, p = get_settings().database_url, app.state.pool
    public(dsn)
    a = pic(dsn, 1, caption="a jar of teeth")
    gone = pic(dsn, 2, caption="a jar of nothing", status="unpublished")
    colony(
        dsn,
        1,
        1,
        "dead",
        died=5,
        cause="depopulated",
        peak_members=[{"id": a, "slug": "img-1"}, {"id": gone, "slug": "img-2"}],
    )
    naming(dsn, True)
    assert await name_jobs.schedule(p) == 0
    await name_jobs.regenerate(p, 1)
    fake = FakeClient(answer("a jar"))
    await run_queued(p, fake)
    text = fake.calls[0]["messages"][0]["content"]
    assert "a jar of teeth" in text and "a jar of nothing" not in text
    assert row(dsn, 1) == ("a jar", "model")
