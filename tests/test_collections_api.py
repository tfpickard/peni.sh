"""Collections over http: the public list, collection and book, the picture's leaf line,
the admin surface, and the upload form's collection fields."""

from __future__ import annotations

from typing import Any

import httpx
import numpy as np
import pytest

from penish.collection import store
from penish.config import get_settings
from tests import test_admin, test_admin_pictures
from tests.test_admin_pictures import XHR, add, sha, sql

# The admin pictures client: logged in, with a 4-wide test embedding space.
client = test_admin_pictures.client
# A client holding only the read token.
reader = test_admin.reader
bearer = test_admin._bearer

PUB = "/api/collections"
ADM = "/api/admin/collections"
SLUG = "pansophicon"


def cid(dsn: str) -> int:
    return int(sql(dsn, "select id from collections where slug = %s", SLUG)[0][0])


def publish(dsn: str, on: bool = True) -> None:
    sql(dsn, "update collections set published = %s", on)


def leaf(dsn: str, image_id: int, book: int, position: int, pinned: bool = False) -> None:
    sql(
        dsn,
        "insert into collection_leaves (image_id, collection_id, book, position, book_pinned,"
        " pinned) values (%s, %s, %s, %s, %s, %s)",
        image_id,
        cid(dsn),
        book,
        position,
        pinned,
        pinned,
    )


def noted(dsn: str, image_id: int, note: str, source: str = "owner") -> None:
    sql(
        dsn,
        "update collection_leaves set note = %s, note_source = %s where image_id = %s",
        note,
        source,
        image_id,
    )


def moved(dsn: str, image_id: int, frm: tuple[int, int] | None, to: tuple[int, int]) -> None:
    sql(
        dsn,
        "insert into collection_leaf_history (image_id, collection_id, from_book, from_position,"
        " to_book, to_position, reason) values (%s, %s, %s, %s, %s, %s, 'resort')",
        image_id,
        cid(dsn),
        frm[0] if frm else None,
        frm[1] if frm else None,
        *to,
    )


# -- public ------------------------------------------------------------------------


async def test_unpublished_is_not_here(client: httpx.AsyncClient, clean_db: str) -> None:
    i = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, i, 3, 14)
    noted(clean_db, i, "Vellum. Hand A.")
    # None of the collection's own text, nor the leaf's note, shows anywhere.
    for path in (PUB, f"{PUB}/{SLUG}", "/api/images/img-1"):
        text = (await client.get(path)).text
        for word in ("Pansophikon ton panton", "Ioannina", "IMPORTANT BYZANTINE", "Hand A"):
            assert word not in text
    assert (await client.get(PUB)).json() == {"collections": []}
    assert (await client.get(f"{PUB}/{SLUG}")).status_code == 404
    assert (await client.get(f"{PUB}/{SLUG}/books/3")).status_code == 404
    assert (await client.get("/api/images/img-1")).json()["leaf"] is None
    assert (await client.get(f"/collections/{SLUG}")).status_code == 404


