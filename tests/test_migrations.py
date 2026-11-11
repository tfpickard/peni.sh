import re
from pathlib import Path

import psycopg
import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "004_analysis.sql"


def _section(name: str) -> str:
    text = MIGRATION.read_text()
    m = re.search(rf"--\s*migrate:{name}(.*?)(?:--\s*migrate:\w+|\Z)", text, re.S)
    assert m
    return m.group(1)


def test_analysis_copies_descriptions(clean_db: str) -> None:
    with psycopg.connect(clean_db, autocommit=True) as conn:
        ids = [
            conn.execute(
                "insert into images (sha256, status) values (%s, 'live') returning id",
                (f"{n:064x}",),
            ).fetchone()[0]  # type: ignore[index]
            for n in (1, 2)
        ]
        conn.execute(
            "insert into image_texts (image_id, field, body, source, model) values"
            " (%s, 'description', 'Subject: a cup.', 'model', 'm1'),"
            " (%s, 'caption', 'a cup', 'model', 'm1'),"
            " (%s, 'description', 'Subject: mine.', 'owner', null)",
            (ids[0], ids[0], ids[1]),
        )
        # 004 is already applied by the fixture; its up is safe to run again.
        conn.execute(_section("up").encode())
        rows = conn.execute(
            "select image_id, body, source, model from image_texts"
            " where field = 'analysis' order by image_id"
        ).fetchall()
        assert rows == [
            (ids[0], "Subject: a cup.", "model", "m1"),
            (ids[1], "Subject: mine.", "owner", None),
        ]

        try:
            conn.execute(_section("down").encode())
            assert conn.execute(
                "select count(*) from image_texts where field = 'analysis'"
            ).fetchone() == (0,)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "insert into image_texts (image_id, field, body, source)"
                    " values (%s, 'analysis', 'x', 'owner')",
                    (ids[0],),
                )
        finally:
            conn.execute(_section("up").encode())
