"""Collections over http, public and admin, and from the inbox through to a filed leaf."""

from __future__ import annotations

from typing import Any, cast

import httpx
import psycopg
import pytest

from penish.config import Settings, get_settings
from penish.worker import collection_jobs
from penish.worker.jobs import HardError, Job
from tests.test_admin_pictures import XHR, add, client, png  # noqa: F401
from tests.test_ingest import FakeEmbedder, drop, run
from tests.test_ingest import png as ingest_png
from tests.test_ingest import settings as ingest_settings  # noqa: F401

URL = "/api/admin/collections"
PUB = "/api/collections"


def sql(dsn: str, q: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(q.encode(), args)
        return cur.fetchall() if cur.description else []


def leaf(dsn: str, image_id: int, book: int, position: int, **pins: bool) -> None:
    sql(
        dsn,
        "insert into collection_leaves (image_id, collection_id, book_id, position, pinned_book,"
        " pinned_position) select %s, c.id, b.id, %s, %s, %s from collections c"
        " join collection_books b on b.collection_id = c.id"
        " where c.slug = 'pansophicon' and b.number = %s",
        image_id,
        position,
        pins.get("pinned_book", False),
        pins.get("pinned_position", False),
        book,
    )


def history(
    dsn: str, image_id: int, frm: tuple[int, int] | None, to: tuple[int, int] | None
) -> None:
    sql(
        dsn,
        "insert into collection_leaf_history (image_id, collection_id, from_book, from_position,"
        " to_book, to_position, reason) select %s, id, %s, %s, %s, %s, 'auto' from collections",
        image_id,
        *(frm or (None, None)),
        *(to or (None, None)),
    )


def publish(dsn: str, on: bool = True) -> None:
    sql(dsn, "update collections set published = %s", on)


# -- public --------------------------------------------------------------------


async def test_unpublished_is_nowhere(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add(clean_db, 1, caption="x")
    leaf(clean_db, a, 3, 14)
    assert (await client.get(PUB)).json() == {"collections": []}
    assert (await client.get(f"{PUB}/pansophicon")).status_code == 404
    assert (await client.get(f"{PUB}/pansophicon/books/3")).status_code == 404
    detail = (await client.get("/api/images/img-1")).json()
    assert detail["leaf"] is None


async def test_published_collection(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add(clean_db, 1)
    b = add(clean_db, 2)
    hidden = add(clean_db, 3, status="unpublished")
    leaf(clean_db, a, 3, 14)
    leaf(clean_db, b, 3, 2)
    leaf(clean_db, hidden, 1, 1)
    publish(clean_db)

    got = (await client.get(PUB)).json()["collections"]
    assert len(got) == 1
    assert got[0]["title"] == "Πανσοφικὸν τῶν πάντων"
    assert (got[0]["abbrev"], got[0]["abbrev_latin"]) == ("ΠΤΠ", "PTP")
    # A picture taken down is not a leaf anyone can see.
    assert (got[0]["leaves"], got[0]["capacity"]) == (2, 539)

    c = (await client.get(f"{PUB}/pansophicon")).json()
    assert c["preamble"].startswith("Acquired incomplete.")
    assert [bk["number"] for bk in c["books"]] == list(range(1, 8))
    third = c["books"][2]
    assert third["title"] is None
    assert [(lf["position"], lf["slug"]) for lf in third["leaves"]] == [(2, "img-2"), (14, "img-1")]
    assert c["books"][0]["leaves"] == []

    bk = (await client.get(f"{PUB}/pansophicon/books/3")).json()
    assert len(bk["slots"]) == 77
    assert bk["slots"][13]["slug"] == "img-1" and bk["slots"][1]["slug"] == "img-2"
    assert sum(s is not None for s in bk["slots"]) == 2
    assert bk["collection"]["slug"] == "pansophicon"
    assert (await client.get(f"{PUB}/pansophicon/books/8")).status_code == 404

    # Nothing about how filing works is ever served.
    for body in (got, c, bk):
        text = str(body)
        assert "anchor" not in text and "hint" not in text and "score" not in text


async def test_leaf_line_and_previous(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add(clean_db, 1)
    leaf(clean_db, a, 3, 14)
    publish(clean_db)
    assert (await client.get("/api/images/img-1")).json()["leaf"] == {
        "collection": {"slug": "pansophicon", "abbrev": "ΠΤΠ", "abbrev_latin": "PTP"},
        "book": 3,
        "position": 14,
        "previous": None,
    }
    history(clean_db, a, (5, 3), (3, 22))
    history(clean_db, a, (3, 22), (3, 14))
    got = (await client.get("/api/images/img-1")).json()["leaf"]
    assert got["previous"] == {"book": 3, "position": 22}


# -- admin ---------------------------------------------------------------------


async def test_admin_shows_everything(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    r = await client.get(URL)
    assert r.status_code == 200
    body = r.json()
    c = body["collections"][0]
    assert c["slug"] == "pansophicon" and c["published"] is False
    assert all(b["anchor_text"] and b["tag_hints"] for b in c["books"])
    assert body["tuning"]["collections.k"] == {"value": 3.0, "default": 3.0}
    assert body["tuning"]["collections.weekly_resort"]["value"] is False


async def test_admin_patch_collection_and_book(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    r = await client.patch(
        f"{URL}/pansophicon", json={"published": True, "preamble": "Mine now."}, headers=XHR
    )
    assert r.status_code == 200 and r.json()["published"] is True
    assert (await client.get(PUB)).json()["collections"][0]["slug"] == "pansophicon"

    sql(
        clean_db,
        "update collection_books set anchor_vec = '[1,0,0,0]', anchor_model = 'test/embed'",
    )
    r = await client.patch(
        f"{URL}/pansophicon/books/2",
        json={"title": "  herbal  ", "anchor_text": "Leaves.", "tag_hints": ["Leaf", "leaf", " "]},
        headers=XHR,
    )
    assert r.status_code == 200
    b2 = r.json()["books"][1]
    assert (b2["title"], b2["anchor_text"], b2["tag_hints"], b2["anchored"]) == (
        "herbal",
        "Leaves.",
        ["leaf"],
        False,
    )
    assert r.json()["books"][0]["anchored"] is True
    assert sql(clean_db, "select type from jobs") == [("collection_anchor",)]
    r = await client.patch(f"{URL}/pansophicon/books/2", json={"title": ""}, headers=XHR)
    assert r.json()["books"][1]["title"] is None
    pub = (await client.get(f"{PUB}/pansophicon")).json()
    assert pub["preamble"] == "Mine now." and pub["books"][1]["title"] is None


async def test_admin_place_requests_and_moves(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    waiting = add(clean_db, 1, status="processing")
    live = add(clean_db, 2, vec=[1, 0, 0, 0])
    r = await client.put(f"{URL}/pansophicon/pictures/{waiting}", json={"book": "iii"}, headers=XHR)
    assert r.json()["state"] == "requested"
    # Not live: requested, but nothing to file yet.
    assert sql(clean_db, "select count(*) from jobs") == [(0,)]
    r = await client.put(f"{URL}/pansophicon/pictures/{live}", json={}, headers=XHR)
    assert sql(clean_db, "select type, payload->>'image_id' from jobs") == [
        ("collection_file", str(live))
    ]
    bad = await client.put(f"{URL}/pansophicon/pictures/{live}", json={"book": "viii"}, headers=XHR)
    assert bad.status_code == 422 and bad.json()["detail"] == "no book viii"
    bad = await client.put(f"{URL}/pansophicon/pictures/{live}", json={"position": 3}, headers=XHR)
    assert bad.status_code == 422

    mine = await client.get(f"{URL}/picture/{waiting}")
    assert mine.json()["collections"][0]["request"] == {"book": 3, "position": None, "error": None}

    # Filed leaves move now, pinned, and a pinned slot is refused.
    other = add(clean_db, 3, vec=[0, 1, 0, 0])
    leaf(clean_db, live, 1, 1)
    leaf(clean_db, other, 6, 9, pinned_book=True, pinned_position=True)
    r = await client.put(
        f"{URL}/pansophicon/pictures/{live}", json={"book": "vi", "position": "9"}, headers=XHR
    )
    assert r.status_code == 409 and r.json()["detail"] == "book vi, leaf 9 is taken"
    r = await client.put(
        f"{URL}/pansophicon/pictures/{live}", json={"book": "VI", "position": 10}, headers=XHR
    )
    assert r.status_code == 200, r.text
    moved = [m for m in r.json()["moves"] if m["reason"] == "owner"]
    assert moved[0]["from"] == {"book": 1, "position": 1}
    assert moved[0]["to"] == {"book": 6, "position": 10}
    mine = (await client.get(f"{URL}/picture/{live}")).json()["collections"][0]
    assert mine["leaf"] == {"book": 6, "position": 10, "pinned_book": True, "pinned_position": True}

    assert (
        await client.post(
            f"{URL}/pansophicon/pictures/{live}/pin", json={"pinned": False}, headers=XHR
        )
    ).status_code == 200
    assert sql(clean_db, "select pinned_book from collection_leaves where image_id = %s", live) == [
        (False,)
    ]
    assert (
        await client.delete(f"{URL}/pansophicon/pictures/{live}", headers=XHR)
    ).status_code == 200
    assert (
        await client.delete(f"{URL}/pansophicon/pictures/{live}", headers=XHR)
    ).status_code == 404
    hist = (await client.get(URL)).json()["collections"][0]["history"]
    assert hist[0]["to"] is None and hist[0]["from"] == {"book": 6, "position": 10}


async def test_admin_retry_and_resort(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    sql(
        clean_db,
        "insert into collection_requests (image_id, collection_id, error)"
        " select %s, id, 'every book is full' from collections",
        a,
    )
    errs = (await client.get(URL)).json()["collections"][0]["requests"]
    assert errs[0]["error"] == "every book is full" and errs[0]["slug"] == "img-1"
    r = await client.post(f"{URL}/pansophicon/requests/{a}/retry", headers=XHR)
    assert r.json() == {"queued": True}

    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    leaf(clean_db, b, 4, 50)
    r = await client.post(f"{URL}/pansophicon/resort", json={"dry_run": True}, headers=XHR)
    assert r.status_code == 200
    proposed = r.json()["moves"]
    assert proposed and proposed[0]["slug"] == "img-2"
    assert sql(clean_db, "select position from collection_leaves") == [(50,)]
    r = await client.post(f"{URL}/pansophicon/resort", json={"dry_run": False}, headers=XHR)
    assert r.json()["moves"] == proposed
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(len(proposed),)]


async def test_upload_writes_collection_sidecar(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    r = await client.post(
        "/api/admin/upload",
        headers=XHR,
        files={"image": ("x.png", png(), "image/png")},
        data={"collection": "pansophicon", "book": "3", "leaf": "14"},
    )
    assert r.status_code == 200, r.text
    stem = r.json()["stem"]
    side = get_settings().inbox / f"{stem}_collection.txt"
    assert side.read_text() == "pansophicon iii 14\n"
    for bad in ({"book": "viii"}, {"leaf": "3"}, {"book": "i", "leaf": "78"}):
        r = await client.post(
            "/api/admin/upload",
            headers=XHR,
            files={"image": ("x.png", png(), "image/png")},
            data={"collection": "pansophicon", **bad},
        )
        assert r.status_code == 422, bad


# -- from the inbox ---------------------------------------------------------------


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


async def q(p: Any, s: str, *args: Any) -> list[dict[str, Any]]:
    async with p.connection() as conn:
        cur = await conn.execute(s.encode(), args)
        return list(await cur.fetchall())


async def test_malformed_sidecar_never_fails_the_picture(
    p: Any,
    ingest_settings: Settings,  # noqa: F811
) -> None:
    job = drop(ingest_settings, "lamp", ingest_png(1), collection="pansophicon iiii")
    await run(job, p, ingest_settings)
    assert [r["status"] for r in await q(p, "select status::text as status from images")] == [
        "live"
    ]
    assert await q(p, "select * from collection_requests") == []
    failed = await q(
        p, "select status::text as status, last_error from jobs where type = 'collection_file'"
    )
    assert len(failed) == 1 and failed[0]["status"] == "failed"
    assert "lamp_collection.txt" in failed[0]["last_error"] and "iiii" in failed[0]["last_error"]
    # A retry from the queue page says the same thing again.
    rows = await q(p, "select id, payload from jobs where type = 'collection_file'")
    marker = Job(
        id=rows[0]["id"],
        type="collection_file",
        payload=rows[0]["payload"],
        attempts=1,
        last_error=None,
    )
    with pytest.raises(HardError, match="iiii"):
        await collection_jobs.handle_file(
            marker, pool=p, settings=ingest_settings, embedder=FakeEmbedder()
        )

    unknown = drop(ingest_settings, "moss", ingest_png(2), collection="bestiary")
    await run(unknown, p, ingest_settings)
    errs = await q(p, "select last_error from jobs where type = 'collection_file' order by id")
    assert "no collection 'bestiary'" in errs[-1]["last_error"]


async def test_sidecar_to_leaf(p: Any, ingest_settings: Settings) -> None:  # noqa: F811
    job = drop(ingest_settings, "lamp", ingest_png(1), collection="pansophicon iii")
    await run(job, p, ingest_settings)
    req = await q(p, "select book, position from collection_requests")
    assert req == [{"book": 3, "position": None}]
    jobs = await q(p, "select id, payload from jobs where type = 'collection_file'")
    assert len(jobs) == 1
    emb = FakeEmbedder()
    filing = Job(
        id=jobs[0]["id"],
        type="collection_file",
        payload=jobs[0]["payload"],
        attempts=1,
        last_error=None,
    )
    await collection_jobs.handle_file(filing, pool=p, settings=ingest_settings, embedder=emb)
    # Every anchor embedded under the current model before scoring.
    assert len(emb.calls) == 1 and len(emb.calls[0]) == 7
    anchored = await q(
        p, "select count(*) as n from collection_books where anchor_model = 'test/embed'"
    )
    assert anchored == [{"n": 7}]
    got = await q(
        p,
        "select b.number, l.position, l.pinned_book, l.pinned_position from collection_leaves l"
        " join collection_books b on b.id = l.book_id",
    )
    assert got == [{"number": 3, "position": 1, "pinned_book": True, "pinned_position": False}]
    assert await q(p, "select * from collection_requests") == []
    # A second run has no request left and changes nothing.
    await collection_jobs.handle_file(filing, pool=p, settings=ingest_settings, embedder=emb)
    assert len(emb.calls) == 1