async def test_published_list_and_collection(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    hidden = add(clean_db, 3, status="unpublished")
    leaf(clean_db, a, 3, 14)
    leaf(clean_db, b, 1, 2)
    leaf(clean_db, hidden, 1, 1)
    publish(clean_db)

    r = (await client.get(PUB)).json()["collections"]
    assert r == [
        {
            "slug": SLUG,
            "title": "Πανσοφικὸν τῶν πάντων",
            "abbrev": "ΠΤΠ",
            "abbrev_latin": "PTP",
            "book_count": 7,
            "leaves_per_book": 77,
            "leaves": 2,
            "capacity": 539,
        }
    ]
    c = (await client.get(f"{PUB}/{SLUG}")).json()
    assert c["preamble"].startswith("Acquired incomplete.")
    assert [bk["number"] for bk in c["books"]] == list(range(1, 8))
    assert [(x["position"], x["slug"]) for x in c["books"][0]["leaves"]] == [(2, "img-2")]
    assert [(x["position"], x["slug"]) for x in c["books"][2]["leaves"]] == [(14, "img-1")]
    assert all(bk["title"] is None for bk in c["books"])
    # The filer's private material stays private.
    text = (await client.get(f"{PUB}/{SLUG}")).text
    for word in ("anchor", "tag_hints", "score", "Bestiary", "pinned"):
        assert word not in text
    page = await client.get(f"/collections/{SLUG}")
    assert page.status_code == 200 and "Πανσοφικὸν τῶν πάντων" in page.text


async def test_book_has_every_slot(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 3, 14)
    publish(clean_db)
    r = (await client.get(f"{PUB}/{SLUG}/books/3")).json()
    assert r["number"] == 3 and r["title"] is None
    assert len(r["slots"]) == 77
    assert [i for i, s in enumerate(r["slots"]) if s] == [13]
    assert r["slots"][13]["slug"] == "img-1" and r["slots"][13]["variants"]
    assert r["collection"]["leaves"] == 1
    assert (await client.get(f"{PUB}/{SLUG}/books/8")).status_code == 404
    assert (await client.get(f"/collections/{SLUG}/8")).status_code == 404
    page = await client.get(f"/collections/{SLUG}/3")
    assert page.status_code == 200 and "ΠΤΠ iii" in page.text


# -- incertae sedis: the loose leaves (book 0) --------------------------------------


async def test_loose_leaves_row_and_counts(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    c = add(clean_db, 3, vec=[0, 0, 1, 0])
    leaf(clean_db, a, 3, 14)
    publish(clean_db)
    # None loose yet: seven rows, as ever.
    got = (await client.get(f"{PUB}/{SLUG}")).json()
    assert [bk["number"] for bk in got["books"]] == list(range(1, 8))
    leaf(clean_db, b, 0, 4)
    leaf(clean_db, c, 0, 1)
    # The books' count is the books'; capacity stays seven of seventy-seven.
    head = (await client.get(PUB)).json()["collections"][0]
    assert (head["leaves"], head["capacity"], head["book_count"]) == (1, 539, 7)
    got = (await client.get(f"{PUB}/{SLUG}")).json()
    assert got["leaves"] == 1
    assert [bk["number"] for bk in got["books"]] == [1, 2, 3, 4, 5, 6, 7, 0]
    loose = got["books"][-1]
    assert loose["title"] == "incertae sedis"
    assert [(x["position"], x["slug"]) for x in loose["leaves"]] == [(1, "img-3"), (4, "img-2")]


async def test_loose_leaves_page(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 0, 4)
    leaf(clean_db, b, 0, 120)
    publish(clean_db)
    r = (await client.get(f"{PUB}/{SLUG}/books/0")).json()
    # No frame of 77: the leaves there are, in number order, past 77 if need be.
    assert (r["number"], r["title"]) == (0, "incertae sedis")
    assert [(s["position"], s["slug"]) for s in r["slots"]] == [(4, "img-1"), (120, "img-2")]
    assert r["collection"]["leaves"] == 0
    page = await client.get(f"/collections/{SLUG}/0")
    assert page.status_code == 200 and "ΠΤΠ incertae sedis" in page.text
    # Its title is the owner's, on the page as anywhere.
    sql(clean_db, "update collection_books set title = 'sine loco' where number = 0")
    page = await client.get(f"/collections/{SLUG}/0")
    assert "ΠΤΠ sine loco" in page.text
    # A collection with no place for loose leaves has no such page.
    sql(clean_db, "delete from collection_leaves")
    sql(clean_db, "delete from collection_books where number = 0")
    assert (await client.get(f"{PUB}/{SLUG}/books/0")).status_code == 404
    assert (await client.get(f"/collections/{SLUG}/0")).status_code == 404


async def test_a_loose_leaf_on_its_picture_page(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 0, 4)
    moved(clean_db, a, None, (0, 4))
    leaf(clean_db, b, 3, 14)
    moved(clean_db, b, (0, 4), (3, 14))
    publish(clean_db)
    # The line names the loose leaves by the title it is given (`ptp incertae sedis, leaf 4`),
    # and a leaf once loose says so (`formerly ptp incertae sedis, leaf 4`).
    got = (await client.get("/api/images/img-1")).json()["leaf"]
    assert (got["book"], got["position"], got["previous"]) == (0, 4, None)
    assert got["collection"]["loose_title"] == "incertae sedis"
    got = (await client.get("/api/images/img-2")).json()["leaf"]
    assert (got["book"], got["previous"]) == (3, {"book": 0, "position": 4})
    # Strips carry the title too: img-2's neighbours include the loose img-1.
    near = (await client.get("/api/images/img-2")).json()
    refs = [x["leaf"] for k in ("similar", "dissimilar") for x in near.get(k, []) if x.get("leaf")]
    assert refs and all(r["book"] == 0 for r in refs)
    assert all(r["collection"]["loose_title"] == "incertae sedis" for r in refs)


async def test_the_week_line_names_a_loose_leaf(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 0, 4)
    moved(clean_db, a, (2, 9), (0, 4))
    publish(clean_db)
    got = (await client.get("/api/stats")).json()["changed"]
    assert (got["book"], got["position"], got["from"]) == (0, 4, {"book": 2, "position": 9})
    assert got["collection"]["loose_title"] == "incertae sedis"


async def test_admin_loose_leaves(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 3, 14)
    leaf(clean_db, b, 0, 1)
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert d["loose_title"] == "incertae sedis"
    last = d["books"][-1]
    assert (last["number"], last["title"], last["anchor_state"], last["hint_leaves"]) == (
        0,
        "incertae sedis",
        None,
        {},
    )
    assert [x["image_id"] for x in last["leaves"]] == [b]
    listed = (await client.get(ADM)).json()["collections"][0]
    assert listed["loose_title"] == "incertae sedis"
    # The owner moves a leaf out of a book into the loose leaves, and gives one a number.
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 0})
    assert r.status_code == 200
    assert places(clean_db)[a] == (0, 2)
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 0, "position": 1})
    # The leaf the filer had at 1 goes to the end, which a's leaving 2 has made 2.
    assert places(clean_db) == {a: (0, 1), b: (0, 2)}
    # Pinned there, it cannot be pushed aside; moving out leaves its number empty.
    assert (
        await client.put(f"{ADM}/{SLUG}/leaves/{b}", json={"book": 0, "position": 1})
    ).json() == {"detail": "incertae sedis leaf 1 is taken"}
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 2})
    # Out again: its number stays a gap, and nobody is renumbered into it.
    assert places(clean_db) == {a: (2, 1), b: (0, 2)}
    # A title and nothing else.
    r = await client.patch(f"{ADM}/{SLUG}/books/0", json={"title": "sine loco"})
    assert r.json()["loose_title"] == "sine loco"
    r = await client.patch(
        f"{ADM}/{SLUG}/books/0", json={"title": "sine loco", "anchor_text": "", "tag_hints": []}
    )
    assert r.status_code == 200
    for bad in ({"anchor_text": "Things."}, {"tag_hints": ["x"]}, {"title": " "}):
        assert (await client.patch(f"{ADM}/{SLUG}/books/0", json=bad)).status_code == 422
    assert sql(clean_db, "select count(*) from jobs where type = 'collection_anchor'") == [(0,)]


