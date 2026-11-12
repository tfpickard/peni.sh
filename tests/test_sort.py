from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import psycopg
import pytest

from penish.api import routes_images
from penish.sort import (
    SORTS,
    Row,
    anti_clump,
    clump,
    distances,
    drunkards_walk,
    hue,
    order,
    tidal,
)
from tests.test_api_images import add_image, client, unit  # noqa: F401

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh() -> None:
    routes_images._orders._items.clear()


def rows(n: int) -> list[Row]:
    # Newest first, as the route passes them.
    return [Row(i, T0 + timedelta(minutes=i), None, None) for i in range(n, 0, -1)]


def vec(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=np.float64)


# Two tight groups: a near the x axis, b near the y axis.
GROUPS = {
    5: vec(1, 0.05),
    4: vec(0.05, 1),
    3: vec(1, 0.1),
    2: vec(0.1, 1),
    1: vec(1, 0.0),
}


def test_every_order_is_a_permutation() -> None:
    rs = [Row(r.id, r.published_at, ["#ff0000"], 0.1 * r.id) for r in rows(5)]
    for s in SORTS:
        assert sorted(order(s, rs, GROUPS, seed="x")) == [1, 2, 3, 4, 5], s  # type: ignore[arg-type]


def test_clumped_keeps_groups_together() -> None:
    got = order("clumped", rows(5), GROUPS)
    assert got[0] == 5 and set(got[:3]) == {1, 3, 5}
    assert set(got[3:]) == {2, 4}


def test_anti_clumped_alternates() -> None:
    got = order("anti-clumped", rows(5), GROUPS)
    assert got[0] == 5 and got[1] in (2, 4)


def test_drifting_breaks_up_runs() -> None:
    # Newest three are one group; drifting must not show all three first.
    vs = {5: vec(1, 0), 4: vec(1, 0.01), 3: vec(1, 0.02), 2: vec(0, 1), 1: vec(0.01, 1)}
    got = order("drifting", rows(5), vs)
    assert got[0] == 5
    assert set(got[:3]) != {3, 4, 5}


def test_missing_vectors_follow_in_base_order() -> None:
    vs = {5: vec(1, 0), 3: vec(0, 1)}
    assert order("anti-clumped", rows(5), vs)[2:] == [4, 2, 1]
    assert order("oldest-drifting", rows(5), vs)[2:] == [1, 2, 4]


def test_seeded_orders_repeat_and_differ() -> None:
    a = order("random", rows(20), {}, seed="a")
    assert a == order("random", rows(20), {}, seed="a")
    assert a != order("random", rows(20), {}, seed="b")
    d = distances(np.eye(6))
    assert drunkards_walk(d, "s") == drunkards_walk(d, "s")


def test_lonely_puts_the_outlier_first() -> None:
    vs = {5: vec(1, 0), 4: vec(1, 0.01), 3: vec(1, 0.02), 2: vec(0, 1), 1: vec(1, 0.03)}
    assert order("lonely", rows(5), vs)[0] == 2


def test_tidal_and_simple_orders() -> None:
    assert tidal(5) == [0, 4, 1, 3, 2]
    assert order("oldest", rows(3), {}) == [1, 2, 3]
    rs = [Row(1, T0, None, 0.2), Row(2, T0, None, None), Row(3, T0, None, 0.9)]
    assert order("surprising", rs, {}) == [3, 1, 2]
    late = Row(1, datetime(2026, 1, 1, 23, tzinfo=UTC), None, None)
    early = Row(2, datetime(2026, 3, 1, 1, tzinfo=UTC), None, None)
    assert order("time-of-day", [late, early], {}) == [2, 1]


def test_rainbow_hues() -> None:
    assert hue(["#ff0000"]) < hue(["#00ff00"]) < hue(["#0000ff"]) < hue(["#808080"])
    assert hue(None) > hue(["#ffffff"])
    assert hue(["#f00"]) == hue(["#ff0000"])
    assert hue(["nonsense"]) == hue(None)


def test_small_inputs() -> None:
    assert clump(distances(vec(1, 0)[None, :])) == [0]
    assert anti_clump(distances(vec(1, 0)[None, :])) == [0]
    assert order("drifting", rows(1), {1: vec(1, 0)}) == [1]
    assert order("clumped", [], {}) == []


async def test_api_pages_a_computed_order(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    for n in range(1, 8):
        add_image(clean_db, n, vec=unit(n % 2))
    r = await client.get("/api/images", params={"sort": "oldest", "limit": 3})
    body = r.json()
    assert [i["slug"] for i in body["items"]] == ["img-1", "img-2", "img-3"]
    assert body["next_cursor"] == "3"
    r = await client.get("/api/images", params={"sort": "oldest", "limit": 3, "cursor": "6"})
    assert [i["slug"] for i in r.json()["items"]] == ["img-7"]
    assert r.json()["next_cursor"] is None

    seen: list[str] = []
    cursor = None
    while True:
        params = {"sort": "drunkards-walk", "seed": "abc", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/images", params=params)).json()
        seen += [i["slug"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(f"img-{n}" for n in range(1, 8))


async def test_api_order_follows_changes(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    for n in range(1, 4):
        add_image(clean_db, n)
    r = await client.get("/api/images", params={"sort": "oldest"})
    assert len(r.json()["items"]) == 3
    with psycopg.connect(clean_db) as conn:
        conn.execute("update images set status = 'failed', updated_at = now() where slug = 'img-2'")
        conn.commit()
    add_image(clean_db, 4)
    r = await client.get("/api/images", params={"sort": "oldest"})
    assert [i["slug"] for i in r.json()["items"]] == ["img-1", "img-3", "img-4"]


async def test_api_sort_with_tag_and_bad_input(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    for n in range(1, 4):
        add_image(clean_db, n)
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "insert into tags (image_id, tag, source) select id, 'x', 'model' from images "
            "where slug in ('img-1', 'img-3')"
        )
        conn.commit()
    r = await client.get("/api/images", params={"sort": "oldest", "tag": "x"})
    assert [i["slug"] for i in r.json()["items"]] == ["img-1", "img-3"]
    assert r.json()["count"] == 2
    assert (await client.get("/api/images", params={"sort": "sideways"})).status_code == 400
    bad = await client.get("/api/images", params={"sort": "oldest", "cursor": "x"})
    assert bad.status_code == 400
