import psycopg


def test_seed_haikus_present(clean_db: str) -> None:
    with psycopg.connect(clean_db) as conn:
        assert conn.execute("select count(*) from haikus").fetchone()[0] == 40  # type: ignore[index]