async def test_admin_files_a_picture_straight_into_the_loose_leaves(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 0, "position": 300})
    assert r.status_code == 200 and r.json()["requests"][0]["book"] == 0
    d = (await client.get(f"/api/admin/pictures/{a}")).json()["collections"][0]
    assert d["request"]["book"] == 0
    sql(clean_db, "delete from collection_requests")
    sql(clean_db, "delete from collection_books where number = 0")
    assert (await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 0})).json() == {
        "detail": "no book 0"
    }


async def test_picture_leaf_and_previous(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 3, 14)
    moved(clean_db, a, None, (5, 3))
    publish(clean_db)
    got = (await client.get("/api/images/img-1")).json()["leaf"]
    assert got == {
        "collection": {
            "slug": SLUG,
            "title": "Πανσοφικὸν τῶν πάντων",
            "abbrev": "ΠΤΠ",
            "abbrev_latin": "PTP",
            "loose_title": "incertae sedis",
        },
        "book": 3,
        "position": 14,
        "previous": None,
        "note": None,
    }
    moved(clean_db, a, (5, 3), (3, 22))
    moved(clean_db, a, (3, 22), (3, 14))
    got = (await client.get("/api/images/img-1")).json()["leaf"]
    assert got["previous"] == {"book": 3, "position": 22}


async def test_owner_text_and_leaf_note(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 3, 14)
    noted(clean_db, a, "Ledger paper, machine-ruled.", "model")
    publish(clean_db)
    c = (await client.get(f"{PUB}/{SLUG}")).json()
    assert c["gloss"] == "Pansophikon ton panton. The all-wise thing of all things."
    assert [label for label, _ in c["record"]] == [
        "extent",
        "languages",
        "support",
        "condition",
        "provenance",
        "exhibited",
        "literature",
    ]
    provenance = dict(c["record"])["provenance"].split("\n")
    assert len(provenance) == 5 and provenance[-1].startswith("Acquired 2026")
    assert c["vendor_text"].startswith("IMPORTANT BYZANTINE CODEX")
    assert (await client.get("/api/images/img-1")).json()["leaf"]["note"] == (
        "Ledger paper, machine-ruled."
    )
    # A leaf's note is public; the filer's internals and the note's source stay private.
    text = (await client.get("/api/images/img-1")).text
    assert "note_source" not in text and "note_model" not in text


# -- admin ---------------------------------------------------------------------------


async def test_admin_needs_login(client: httpx.AsyncClient) -> None:
    client.cookies.clear()
    assert (await client.get(ADM)).status_code == 401


async def test_admin_detail(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 2, 7, pinned=True)
    assert (await client.get(ADM)).json()["collections"][0]["slug"] == SLUG
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert d["published"] is False and d["tuning"]["anchor_k"] == 10.0
    assert d["tuning_limits"]["anchor_k"] == [0.0, 1000.0]
    assert d["books"][0]["anchor_text"].startswith("Animals and composite creatures")
    assert d["books"][0]["anchored"] is False and "creature" in d["books"][0]["tag_hints"]
    assert d["books"][1]["leaves"] == [
        {
            "image_id": a,
            "slug": "img-1",
            "status": "live",
            "thumb": d["books"][1]["leaves"][0]["thumb"],
            "position": 7,
            "pinned": True,
            "book_pinned": True,
            "note": None,
            "note_source": None,
            "note_queued": False,
        }
    ]


def arrived(dsn: str, stem: str, n: int, error: str | None = None) -> None:
    sql(
        dsn,
        "insert into uploads (stem, sha256, filename, collection_line, source, error)"
        " values (%s, %s, %s, %s, 'form', %s)",
        stem,
        sha(n),
        f"{stem}.HEIC",
        SLUG,
        error,
    )


