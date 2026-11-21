"""Migration 023: the Pansophicon's books rewritten, and only where the owner has not."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.conftest import _UP, COLLECTION_SEEDS, MIGRATIONS

MIGRATION = MIGRATIONS / "023_pansophicon_of_man.sql"
ANCHORS = Path(__file__).resolve().parent.parent / "docs" / "ptp_anchors.json"
NUMERALS = ("i", "ii", "iii", "iv", "v", "vi", "vii")
MEDIUM_WORDS = (
    "parchment",
    "engraved",
    "engraving",
    "woodcut",
    "mosaic",
    "fresco",
    "lapis",
    "gouache",
    "tempera",
    "etching",
    "manuscript",
)
Books = dict[int, tuple[str, list[str]]]


def section(name: str) -> bytes:
    m = re.search(rf"--\s*migrate:{name}(.*?)(?:--\s*migrate:\w+|\Z)", MIGRATION.read_text(), re.S)
    assert m
    return m.group(1).encode()


def new_books() -> Books:
    raw = json.loads(ANCHORS.read_text())
    return {n: (raw[r]["anchor"], raw[r]["hints"]) for n, r in enumerate(NUMERALS, 1)}


def books(conn: psycopg.Connection[Any]) -> Books:
    rows = conn.execute(
        "select b.number, b.anchor_text, b.tag_hints from collection_books b"
        " join collections c on c.id = b.collection_id where c.slug = 'pansophicon'"
        " order by b.number"
    ).fetchall()
    return {n: (t, h) for n, t, h in rows}


def vectors(conn: psycopg.Connection[Any]) -> dict[int, tuple[Any, Any, Any]]:
    rows = conn.execute(
        "select b.number, b.anchor_embedding is not null, b.anchor_model, b.anchor_dim"
        " from collection_books b join collections c on c.id = b.collection_id"
        " where c.slug = 'pansophicon' order by b.number"
    ).fetchall()
    return {n: (e, m, d) for n, e, m, d in rows}


def pre_023(conn: psycopg.Connection[Any]) -> Books:
    """The collection as 018 and 020 leave it, which is what 023 runs against on a deploy."""
    conn.execute("delete from collections")
    for seed in COLLECTION_SEEDS[:2]:
        m = _UP.search(seed.read_text())
        assert m
        conn.execute(m.group(1).encode())
    return books(conn)


def embed_all(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "update collection_books set anchor_embedding = '[1,2,3]'::vector,"
        " anchor_model = 'm', anchor_dim = 3"
    )


def anchor_k(conn: psycopg.Connection[Any]) -> Any:
    return conn.execute("select tuning from collections where slug = 'pansophicon'").fetchone()[0]  # type: ignore[index]


def test_fresh_database_carries_the_new_books(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        assert books(conn) == new_books()
        # No vector, so the worker's sweep embeds all seven.
        assert vectors(conn) == {n: (False, None, None) for n in range(1, 8)}
        assert anchor_k(conn) == {"anchor_k": 10}


def test_texts_equal_the_file_the_report_reads(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        got = books(conn)
    want = json.loads(ANCHORS.read_text())
    assert list(want) == list(NUMERALS)
    for n, r in enumerate(NUMERALS, 1):
        assert got[n][0] == want[r]["anchor"]
        assert got[n][1] == want[r]["hints"]


def test_no_medium_words_in_the_new_anchors() -> None:
    for n, (text, hints) in new_books().items():
        for word in MEDIUM_WORDS:
            assert not re.search(rf"\b{word}\b", text, re.I), (n, word)
            assert word not in [h.lower() for h in hints], (n, word)


def test_a_hand_edited_book_is_left_alone(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        old = pre_023(conn)
        assert old != new_books()
        conn.execute("update collection_books set anchor_text = 'mine' where number = 2")
        conn.execute("update collection_books set tag_hints = array['mine'] where number = 5")
        embed_all(conn)
        conn.execute(section("up"))

        got = books(conn)
        want = new_books()
        for n in (1, 3, 4, 6, 7):
            assert got[n] == want[n]
        # Edited in the anchor only, and in the hints only: each is skipped whole.
        assert got[2] == ("mine", old[2][1])
        assert got[5] == (old[5][0], ["mine"])
        assert vectors(conn) == {
            n: (True, "m", 3) if n in (2, 5) else (False, None, None) for n in range(1, 8)
        }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({}, {"anchor_k": 10}),
        ({"anchor_k": 3}, {"anchor_k": 10}),
        ({"anchor_k": 3.0}, {"anchor_k": 10}),
        ({"anchor_k": None}, {"anchor_k": 10}),
        ({"anchor_k": 6}, {"anchor_k": 6}),
        ({"anchor_k": 10}, {"anchor_k": 10}),
        ({"anchor_k": 0}, {"anchor_k": 0}),
        ({"anchor_k": "3"}, {"anchor_k": "3"}),
        ({"tag_alpha": 0.2, "weekly_resort": True}, {"tag_alpha": 0.2, "weekly_resort": True}),
    ],
)
def test_anchor_k_moves_only_from_the_default(
    clean_db: str, before: dict[str, Any], after: dict[str, Any]
) -> None:
    other = {"tag_alpha": 0.2, "book_margin": 0.1, "weekly_resort": True}
    with psycopg.connect(clean_db, autocommit=True) as conn:
        pre_023(conn)
        conn.execute(
            "update collections set tuning = %s::jsonb where slug = 'pansophicon'",
            (json.dumps({**other, **before}),),
        )
        conn.execute(section("up"))
        expected = {**other, **after}
        if "anchor_k" not in before:
            expected = {**other, "anchor_k": 10, **after}
        assert anchor_k(conn) == expected


def test_titles_leaves_pins_and_other_collections_are_untouched(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        pre_023(conn)
        conn.execute("update collection_books set title = 'Of Beasts' where number = 1")
        conn.execute("update collection_books set title = 'Of Man' where number = 7")
        conn.execute("update collections set published = true, gloss = 'g', preamble = 'p'")
        ids = [
            conn.execute(
                "insert into images (sha256, status) values (%s, 'live') returning id",
                (f"{n:064x}",),
            ).fetchone()[0]  # type: ignore[index]
            for n in (1, 2, 3)
        ]
        cid = conn.execute("select id from collections where slug = 'pansophicon'").fetchone()[0]  # type: ignore[index]
        for image_id, book, pos, bp, p, note in (
            (ids[0], 4, 1, True, True, "Hand A."),
            (ids[1], 4, 2, True, False, None),
            (ids[2], 7, 1, False, False, "Hand B."),
        ):
            conn.execute(
                "insert into collection_leaves (image_id, collection_id, book, position,"
                " book_pinned, pinned, note, note_source) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (image_id, cid, book, pos, bp, p, note, "owner" if note else None),
            )
        # A second collection whose books carry the very seed text: the slug guard alone
        # keeps it out.
        conn.execute(
            "insert into collections (slug, title, abbrev, abbrev_latin, book_count,"
            " leaves_per_book, tuning) values ('other', 't', 'a', 'a', 7, 7, '{\"anchor_k\": 3}')"
        )
        conn.execute(
            "insert into collection_books (collection_id, number, title, anchor_text, tag_hints)"
            " select o.id, b.number, b.title, b.anchor_text, b.tag_hints from collections o,"
            " collection_books b where o.slug = 'other' and b.collection_id = %s",
            (cid,),
        )

        probe = (
            "select md5(coalesce(string_agg(r::text, '|' order by r::text), '')), count(*)"
            " from ({}) r"
        )
        queries = [
            "select collection_id, number, title from collection_books",
            "select * from collection_leaves",
            "select id, slug, title, abbrev, preamble, book_count, leaves_per_book, published,"
            " gloss, record, vendor_text, last_resort_at from collections",
            "select * from collection_leaf_history",
            "select * from collection_requests",
            "select b.* from collection_books b join collections c on c.id = b.collection_id"
            " where c.slug = 'other'",
            "select tuning from collections where slug = 'other'",
        ]
        before = [conn.execute(probe.format(q)).fetchone() for q in queries]
        assert before[1][1] == 3  # type: ignore[index]
        conn.execute(section("up"))
        assert [conn.execute(probe.format(q)).fetchone() for q in queries] == before
        conn.execute(section("down"))
        assert [conn.execute(probe.format(q)).fetchone() for q in queries] == before


def test_down_restores_unedited_books_and_spares_later_edits(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        old = pre_023(conn)
        conn.execute(section("up"))
        assert books(conn) == new_books()
        conn.execute("update collection_books set anchor_text = 'later' where number = 3")
        conn.execute("update collection_books set tag_hints = array['later'] where number = 6")
        embed_all(conn)
        conn.execute(section("down"))

        got = books(conn)
        for n in (1, 2, 4, 5, 7):
            assert got[n] == old[n]
        assert got[3] == ("later", new_books()[3][1])
        assert got[6] == (new_books()[6][0], ["later"])
        assert vectors(conn) == {
            n: (True, "m", 3) if n in (3, 6) else (False, None, None) for n in range(1, 8)
        }
        assert anchor_k(conn) == {"anchor_k": 3}


def test_down_leaves_an_owner_anchor_k_alone(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        pre_023(conn)
        conn.execute("update collections set tuning = '{\"anchor_k\": 6}'")
        conn.execute(section("up"))
        conn.execute(section("down"))
        assert anchor_k(conn) == {"anchor_k": 6}
