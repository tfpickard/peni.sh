import re
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest

from penish.api import attention, routes_search
from penish.mycelium import store
from penish.mycelium.tunables import DEFAULTS
from penish.worker import ingest
from tests.test_api_images import add_image, client, sha, unit  # noqa: F401

H = {"X-Requested-With": "penish"}
MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "006_attention.sql"
# 1 - e^-1: a picture unseen for exactly one time constant.
ONE_TAU = 0.632


@pytest.fixture(autouse=True)
def _fresh() -> None:
    attention.view_limit.clear()
    attention.click_limit.clear()
    routes_search.search_limit.clear()
    routes_search._vectors.clear()


async def view(c: httpx.AsyncClient, slug: str = "img-1", **kw: Any) -> httpx.Response:
    return await c.post(f"/api/images/{slug}/view", headers=H, **kw)


def attention_of(dsn: str, image_id: int) -> tuple[int, Any] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select views, last_viewed_at from picture_attention where image_id = %s",
            (image_id,),
        ).fetchone()
    return cast(tuple[int, Any] | None, row)


def today_views(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("select n from view_days where day = current_date").fetchone()
    return int(row[0]) if row else 0


def unseen_for(dsn: str, image_id: int, hours: float, neighbor: float = 0.0) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            insert into picture_attention (image_id, last_viewed_at, neighbor_level)
            values (%(id)s, now() - %(h)s * interval '1 hour', %(nl)s)
            on conflict (image_id) do update
               set last_viewed_at = excluded.last_viewed_at,
                   neighbor_level = excluded.neighbor_level
            """,
            {"id": image_id, "h": hours, "nl": neighbor},
        )
        conn.commit()


# -- the endpoint -----------------------------------------------------------


async def test_view_counts_and_returns_204(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    i = add_image(clean_db, 1)
    r = await view(client)
    assert r.status_code == 204
    assert r.content == b""
    got = attention_of(clean_db, i)
    assert got is not None and got[0] == 1
    assert today_views(clean_db) == 1


async def test_requires_header(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    add_image(clean_db, 1)
    assert (await client.post("/api/images/img-1/view")).status_code == 403
    r = await client.post("/api/images/img-1/view", headers={"X-Requested-With": "x"})
    assert r.status_code == 403
    assert today_views(clean_db) == 0


async def test_unknown_and_not_live_are_404(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    add_image(clean_db, 1, status="queued")
    for slug in ("img-1", "nope"):
        r = await view(client, slug)
        assert r.status_code == 404
        assert r.json() == {"detail": "not here"}
    assert today_views(clean_db) == 0


async def test_rate_limit(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    add_image(clean_db, 1)
    for _ in range(attention.VIEW_LIMIT):
        assert (await view(client)).status_code == 204
    r = await view(client)
    assert r.status_code == 429
    assert r.json() == {"detail": "wait"}
    # Hammering one picture counts it once.
    assert today_views(clean_db) == 1


async def test_one_view_per_picture_per_minute(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    i = add_image(clean_db, 1)
    add_image(clean_db, 2)
    assert (await view(client)).status_code == 204
    first = attention_of(clean_db, i)
    assert (await view(client)).status_code == 204
    assert attention_of(clean_db, i) == first
    assert today_views(clean_db) == 1
    # Another picture is its own minute.
    assert (await view(client, "img-2")).status_code == 204
    assert today_views(clean_db) == 2
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "update picture_attention set last_viewed_at = now() - interval '61 seconds'"
            " where image_id = %s",
            (i,),
        )
        conn.commit()
    assert (await view(client)).status_code == 204
    got = attention_of(clean_db, i)
    assert got is not None and got[0] == 2
    assert today_views(clean_db) == 3
    with psycopg.connect(clean_db) as conn:
        moved = conn.execute(
            "select now() - last_viewed_at < interval '5 seconds' from picture_attention"
            " where image_id = %s",
            (i,),
        ).fetchone()
    assert moved == (True,)


async def test_from_is_validated(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    add_image(clean_db, 1)
    for ok in ({"from": "img-2"}, {"from": None}, {}):
        assert (await view(client, json=ok)).status_code == 204, ok
    for bad in ({"from": "Not A Slug"}, {"from": "x" * 101}, {"from": 5}, {"from": ""}):
        assert (await view(client, json=bad)).status_code == 422, bad


async def test_updated_at_untouched(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    i = add_image(clean_db, 1)
    with psycopg.connect(clean_db) as conn:
        before = conn.execute("select updated_at from images where id = %s", (i,)).fetchone()
    assert (await view(client)).status_code == 204
    with psycopg.connect(clean_db) as conn:
        after = conn.execute("select updated_at from images where id = %s", (i,)).fetchone()
    assert before == after


async def test_nothing_about_the_visitor(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    add_image(clean_db, 1)
    ip = "203.0.113.77"
    await client.post("/api/images/img-1/view", headers={**H, "CF-Connecting-IP": ip})
    with psycopg.connect(clean_db) as conn:
        cols = conn.execute(
            "select table_name, column_name from information_schema.columns"
            " where table_name in ('picture_attention', 'view_days')"
        ).fetchall()
        dump = str(conn.execute("select * from picture_attention").fetchall()) + str(
            conn.execute("select * from view_days").fetchall()
        )
    by: dict[str, set[str]] = {}
    for t, c in cols:
        by.setdefault(t, set()).add(c)
    assert by == {
        "picture_attention": {"image_id", "views", "last_viewed_at", "neighbor_level"},
        "view_days": {"day", "n"},
    }
    assert not any(re.search(r"ip|voter|visitor|cookie|agent", c) for _, c in cols)
    assert ip not in dump


# -- overgrowth in payloads -------------------------------------------------


def _values(items: list[dict[str, Any]]) -> list[float]:
    return [it["overgrowth"] for it in items]


async def test_overgrowth_on_every_tile(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from penish.api import embed_client

    for n in range(1, 4):
        add_image(clean_db, n, vec=unit(n - 1))
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "insert into image_categories (image_id, category, source)"
            " select id, 'cartoon', 'model' from images"
        )
        conn.execute(
            "insert into tags (image_id, tag, source) select id, 'cup', 'model' from images"
        )
        conn.commit()

    async def fake(text: str, **kw: Any) -> dict[str, Any]:
        return {"vector": unit(0), "model": None, "dim": len(unit(0))}

    monkeypatch.setattr(embed_client, "embed_query_full", fake)

    async def surfaces(detail_of: str) -> dict[str, list[dict[str, Any]]]:
        detail = (await client.get(f"/api/images/{detail_of}")).json()
        random = await client.get("/api/images", params={"sort": "random", "seed": "s"})
        return {
            "newest": (await client.get("/api/images")).json()["items"],
            "random": random.json()["items"],
            "search": (await client.get("/api/search", params={"q": "cup"})).json()["items"],
            "covers": [
                c["cover"]
                for c in (await client.get("/api/categories")).json()["categories"]
                if c["cover"]
            ],
            "strips": detail["similar"] + detail["dissimilar"],
        }

    # Published months ago and never opened: fully grown over, on every surface.
    for name, items in (await surfaces("img-1")).items():
        assert items, name
        for v in _values(items):
            assert isinstance(v, float), name
        assert set(_values(items)) == {1.0}, name

    # One view clears exactly that picture, wherever it shows up.
    assert (await view(client)).status_code == 204
    after = await surfaces("img-2")
    for name, items in after.items():
        assert items, name
        assert {it["slug"]: it["overgrowth"] for it in items} == {
            it["slug"]: 0.0 if it["slug"] == "img-1" else 1.0 for it in items
        }, name
    for name in ("newest", "random", "search", "strips"):
        assert "img-1" in {it["slug"] for it in after[name]}, name


async def test_fresh_view_clears_overgrowth(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    # Every real picture already has a row (backfill or go-live), so the view below
    # goes through the conflict branch of the upsert, not the insert.
    a = add_image(clean_db, 1)
    b = add_image(clean_db, 2)
    unseen_for(clean_db, a, 10_000)
    unseen_for(clean_db, b, 10_000)
    by = {i["slug"]: i["overgrowth"] for i in (await client.get("/api/images")).json()["items"]}
    assert by == {"img-1": 1.0, "img-2": 1.0}
    assert (await view(client)).status_code == 204
    by = {i["slug"]: i["overgrowth"] for i in (await client.get("/api/images")).json()["items"]}
    assert by == {"img-1": 0.0, "img-2": 1.0}


# -- rows created up front --------------------------------------------------


def test_migration_backfills_without_overwriting(clean_db: str) -> None:
    up = re.search(r"--\s*migrate:up(.*?)--\s*migrate:down", MIGRATION.read_text(), re.S)
    assert up
    with psycopg.connect(clean_db, autocommit=True) as conn:
        ids = [
            conn.execute(
                "insert into images (sha256, status) values (%s, %s) returning id", (sha(n), s)
            ).fetchone()[0]  # type: ignore[index]
            for n, s in ((1, "live"), (2, "queued"), (3, "failed"))
        ]
        conn.execute(
            "insert into picture_attention (image_id, views, last_viewed_at)"
            " values (%s, 7, now() - interval '9 days')",
            (ids[1],),
        )
        # 006 is already applied by the fixture; its up is safe to run again.
        conn.execute(up.group(1).encode())
        rows = conn.execute(
            "select image_id, views, now() - last_viewed_at < interval '1 minute'"
            " from picture_attention order by image_id"
        ).fetchall()
    # Every picture gets a fresh row; one that already had a row keeps it untouched.
    assert rows == [(ids[0], 0, True), (ids[1], 7, False), (ids[2], 0, True)]


async def test_go_live_inserts_attention(pool: object) -> None:
    p = cast(Any, pool)
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, status) values (%s, 'processing') returning id", (sha(1),)
        )
        image_id = (await cur.fetchone())["id"]
        await conn.commit()
        assert await ingest._go_live(conn, image_id, "a-cup", None)
        cur = await conn.execute(
            "select views, now() - last_viewed_at < interval '1 minute' as fresh"
            " from picture_attention where image_id = %s",
            (image_id,),
        )
        assert await cur.fetchone() == {"views": 0, "fresh": True}
        # Already live: nothing new, and the row is not reset.
        await conn.execute(
            "update picture_attention set views = 3 where image_id = %s", (image_id,)
        )
        await conn.commit()
        assert not await ingest._go_live(conn, image_id, "a-cup", None)
        cur = await conn.execute(
            "select views from picture_attention where image_id = %s", (image_id,)
        )
        assert await cur.fetchone() == {"views": 3}


async def test_go_live_resets_a_stale_row(pool: object, clean_db: str) -> None:
    p = cast(Any, pool)
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, status) values (%s, 'queued') returning id", (sha(1),)
        )
        image_id = (await cur.fetchone())["id"]
        await conn.commit()
    # A row left from before it went live, as after a week in pending_enrichment.
    unseen_for(clean_db, image_id, 24 * 7)
    with psycopg.connect(clean_db) as conn:
        conn.execute("update picture_attention set views = 4 where image_id = %s", (image_id,))
        conn.commit()
    async with p.connection() as conn:
        assert await ingest._go_live(conn, image_id, "fresh", None)
        await conn.commit()
    assert (await _og(p, [image_id]))[image_id] < 0.01
    got = attention_of(clean_db, image_id)
    assert got is not None and got[0] == 4


# -- overgrowth_by_id -------------------------------------------------------


async def _img(p: Any, n: int, status: str = "live", published: bool = True) -> int:
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into images (sha256, status, published_at) values (%s, %s, %s) returning id",
            (sha(n), status, "2026-01-01" if published else None),
        )
        row = await cur.fetchone()
        await conn.commit()
    return int(row["id"])


async def _og(p: Any, ids: list[int]) -> dict[int, float]:
    async with p.connection() as conn:
        return await store.overgrowth_by_id(conn, ids)


async def test_og_empty_and_unknown(pool: object) -> None:
    p = cast(Any, pool)
    assert await _og(p, []) == {}
    assert await _og(p, [12345]) == {}


async def test_og_no_views_small_gallery_uses_floor(pool: object, clean_db: str) -> None:
    p = cast(Any, pool)
    a = await _img(p, 1)
    # One live picture, no views: 2 * 1 / 1 * 24 = 48 h, below the 72 h floor.
    unseen_for(clean_db, a, DEFAULTS.overgrowth_tau_min_h)
    assert (await _og(p, [a]))[a] == ONE_TAU


async def test_og_no_live_pictures(pool: object, clean_db: str) -> None:
    p = cast(Any, pool)
    a = await _img(p, 1, status="queued")
    b = await _img(p, 2, status="queued", published=False)
    unseen_for(clean_db, a, DEFAULTS.overgrowth_tau_min_h)
    # No attention row and never published: nothing to have neglected.
    assert await _og(p, [a, b]) == {a: ONE_TAU, b: 0.0}


async def test_og_tau_grows_with_gallery_and_shrinks_with_pace(pool: object, clean_db: str) -> None:
    p = cast(Any, pool)
    ids = [await _img(p, n) for n in range(1, 6)]
    a = ids[0]
    # Five live pictures, no views: tau = 2 * 5 * 24 = 240 h.
    unseen_for(clean_db, a, 240)
    assert (await _og(p, [a]))[a] == ONE_TAU
    with psycopg.connect(clean_db) as conn:
        # Seven days ago is outside the week: tau stays 240 h rather than the floor.
        conn.execute("insert into view_days (day, n) values (current_date - 7, 100000)")
        conn.commit()
    assert (await _og(p, [a]))[a] == ONE_TAU
    with psycopg.connect(clean_db) as conn:
        # Six days ago is inside: 70 views over the week is ten a day, tau would be
        # 2 * 5 / 10 * 24 = 24 h, so the 72 h floor holds.
        conn.execute("insert into view_days (day, n) values (current_date - 6, 70)")
        conn.commit()
    unseen_for(clean_db, a, 72)
    assert (await _og(p, [a]))[a] == ONE_TAU


async def test_og_neighbor_spread_and_clamp(pool: object, clean_db: str) -> None:
    p = cast(Any, pool)
    a = await _img(p, 1)
    b = await _img(p, 2)
    unseen_for(clean_db, a, 72, neighbor=1.0)
    unseen_for(clean_db, b, 10_000, neighbor=1.0)
    with psycopg.connect(clean_db) as conn:
        conn.execute("insert into view_days (day, n) values (current_date, 1000)")
        conn.commit()
    got = await _og(p, [a, b])
    assert got[a] == round((1 - 2.718281828459045**-1) * (1 + DEFAULTS.overgrowth_sigma), 3)
    assert got[b] == 1.0


# -- clicks become traffic --------------------------------------------------


def organism(dsn: str, *, running: bool, edges: list[tuple[int, int]]) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("update mycelium_state set running = %s", (running,))
        for src, dst in edges:
            conn.execute(
                "insert into candidate_edges (layer, src, dst, raw, affinity)"
                " values ('whole', %s, %s, 0.5, 0.5)",
                (src, dst),
            )
        conn.commit()


def traffic(dsn: str) -> list[tuple[int, int]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("select src, dst from mycelium_traffic order by id").fetchall()
    return [(int(a), int(b)) for a, b in rows]


async def test_click_needs_a_candidate_edge(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b, c = (add_image(clean_db, n) for n in (1, 2, 3))
    add_image(clean_db, 4, status="queued")
    organism(clean_db, running=True, edges=[(a, b)])
    for slug, src in (
        ("img-2", "img-1"),  # the candidate
        ("img-3", "img-1"),  # no edge
        ("img-1", "img-2"),  # the reverse is not an edge
        ("img-2", "img-2"),  # itself
        ("img-2", "nope"),  # unknown
        ("img-2", "img-4"),  # not live
    ):
        r = await view(client, slug, json={"from": src})
        assert r.status_code == 204, (slug, src)
    assert traffic(clean_db) == [(a, b)]
    # No `from`, no click; the view still counts.
    assert (await view(client, "img-3")).status_code == 204
    assert traffic(clean_db) == [(a, b)]
    got = attention_of(clean_db, c)
    assert got is not None and got[0] == 1


async def test_click_from_an_old_slug(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b = (add_image(clean_db, n) for n in (1, 2))
    with psycopg.connect(clean_db) as conn:
        conn.execute("insert into slug_aliases (slug, image_id) values ('old-one', %s)", (a,))
        conn.commit()
    organism(clean_db, running=True, edges=[(a, b)])
    assert (await view(client, "img-2", json={"from": "old-one"})).status_code == 204
    assert traffic(clean_db) == [(a, b)]


async def test_clicks_dropped_while_stopped(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b = (add_image(clean_db, n) for n in (1, 2))
    organism(clean_db, running=False, edges=[(a, b)])
    assert (await view(client, "img-2", json={"from": "img-1"})).status_code == 204
    assert traffic(clean_db) == []
    # Views still count while the organism sleeps.
    got = attention_of(clean_db, b)
    assert got is not None and got[0] == 1
    assert today_views(clean_db) == 1


async def test_click_budget_per_client(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    a, b, _ = (add_image(clean_db, n) for n in (1, 2, 3))
    organism(clean_db, running=True, edges=[(a, b)])
    # Browsing that is not a click never spends the budget.
    for _ in range(attention.CLICK_LIMIT + 2):
        assert (await view(client, "img-3", json={"from": "img-1"})).status_code == 204
    for _ in range(attention.CLICK_LIMIT + 3):
        assert (await view(client, "img-2", json={"from": "img-1"})).status_code == 204
    assert traffic(clean_db) == [(a, b)] * attention.CLICK_LIMIT
    # Another reader has a budget of their own.
    other = {"CF-Connecting-IP": "198.51.100.9"}
    r = await client.post("/api/images/img-2/view", headers={**H, **other}, json={"from": "img-1"})
    assert r.status_code == 204
    assert len(traffic(clean_db)) == attention.CLICK_LIMIT + 1


async def test_traffic_stores_nothing_about_the_visitor(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a, b = (add_image(clean_db, n) for n in (1, 2))
    organism(clean_db, running=True, edges=[(a, b)])
    ip = "203.0.113.77"
    await client.post(
        "/api/images/img-2/view",
        headers={**H, "CF-Connecting-IP": ip},
        json={"from": "img-1"},
    )
    with psycopg.connect(clean_db) as conn:
        cols = conn.execute(
            "select column_name from information_schema.columns"
            " where table_name = 'mycelium_traffic'"
        ).fetchall()
        dump = str(conn.execute("select * from mycelium_traffic").fetchall())
    assert {c for (c,) in cols} == {"id", "src", "dst", "created_at"}
    assert traffic(clean_db) == [(a, b)]
    assert ip not in dump