async def test_admin_detail_lists_uploads_not_yet_leaves(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    arrived(clean_db, "broken", 1, error="broken.heic: not a readable image")
    filed = add(clean_db, 2, vec=[1, 0, 0, 0])
    leaf(clean_db, filed, 1, 1)
    arrived(clean_db, "filed", 2)
    waiting = add(clean_db, 3, vec=[0, 1, 0, 0])
    sql(
        clean_db,
        "insert into collection_requests (image_id, collection_id, book) values (%s, %s, 1)",
        waiting,
        cid(clean_db),
    )
    arrived(clean_db, "waiting", 3)
    sql(
        clean_db,
        "insert into uploads (stem, sha256, source) values ('elsewhere', %s, 'rsync')",
        sha(4),
    )

    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert [r["image_id"] for r in d["requests"]] == [waiting]
    assert d["uploads"] == [
        {
            "stem": "broken",
            "sha256": sha(1),
            "filename": "broken.HEIC",
            "stage": "failed",
            "error": "broken.heic: not a readable image",
            "slug": None,
            "image_id": None,
            "created_at": d["uploads"][0]["created_at"],
        }
    ]
    assert d["uploads"][0]["created_at"]


async def test_admin_edit_collection(client: httpx.AsyncClient, clean_db: str) -> None:
    r = await client.patch(
        f"{ADM}/{SLUG}",
        json={"preamble": "Found in a drawer.", "published": True, "tuning": {"anchor_k": 5}},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["preamble"] == "Found in a drawer." and d["published"] is True
    assert d["tuning"]["anchor_k"] == 5.0 and d["tuning"]["tag_alpha"] == 0.05
    assert (
        await client.patch(f"{ADM}/{SLUG}", json={"tuning": {"fill_beta": 7}})
    ).status_code == 422
    assert (await client.patch(f"{ADM}/{SLUG}", json={"tuning": {"nope": 1}})).status_code == 422
    assert (await client.get(PUB)).json()["collections"][0]["slug"] == SLUG


async def test_admin_edit_book_reembeds_anchor(client: httpx.AsyncClient, clean_db: str) -> None:
    sql(
        clean_db,
        "update collection_books set anchor_embedding = '[1,0,0,0]', anchor_model = %s,"
        " anchor_dim = 4",
        get_settings().embed_model,
    )
    r = await client.patch(
        f"{ADM}/{SLUG}/books/1",
        json={"title": " bestiary ", "tag_hints": ["Beast", "beast", " "], "anchor_text": "x"},
    )
    d = r.json()["books"][0]
    assert d["title"] == "bestiary" and d["tag_hints"] == ["beast"] and d["anchor_text"] == "x"
    assert d["anchored"] is False
    assert r.json()["books"][1]["anchored"] is True
    jobs = sql(clean_db, "select type from jobs")
    assert jobs == [("collection_anchor",)]
    r = await client.patch(f"{ADM}/{SLUG}/books/1", json={"title": ""})
    assert r.json()["books"][0]["title"] is None
    assert (await client.patch(f"{ADM}/{SLUG}/books/9", json={"title": "x"})).status_code == 404


async def test_anchor_state_and_when(client: httpx.AsyncClient, clean_db: str, pool: Any) -> None:
    anchored(clean_db)
    # Book ii was embedded under another model; book iii has none at all.
    sql(clean_db, "update collection_books set anchor_model = 'old' where number = 2")
    sql(clean_db, "update collection_books set anchor_embedding = null where number = 3")
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    states = [b["anchor_state"] for b in d["books"]]
    # The loose leaves come last, with no anchor to have a state.
    assert states == [
        "embedded",
        "stale",
        "stale",
        "embedded",
        "embedded",
        "embedded",
        "embedded",
        None,
    ]
    # Embedded before the time was kept.
    assert d["books"][0]["anchor_embedded_at"] is None

    for status in ("queued", "running"):
        sql(clean_db, "delete from jobs")
        sql(
            clean_db,
            "insert into jobs (type, payload, status) values ('collection_anchor', %s, %s)",
            f'{{"collection_id": {cid(clean_db)}}}',
            status,
        )
        d = (await client.get(f"{ADM}/{SLUG}")).json()
        assert [b["anchor_state"] for b in d["books"]][:3] == ["embedded", "queued", "queued"]
    sql(clean_db, "update jobs set status = 'done'")
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert [b["anchor_state"] for b in d["books"]][:3] == ["embedded", "stale", "stale"]

    settings = get_settings()
    async with pool.connection() as conn:
        c = await store.collection_by_slug(conn, SLUG)
        assert c is not None
        text = d["books"][2]["anchor_text"]
        assert await store.store_anchor(conn, c["id"], 3, text, np.asarray([0, 0, 1, 0]), settings)
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert d["books"][2]["anchor_state"] == "embedded" and d["books"][2]["anchor_embedded_at"]

    # An edit drops the vector and its time together, and queues the embedding.
    r = await client.patch(f"{ADM}/{SLUG}/books/3", json={"anchor_text": "Other words."})
    b = r.json()["books"][2]
    assert (b["anchor_state"], b["anchor_embedded_at"]) == ("queued", None)
    assert sql(clean_db, "select anchor_embedded_at from collection_books where number = 3") == [
        (None,)
    ]


def tagged(dsn: str, image_id: int, *tags: str) -> None:
    for t in tags:
        sql(dsn, "insert into tags (image_id, tag, source) values (%s, %s, 'model')", image_id, t)


async def test_hint_leaves_count_matching_leaves(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    loose = add(clean_db, 3, vec=[0, 0, 1, 0])
    leaf(clean_db, a, 1, 1)
    leaf(clean_db, b, 5, 1)
    # A tag's words match as tag_overlap matches them, hyphens included; a picture that is
    # not a leaf counts for nothing.
    tagged(clean_db, a, "coiled serpent", "zodiac-man")
    tagged(clean_db, b, "serpent", "alchemy lab")
    tagged(clean_db, loose, "serpent", "flask")
    sql(
        clean_db,
        "update collection_books set tag_hints = %s where number = 1",
        ["serpent", "man", "lab", "flask", "serp"],
    )
    d = (await client.get(f"{ADM}/{SLUG}")).json()
    assert d["books"][0]["hint_leaves"] == {"serpent": 2, "man": 1, "lab": 1, "flask": 0, "serp": 0}
    assert d["books"][1]["hint_leaves"] == dict.fromkeys(d["books"][1]["tag_hints"], 0)


async def test_history_is_the_latest_twenty_and_filters_by_leaf(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    for k in range(25):
        moved(clean_db, a, (1, k + 1), (1, k + 2))
    moved(clean_db, b, None, (2, 1))
    sql(clean_db, "update collection_leaf_history set chosen_by = 'judge' where image_id = %s", b)
    hist = (await client.get(f"{ADM}/{SLUG}")).json()["history"]
    assert len(hist) == 20
    assert (hist[0]["slug"], hist[0]["chosen_by"], hist[0]["reason"]) == (
        "img-2",
        "judge",
        "resort",
    )
    assert hist[1]["to_position"] == 26 and hist[1]["chosen_by"] is None and hist[1]["at"]
    for leaf_ in (str(b), "img-2"):
        got = (await client.get(f"{ADM}/{SLUG}", params={"leaf": leaf_})).json()["history"]
        assert [h["image_id"] for h in got] == [b]
    got = (await client.get(f"{ADM}/{SLUG}", params={"leaf": "img-1"})).json()["history"]
    assert len(got) == 20 and {h["image_id"] for h in got} == {a}
    assert (await client.get(f"{ADM}/{SLUG}", params={"leaf": "nope"})).json()["history"] == []
    # Digits that are not ASCII, and a run too long for an id, are slugs that match nothing,
    # not a 500.
    for odd in ("\u00b2", "\u0661\u0662", "9" * 30):
        r = await client.get(f"{ADM}/{SLUG}", params={"leaf": odd})
        assert r.status_code == 200 and r.json()["history"] == []
    assert len((await client.get(f"{ADM}/{SLUG}", params={"leaf": " "})).json()["history"]) == 20


async def test_judge_failures_among_recent_filings(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    assert (await client.get(f"{ADM}/{SLUG}")).json()["judge_failures"] == {}

    def filed(n: int, error: str | None, at: tuple[int, int] | None = None) -> None:
        i = add(clean_db, n, vec=[1, 0, 0, 0])
        sql(
            clean_db,
            "insert into collection_leaf_history (image_id, collection_id, from_book,"
            " from_position, to_book, to_position, reason, chosen_by, scores)"
            " values (%s, %s, %s, %s, 1, %s, 'auto', 'score', %s)",
            i,
            cid(clean_db),
            at[0] if at else None,
            at[1] if at else None,
            n,
            f'{{"judge": {{"error": "{error}"}}}}' if error else '{"best": 1}',
        )

    # The oldest failure falls out of the last twenty filings; a move is not a filing.
    filed(1, "error")
    for n, e in enumerate(["refused", "timeout", "off", None, "refused"], start=2):
        filed(n, e)
    for n in range(7, 22):
        filed(n, None)
    filed(22, "invalid", at=(2, 1))
    assert (await client.get(f"{ADM}/{SLUG}")).json()["judge_failures"] == {
        "refused": 2,
        "timeout": 1,
    }


async def test_admin_request_move_pin_unfile(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    # Unfiled: a request, and a job because the picture is live.
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 4})
    assert r.status_code == 200
    assert r.json()["requests"][0]["book"] == 4 and r.json()["requests"][0]["job"] == "queued"
    assert (await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 9})).status_code == 409
    assert (await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"position": 3})).status_code == 409
    assert (await client.put(f"{ADM}/{SLUG}/leaves/999", json={})).status_code == 404

    # Filed: a move pins it, and pushing onto a pinned leaf is refused.
    leaf(clean_db, b, 1, 1)
    r = await client.put(f"{ADM}/{SLUG}/leaves/{b}", json={"book": 2, "position": 5})
    assert r.status_code == 200
    got = r.json()["books"][1]["leaves"][0]
    assert (got["position"], got["pinned"]) == (5, True)
    leaf(clean_db, a, 3, 1)
    sql(clean_db, "delete from collection_requests")
    assert (
        await client.put(f"{ADM}/{SLUG}/leaves/{a}", json={"book": 2, "position": 5})
    ).status_code == 409
    hist = r.json()["history"]
    assert (hist[0]["from_book"], hist[0]["to_book"], hist[0]["reason"]) == (1, 2, "owner")

    r = await client.post(f"{ADM}/{SLUG}/leaves/{b}/pin", headers=XHR, json={"pinned": False})
    assert r.json()["books"][1]["leaves"][0]["pinned"] is False
    r = await client.delete(f"{ADM}/{SLUG}/leaves/{b}")
    assert r.status_code == 200 and r.json()["books"][1]["leaves"] == []
    assert (await client.delete(f"{ADM}/{SLUG}/leaves/{b}")).status_code == 404
    # Still on the site.
    assert (await client.get("/api/images/img-2")).status_code == 200


