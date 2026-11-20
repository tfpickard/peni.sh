"""Collections over http: the public list, collection and book, the picture's leaf line,
the admin surface, and the upload form's collection fields."""

from __future__ import annotations

import httpx

from penish.config import get_settings
from tests import test_admin_pictures
from tests.test_admin_pictures import XHR, add, sql

# The admin pictures client: logged in, with a 4-wide test embedding space.
client = test_admin_pictures.client

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
    assert d["published"] is False and d["tuning"]["anchor_k"] == 3.0
    assert d["books"][0]["anchor_text"].startswith("Engraved or painted plates")
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
        }
    ]


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


async def test_admin_resort_dry_run_and_real(client: httpx.AsyncClient, clean_db: str) -> None:
    a = add(clean_db, 1, vec=[1, 0, 0, 0])
    b = add(clean_db, 2, vec=[1, 0, 0, 0])
    leaf(clean_db, a, 1, 40)
    leaf(clean_db, b, 1, 41)
    r = await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": True})
    moves = r.json()["moves"]
    assert {m["slug"] for m in moves} == {"img-1", "img-2"}
    assert sql(clean_db, "select count(*) from collection_leaf_history") == [(0,)]
    r = await client.post(f"{ADM}/{SLUG}/resort", headers=XHR, json={"dry_run": False})
    assert r.json()["resort"]["status"] == "queued"
    assert sql(clean_db, "select type from jobs") == [("collection_resort",)]


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
