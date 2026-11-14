"""Shared fixtures. One throwaway pgvector container per test session.

Set TEST_DATABASE_URL to reuse an existing database (CI uses a service container).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest

from penish.db import make_pool

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_UP = re.compile(r"--\s*migrate:up(.*?)(?:--\s*migrate:down|\Z)", re.S)


def _apply_migrations(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for f in sorted(MIGRATIONS.glob("*.sql")):
            m = _UP.search(f.read_text())
            if m:
                conn.execute(m.group(1).encode())


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    env = os.environ.get("TEST_DATABASE_URL")
    if env:
        _apply_migrations(env)
        yield env
        return
    name = f"penish-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=t",
            "-p",
            "127.0.0.1::5432",
            "pgvector/pgvector:pg17",
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "5432"], check=True, capture_output=True, text=True
            )
            .stdout.strip()
            .splitlines()[0]
            .rsplit(":", 1)[1]
        )
        dsn = f"postgresql://postgres:t@127.0.0.1:{port}/postgres"
        # pg_isready inside the container lies during the init restart; poll a real connect.
        for _ in range(60):
            try:
                psycopg.connect(dsn).close()
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        _apply_migrations(dsn)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def clean_db(database_url: str) -> str:
    """Empty every app table except the seed haikus, so tests stay independent."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        # DELETE, not TRUNCATE CASCADE: haikus references images, and a cascade
        # would wipe the seed deck along with them.
        conn.execute("delete from haikus where source <> 'seed'")
        conn.execute(
            "truncate jobs, corpus_stats, corpus_stats_history, settings, api_usage, heartbeats,"
            " picture_attention, view_days, candidate_edges, edge_state, mycelium_traffic,"
            " mycelium_ticks, mycelium_tunables, colony_ticks, colony_events, colony_members,"
            " colonies, image_facets, image_extracts, served_media, offers_needs restart identity"
        )
        # The singleton row back to how the migration leaves it, with a fixed seed so
        # a test that never sets one still runs the same organism every time.
        conn.execute("delete from mycelium_state")
        conn.execute("insert into mycelium_state (id, seed) values (1, 1)")
        conn.execute("delete from images")
    return database_url


@pytest.fixture
async def pool(clean_db: str) -> AsyncIterator[object]:
    p = make_pool(clean_db)
    await p.open()
    try:
        yield p
    finally:
        await p.close()