async def test_admin_retry_request(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    sql(
        clean_db,
        "insert into collection_requests (image_id, collection_id, book, error)"
        " values (%s, %s, 1, 'book i is full')",
        a,
        cid(clean_db),
    )
    r = await client.post(f"{ADM}/{SLUG}/requests/{a}/retry", headers=XHR)
    assert r.json()["requests"][0]["error"] is None
    assert sql(clean_db, "select type from jobs") == [("collection_file",)]
    assert (await client.post(f"{ADM}/{SLUG}/requests/99/retry", headers=XHR)).status_code == 404


def anchored(dsn: str) -> None:
    """Every book's anchor embedded, book n along axis (n - 1) % 4 of the test space."""
    for n in range(1, 8):
        v = [0.0] * 4
        v[(n - 1) % 4] = 1.0
        sql(
            dsn,
            "update collection_books set anchor_embedding = %s::vector, anchor_model = %s,"
            " anchor_dim = 4 where number = %s",
            str(v),
            get_settings().embed_model,
            n,
        )


async def dry(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": True})


async def apply(client: httpx.AsyncClient, plan: dict[str, object]) -> httpx.Response:
    return await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": False, **plan})


def places(dsn: str) -> dict[int, tuple[int, int]]:
    rows = sql(dsn, "select image_id, book, position from collection_leaves")
    return {i: (b, p) for i, b, p in rows}


async def test_admin_resort_dry_run_and_apply(client: httpx.AsyncClient, clean_db: str) -> None:
    anchored(clean_db)
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[1, 0, 0, 0])
    stray = add(clean_db, 3, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 1, 40)
    leaf(clean_db, b, 1, 41)
    leaf(clean_db, stray, 3, 5)
    r = await dry(client)
    assert r.status_code == 200
    plan = r.json()
    assert isinstance(plan["plan_id"], str) and len(plan["plan_id"]) >= 32
    moves = {m["slug"]: m for m in plan["moves"]}
    assert set(moves) == {"img-1", "img-2", "img-3"}
    # The stray goes home to book ii, ahead of book vi (the same axis, with no leaves
    # either) by nothing but the lower number.
    m = moves["img-3"]
    assert (m["before"], m["after"][0]) == ([3, 5], 2)
    assert m["thumb"] and m["image_id"] == stray
    assert m["margin"] == pytest.approx(0.0)
    # Book v shares book i's axis and is empty, so img-1 trails it by book i's fill alone,
    # well inside the margin a re-sort needs to move it.
    assert -0.001 < moves["img-1"]["margin"] < 0
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]
    # The same state plans the same plan.
    assert (await dry(client)).json() == plan

    r = await apply(client, plan)
    assert r.status_code == 200
    got = places(clean_db)
    assert got == {x["image_id"]: tuple(x["after"]) for x in plan["moves"]}
    hist = sql(
        clean_db,
        "select image_id, reason, chosen_by from collection_leaf_history order by id",
    )
    assert {(i, why) for i, why, _ in hist} == {(a, "resort"), (b, "resort"), (stray, "resort")}
    assert {i: by for i, _, by in hist} == {a: None, b: None, stray: "score"}
    assert r.json()["last_resort_at"]
    # Applied once; the same plan again is for a collection that is no longer there.
    assert (await apply(client, plan)).json() == {"detail": "changed"}
    # No plan, no re-sort.
    r = await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": False})
    assert r.status_code == 422


async def test_apply_applies_the_plan_not_a_new_one(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    anchored(clean_db)
    a = add(clean_db, 1, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 1, 1)
    plan = (await dry(client)).json()
    assert [m["after"][0] for m in plan["moves"]] == [2]
    # The picture's own vector moves on after the dry run, and a fresh plan would keep it
    # in book i; the owner is still given what they were shown.
    sql(clean_db, "update embeddings set vec = '[1,0,0,0]' where image_id = %s", a)
    assert (await dry(client)).json()["moves"] == []
    assert (await apply(client, plan)).status_code == 200
    assert places(clean_db)[a][0] == 2


async def test_apply_refuses_a_changed_collection(client: httpx.AsyncClient, clean_db: str) -> None:
    anchored(clean_db)
    a = add(clean_db, 1, vec=[0, 1, 0, 0])
    b = add(clean_db, 2, vec=[0, 0, 1, 0])
    leaf(clean_db, a, 1, 1)
    leaf(clean_db, b, 1, 2)

    async def refused_after(change: str, *args: object) -> None:
        plan = (await dry(client)).json()
        assert plan["moves"]
        sql(clean_db, change, *args)
        r = await apply(client, plan)
        assert (r.status_code, r.json()) == (409, {"detail": "changed"}), change
        assert places(clean_db)[a][0] == 1

    # A leaf moved, a pin changed, the tuning changed, an anchor re-embedded.
    await refused_after("update collection_leaves set position = 9 where image_id = %s", b)
    await refused_after("update collection_leaves set book_pinned = true where image_id = %s", b)
    await refused_after("""update collections set tuning = tuning || '{"anchor_k": 4}'""")
    await refused_after(
        "update collection_books set anchor_embedding = '[0,0,0,1]' where number = 2"
    )
    # A plan altered on the way back is not the plan that was made.
    plan = (await dry(client)).json()
    plan["moves"][0]["after"] = [7, 77]
    assert (await apply(client, plan)).json() == {"detail": "changed"}
    plan = (await dry(client)).json()
    plan["plan_id"] = "0" * 64
    assert (await apply(client, plan)).json() == {"detail": "changed"}
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]


async def test_dry_run_waits_for_anchors(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 1, 40)
    anchored(clean_db)
    sql(clean_db, "update collection_books set anchor_embedding = null where number = 4")
    for _ in range(2):
        r = await dry(client)
        assert (r.status_code, r.json()) == (409, {"detail": "anchors"})
    # Queued once, however often it is asked.
    assert sql(clean_db, "select type, payload from jobs") == [
        ("collection_anchor", {"collection_id": cid(clean_db)})
    ]
    anchored(clean_db)
    assert (await dry(client)).status_code == 200


async def test_book_pinned_leaves_keep_their_book_in_a_plan(
    client: httpx.AsyncClient, clean_db: str
) -> None:
    anchored(clean_db)
    held = add(clean_db, 1, vec=[0, 1, 0, 0])
    fixed = add(clean_db, 2, vec=[0, 0, 1, 0])
    free = add(clean_db, 3, vec=[0, 0, 0, 1])
    leaf(clean_db, held, 1, 40)
    sql(clean_db, "update collection_leaves set book_pinned = true where image_id = %s", held)
    leaf(clean_db, fixed, 1, 41, pinned=True)
    leaf(clean_db, free, 1, 42)
    plan = (await dry(client)).json()
    by = {m["image_id"]: m for m in plan["moves"]}
    assert fixed not in by
    assert held not in by or by[held]["after"][0] == 1
    assert by[free]["after"][0] == 4
    assert (await apply(client, plan)).status_code == 200
    got = places(clean_db)
    assert got[held][0] == 1 and got[fixed] == (1, 41)


async def test_explain_a_leaf(client: httpx.AsyncClient, clean_db: str) -> None:
    anchored(clean_db)
    a = add(clean_db, 1, vec=[0, 1, 0, 0])
    b = add(clean_db, 2, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 1, 1)
    leaf(clean_db, b, 1, 2)
    sql(
        clean_db,
        "insert into collection_leaf_history (image_id, collection_id, to_book, to_position,"
        " reason, chosen_by, scores) values (%s, %s, 1, 1, 'auto', 'score', %s)",
        a,
        cid(clean_db),
        '{"best": 1, "runner_up": 2, "margin": 0.25, "books": []}',
    )
    r = await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": a})
    assert r.status_code == 200
    got = r.json()
    assert (got["slug"], got["book"], got["position"]) == ("img-1", 1, 1)
    assert (got["pinned"], got["book_pinned"]) == (False, False)
    now = got["now"]
    books = {x["number"]: x for x in now["books"]}
    assert set(books) == set(range(1, 8))
    # Left out of book i, the leaf meets only the anchor and b there, both along axis 0,
    # and counts one leaf of fill, not two.
    assert books[1]["cos"] == pytest.approx(0.0)
    assert books[1]["fill"] == pytest.approx(-0.05 * (1 / 77) ** 2)
    assert books[2]["cos"] == pytest.approx(1.0) and books[2]["fill"] == 0.0
    for x in now["books"]:
        assert x["cos"] + x["tag"] + x["fill"] == pytest.approx(x["total"])
    assert (now["best"], now["runner_up"]) == (2, 6)
    assert now["margin"] == pytest.approx(0.0)
    assert now["lead"] == pytest.approx(books[1]["total"] - 1.0)
    assert got["filed"]["chosen_by"] == "score" and got["filed"]["scores"]["margin"] == 0.25
    assert (got["filed"]["book"], got["filed"]["position"]) == (1, 1)

    other = add(clean_db, 3, vec=[1, 0, 0, 0])
    r = await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": other})
    assert (r.status_code, r.json()) == (404, {"detail": "not here"})
    assert (await client.get(f"{ADM}/{SLUG}/explain", params={"image_id": 999})).status_code == 404
    assert (await client.get(f"{ADM}/nope/explain", params={"image_id": a})).status_code == 404


async def test_explain_reads_with_the_token(reader: httpx.AsyncClient, clean_db: str) -> None:
    (row,) = sql(
        clean_db,
        "insert into images (sha256, slug, status, published_at) values (%s, 'img-1', 'live',"
        " now()) returning id",
        sha(1),
    )
    leaf(clean_db, row[0], 2, 3)
    r = await reader.get(f"{ADM}/{SLUG}/explain", params={"image_id": row[0]}, headers=bearer())
    assert r.status_code == 200, r.text
    got = r.json()
    # No vector, so no scores now, and never filed through the filer.
    assert (got["book"], got["position"], got["now"], got["filed"]) == (2, 3, None, None)
    assert (
        await reader.get(f"{ADM}/{SLUG}/explain", params={"image_id": row[0]})
    ).status_code == 401


async def test_admin_picture_shows_collections(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    d = (await client.get(f"/api/admin/pictures/{a}")).json()["collections"]
    assert d == [
        {
            "slug": SLUG,
            "title": "Πανσοφικὸν τῶν πάντων",
            "abbrev_latin": "PTP",
            "book_count": 7,
            "leaves_per_book": 77,
            "loose_title": "incertae sedis",
            "leaf": None,
            "request": None,
        }
    ]
    leaf(clean_db, a, 6, 2, pinned=True)
    d = (await client.get(f"/api/admin/pictures/{a}")).json()["collections"]
    assert d[0]["leaf"] == {
        "book": 6,
        "position": 2,
        "pinned": True,
        "book_pinned": True,
        "note": None,
        "note_source": None,
        "note_model": None,
    }


# -- upload --------------------------------------------------------------------------


def _png() -> bytes:
    from tests.test_admin_pictures import png

    return png()


async def _upload(client: httpx.AsyncClient, data: dict[str, str]) -> httpx.Response:
    return await client.post(
        "/api/admin/upload", headers=XHR, files={"image": ("x.png", _png(), "image/png")}, data=data
    )


async def test_upload_writes_collection_sidecar(client: httpx.AsyncClient) -> None:
    inbox = get_settings().inbox
    r = await _upload(client, {"collection": SLUG, "book": "3", "leaf": "14"})
    stem = r.json()["stem"]
    assert (inbox / f"{stem}_collection.txt").read_text() == "pansophicon iii 14\n"
    r = await _upload(client, {"collection": SLUG, "book": "vii"})
    assert (inbox / f"{r.json()['stem']}_collection.txt").read_text() == "pansophicon vii\n"
    r = await _upload(client, {"collection": SLUG})
    assert (inbox / f"{r.json()['stem']}_collection.txt").read_text() == "pansophicon\n"
    r = await _upload(client, {})
    assert not (inbox / f"{r.json()['stem']}_collection.txt").exists()


async def test_upload_refuses_a_bad_collection(client: httpx.AsyncClient) -> None:
    assert (await _upload(client, {"collection": "two words here too"})).status_code == 422
    assert (await _upload(client, {"collection": SLUG, "leaf": "3"})).status_code == 422
    assert (await _upload(client, {"collection": SLUG, "book": "iiii"})).status_code == 422


async def test_admin_edits_owner_text(client: httpx.AsyncClient, clean_db: str) -> None:
    r = await client.patch(
        f"{ADM}/{SLUG}",
        json={
            "gloss": " The all-wise thing. ",
            "record": [
                ["extent", "one book"],
                ["provenance", " A shed.\n\n By descent. "],
                ["", ""],
            ],
            "vendor_text": "  ",
        },
    )
    d = r.json()
    assert d["gloss"] == "The all-wise thing."
    assert d["record"] == [["extent", "one book"], ["provenance", "A shed.\nBy descent."]]
    assert d["vendor_text"] is None
    bad = await client.patch(f"{ADM}/{SLUG}", json={"record": [["", "a value with no label"]]})
    assert bad.status_code == 422
    # Leaving a field out leaves it alone.
    d = (await client.patch(f"{ADM}/{SLUG}", json={"preamble": "x"})).json()
    assert d["gloss"] == "The all-wise thing." and len(d["record"]) == 2


async def test_admin_notes(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[0, 1, 0, 0])
    leaf(clean_db, a, 3, 14)
    leaf(clean_db, b, 3, 15)
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}/note", json={"note": " Vellum. "})
    got = r.json()["books"][2]["leaves"][0]
    assert (got["note"], got["note_source"]) == ("Vellum.", "owner")
    assert (await client.post(f"{ADM}/{SLUG}/leaves/{a}/note", headers=XHR)).status_code == 409
    # Missing notes: only b, which has none.
    r = await client.post(f"{ADM}/{SLUG}/notes", headers=XHR)
    assert r.json()["notes_queued"] == 1
    payloads = [
        x[0] for x in sql(clean_db, "select payload from jobs where type = 'collection_note'")
    ]
    assert payloads == [{"image_id": b, "collection_id": cid(clean_db), "rewrite": False}]
    # One leaf on demand is a rewrite, in its own key space.
    r = await client.post(f"{ADM}/{SLUG}/leaves/{b}/note", headers=XHR)
    assert r.status_code == 200 and r.json()["notes_queued"] == 2
    # The list says which leaf waits; a leaf holding two jobs is one leaf.
    leaves = r.json()["books"][2]["leaves"]
    assert [x["note_queued"] for x in leaves] == [False, True]
    # Clearing the owner's note gives the leaf back.
    r = await client.put(f"{ADM}/{SLUG}/leaves/{a}/note", json={"note": ""})
    got = r.json()["books"][2]["leaves"][0]
    assert (got["note"], got["note_source"]) == (None, None)
    assert (
        await client.put(f"{ADM}/{SLUG}/leaves/999/note", json={"note": "x"})
    ).status_code == 404
    assert (await client.post(f"{ADM}/{SLUG}/leaves/999/note", headers=XHR)).status_code == 404


async def test_upload_writes_note_sidecar_with_a_collection(client: httpx.AsyncClient) -> None:
    inbox = get_settings().inbox
    r = await _upload(client, {"collection": SLUG, "book": "3", "note": " Vellum.\nPatched. "})
    stem = r.json()["stem"]
    assert (inbox / f"{stem}_note.txt").read_text() == "Vellum.\nPatched.\n"
    # A note needs a leaf to belong to.
    r = await _upload(client, {"note": "Vellum."})
    assert not (inbox / f"{r.json()['stem']}_note.txt").exists()
    r = await _upload(client, {"collection": SLUG, "note": "  "})
    assert not (inbox / f"{r.json()['stem']}_note.txt").exists()
