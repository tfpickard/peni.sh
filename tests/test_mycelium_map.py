"""The map: `GET /api/mycelium` and the layout behind it.

Rows go straight into the network and colony tables, so each part of the payload is
exact. The layout itself is checked as a pure function at the end.
"""

from __future__ import annotations

from typing import Any

import httpx
import numpy as np
import psycopg
import pytest
from psycopg.rows import dict_row

from penish.api import routes_mycelium
from penish.config import get_settings
from penish.mycelium import layout, store
from tests.test_api_images import DIM, add_image, client  # noqa: F401

# A word that occurs nowhere else, so a hit can only be a phrase.
PHRASE_WORD = "quillobrank"


def sql(dsn: str, q: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(q.encode(), args)
        return cur.fetchall() if cur.description else []


def axis(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def cand(dsn: str, src: int, dst: int, affinity: float, layer: str = "whole") -> None:
    sql(
        dsn,
        "insert into candidate_edges (layer, src, dst, raw, affinity) values (%s, %s, %s, %s, %s)",
        layer,
        src,
        dst,
        affinity,
        affinity,
    )


def strength(dsn: str, src: int, dst: int, s: float, layer: str = "whole") -> None:
    sql(
        dsn,
        "insert into edge_state (layer, src, dst, strength, latched, fatigue, fused, idle)"
        " values (%s, %s, %s, %s, false, 0, false, 0)"
        " on conflict (layer, src, dst) do update set strength = excluded.strength",
        layer,
        src,
        dst,
        s,
    )


def gallery(dsn: str, cid: int, serial: int, state: str, members: list[int], **kw: Any) -> None:
    sql(
        dsn,
        "insert into colonies (id, layer, state, serial, title, born_tick, fruited_tick,"
        " died_tick, cause) values (%s, 'whole', %s, %s, %s, 0, 1, %s, %s)",
        cid,
        state,
        serial,
        kw.get("title"),
        kw.get("died"),
        kw.get("cause"),
    )
    for i in members:
        sql(
            dsn,
            "insert into colony_members (colony_id, image_id, slug, founder, joined_tick,"
            " left_tick) select %s, id, slug, true, 1, %s from images where id = %s",
            cid,
            kw.get("left"),
            i,
        )


def seed(dsn: str) -> tuple[list[int], int]:
    """Five live pictures in a chain of candidates, one unpublished picture wired into
    everything, one still queued. Visible edges on three layers, one in both directions."""
    ids = [add_image(dsn, n, caption=f"caption {n}", vec=axis(n)) for n in range(1, 6)]
    hidden = add_image(dsn, 50, status="unpublished", vec=axis(50))
    add_image(dsn, 60, status="queued")
    for a, b in zip(ids, ids[1:], strict=False):
        cand(dsn, a, b, 0.8)
        cand(dsn, b, a, 0.6)
    cand(dsn, ids[0], hidden, 0.9)
    cand(dsn, hidden, ids[0], 0.9)

    strength(dsn, ids[0], ids[1], 0.4)
    strength(dsn, ids[1], ids[0], 0.7)  # the stronger direction wins
    strength(dsn, ids[1], ids[2], 0.1)  # below hypha_visible
    strength(dsn, ids[2], ids[3], 0.5, "facet:mood")
    strength(dsn, ids[3], ids[4], 0.3, "need")
    strength(dsn, ids[0], hidden, 0.9)
    strength(dsn, hidden, ids[4], 0.9, "need")

    for i in [*ids, hidden]:
        sql(
            dsn,
            "insert into offers_needs (image_id, role, phrase, vec, model, dim)"
            " values (%s, 'need', %s, %s::vector, %s, %s)",
            i,
            f"a {PHRASE_WORD} {i}",
            str(axis(100)),
            get_settings().embed_model,
            DIM,
        )
    sql(dsn, "update mycelium_state set last_tick = 7, graph_version = 3 where id = 1")
    return ids, hidden


async def fetch(c: httpx.AsyncClient) -> tuple[dict[str, Any], httpx.Response]:
    r = await c.get("/api/mycelium")
    assert r.status_code == 200
    return r.json(), r


def positions(body: dict[str, Any]) -> dict[str, tuple[float, float]]:
    return {n["slug"]: (n["x"], n["y"]) for n in body["nodes"]}


async def test_shape(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    ids, hidden = seed(clean_db)
    gallery(clean_db, 1, 4, "alive", [ids[0], ids[1], hidden], title="a gallery")
    gallery(clean_db, 2, 5, "fading", [ids[1], ids[2]])
    gallery(clean_db, 3, 6, "dead", [ids[3]], died=5, cause="starved", left=5)
    gallery(clean_db, 4, None, "forming", [ids[4]])  # type: ignore[arg-type]

    body, _ = await fetch(client)
    assert set(body) == {"graph_version", "tick", "nodes", "edges", "galleries"}
    assert body["graph_version"] == 3 and body["tick"] == 7
    slugs = {sql(clean_db, "select slug from images where id = %s", i)[0][0]: i for i in ids}
    assert [n["slug"] for n in body["nodes"]] == list(slugs)
    for n in body["nodes"]:
        assert set(n) == {"slug", "x", "y", "overgrowth", "galleries"}
        assert 0 <= n["x"] <= 1 and 0 <= n["y"] <= 1
        assert 0 <= n["overgrowth"] <= 1
        # Galleries are not public yet: none are named, not even on the nodes.
        assert n["galleries"] == []
    assert body["galleries"] == []

    s = [f"img-{n}" for n in range(1, 6)]
    assert body["edges"] == [
        {"a": s[0], "b": s[1], "layer": "whole", "strength": 0.7},
        {"a": s[2], "b": s[3], "layer": "facet:mood", "strength": 0.5},
        {"a": s[3], "b": s[4], "layer": "need", "strength": 0.3},
    ]

    sql(clean_db, "update mycelium_state set public_since = now() where id = 1")
    body, _ = await fetch(client)
    assert body["galleries"] == [
        {"serial": 4, "title": "a gallery", "state": "living"},
        {"serial": 5, "title": "gallery 5", "state": "fading"},
    ]
    member = {n["slug"]: n["galleries"] for n in body["nodes"]}
    assert member == {s[0]: [4], s[1]: [4, 5], s[2]: [5], s[3]: [], s[4]: []}


async def test_no_phrases_and_no_unpublished(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    ids, hidden = seed(clean_db)
    gallery(clean_db, 1, 4, "alive", [ids[0], hidden])
    sql(clean_db, "update mycelium_state set public_since = now() where id = 1")
    _, r = await fetch(client)
    text = r.text
    assert PHRASE_WORD not in text
    assert "img-50" not in text and "img-60" not in text
    assert all(PHRASE_WORD not in v for v in r.headers.values())

    # Unpublished during a tick: the dirty mark the unpublish sets drops it at once.
    sql(clean_db, "update images set status = 'unpublished' where id = %s", ids[4])
    sql(clean_db, "update mycelium_state set graph_dirty = true where id = 1")
    body, _ = await fetch(client)
    assert "img-5" not in {n["slug"] for n in body["nodes"]}
    assert all("img-5" not in (e["a"], e["b"]) for e in body["edges"])


async def test_cache_per_tick(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    ids, _ = seed(clean_db)
    sql(
        clean_db,
        "update mycelium_state set running = true,"
        " next_due = now() + interval '5 minutes' where id = 1",
    )
    first, r = await fetch(client)
    age = int(r.headers["cache-control"].removeprefix("public, max-age="))
    assert 280 <= age <= 300

    # Within one tick the map stays as it was, whatever the tables do meanwhile.
    strength(clean_db, ids[0], ids[1], 0.95)
    again, _ = await fetch(client)
    assert again == first

    # The next tick shows it.
    sql(clean_db, "update mycelium_state set last_tick = 8 where id = 1")
    later, _ = await fetch(client)
    assert later["tick"] == 8
    assert later["edges"][0]["strength"] == 0.95

    # Behind schedule: nothing to keep. Stopped: a short while.
    sql(clean_db, "update mycelium_state set next_due = now() - interval '1 minute' where id = 1")
    _, r = await fetch(client)
    assert r.headers["cache-control"] == "public, max-age=0"
    sql(clean_db, "update mycelium_state set running = false where id = 1")
    _, r = await fetch(client)
    assert r.headers["cache-control"] == f"public, max-age={routes_mycelium.STOPPED_MAX_AGE}"


async def test_cache_follows_titles_and_tunables_while_stopped(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    """Titles and tunables change outside the tick, and a stopped network has no tick
    coming to carry them."""
    ids, _ = seed(clean_db)
    gallery(clean_db, 1, 4, "alive", [ids[0], ids[1]], title="old name")
    sql(clean_db, "update mycelium_state set running = false, public_since = now() where id = 1")
    body, _ = await fetch(client)
    assert body["galleries"][0]["title"] == "old name"
    assert len(body["edges"]) == 3

    sql(clean_db, "update colonies set title = 'new name', title_source = 'owner' where id = 1")
    body, _ = await fetch(client)
    assert body["galleries"][0]["title"] == "new name"
    assert (await client.get("/api/galleries/4")).json()["title"] == "new name"

    sql(clean_db, "update colonies set title = null, title_source = null where id = 1")
    body, _ = await fetch(client)
    assert body["galleries"][0]["title"] == "gallery 4"

    # hypha_visible raised past every edge but the strongest.
    sql(
        clean_db,
        """insert into mycelium_tunables ("values", note) values ('{"hypha_visible": 0.6}', 't')""",
    )
    body, _ = await fetch(client)
    assert [e["strength"] for e in body["edges"]] == [0.7]


async def test_reset_while_stopped_drops_the_kept_edges(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    """A public reset deletes edges and levels but keeps the tick, the graph, the live
    set and, with no gallery to kill, the titles; the dirty flag was set already."""
    seed(clean_db)
    sql(
        clean_db,
        "update mycelium_state set running = false, graph_dirty = true,"
        " public_since = now() where id = 1",
    )
    body, _ = await fetch(client)
    assert len(body["edges"]) == 3

    async with await psycopg.AsyncConnection.connect(
        clean_db, row_factory=dict_row, autocommit=True
    ) as conn:
        assert await store.reset(conn) == "public"
    body, _ = await fetch(client)
    assert body["edges"] == []
    assert body["tick"] == 7 and body["graph_version"] == 3


async def test_stopped_body_expires(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no tick coming, what changes with time alone (strength here stands for
    overgrowth, which moves with neglect and views) still reaches the map."""
    ids, _ = seed(clean_db)
    t = [1000.0]
    monkeypatch.setattr(routes_mycelium, "clock", lambda: t[0])
    sql(clean_db, "update mycelium_state set running = false where id = 1")
    first, _ = await fetch(client)
    assert first["edges"][0]["strength"] == 0.7

    strength(clean_db, ids[1], ids[0], 0.95)
    t[0] += routes_mycelium.STOPPED_MAX_AGE - 1
    body, _ = await fetch(client)
    assert body == first
    t[0] += 2
    body, _ = await fetch(client)
    assert body["edges"][0]["strength"] == 0.95

    # Behind schedule: no tick is coming either.
    sql(
        clean_db, "update mycelium_state set running = true, next_due = now() - interval '1 minute'"
    )
    first, _ = await fetch(client)
    strength(clean_db, ids[1], ids[0], 0.85)
    t[0] += routes_mycelium.STOPPED_MAX_AGE + 1
    body, _ = await fetch(client)
    assert body["edges"][0]["strength"] == 0.85

    # Running on schedule: kept for the tick, however long the clock says it has been.
    sql(clean_db, "update mycelium_state set next_due = now() + interval '5 minutes'")
    first, _ = await fetch(client)
    strength(clean_db, ids[1], ids[0], 0.75)
    t[0] += 10 * routes_mycelium.STOPPED_MAX_AGE
    body, _ = await fetch(client)
    assert body == first


async def test_layout_per_graph_version(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    ids, _ = seed(clean_db)
    first, _ = await fetch(client)
    pos = positions(first)
    assert len(set(pos.values())) == len(pos)

    # Ticks go by on the same graph: nothing moves.
    for t in (8, 9, 10):
        strength(clean_db, ids[2], ids[3], 0.2 + t / 100)
        sql(clean_db, "update mycelium_state set last_tick = %s where id = 1", t)
        body, _ = await fetch(client)
        assert positions(body) == pos

    # Forgotten and computed again (another process, a restart): the same places.
    routes_mycelium._layouts.clear()
    sql(clean_db, "update mycelium_state set last_tick = 11 where id = 1")
    body, _ = await fetch(client)
    assert positions(body) == pos

    # A rebuilt graph is a new layout.
    sql(clean_db, "delete from candidate_edges where layer = 'whole'")
    cand(clean_db, ids[0], ids[4], 0.9)
    cand(clean_db, ids[1], ids[3], 0.9)
    sql(clean_db, "update mycelium_state set graph_version = 4, last_tick = 12 where id = 1")
    body, _ = await fetch(client)
    assert body["graph_version"] == 4
    assert positions(body) != pos


async def test_empty(client: httpx.AsyncClient, clean_db: str) -> None:  # noqa: F811
    body, r = await fetch(client)
    assert body == {"graph_version": 0, "tick": None, "nodes": [], "edges": [], "galleries": []}
    assert r.headers["cache-control"] == f"public, max-age={routes_mycelium.STOPPED_MAX_AGE}"


# -- the layout, pure ----------------------------------------------------------------


def clustered(n: int, groups: int, seed: int = 0) -> tuple[list[int], list[tuple[int, int, float]]]:
    """Pictures in groups, each joined to the next few of its own group."""
    rng = np.random.default_rng(seed)
    label = {i: int(rng.integers(groups)) for i in range(1, n + 1)}
    edges = [
        (a, b, 0.5 + 0.5 * float(rng.random()))
        for a in label
        for b in label
        if a != b and label[a] == label[b] and abs(a - b) <= 3 * groups
    ]
    return list(label), edges


def test_layout_is_deterministic_and_in_the_square() -> None:
    ids, edges = clustered(60, 3)
    a = layout.force_layout(ids, edges, 7)
    b = layout.force_layout(list(reversed(ids)), list(reversed(edges)), 7)
    assert a == b
    assert set(a) == set(ids)
    xs = [p[0] for p in a.values()]
    ys = [p[1] for p in a.values()]
    assert min(xs) >= 0 and max(xs) <= 1 and min(ys) >= 0 and max(ys) <= 1
    # Aspect kept: one side spans the whole square.
    assert max(max(xs) - min(xs), max(ys) - min(ys)) == 1.0
    assert layout.force_layout(ids, edges, 8) != a
    assert layout.fingerprint(ids, edges, 7) == layout.fingerprint(ids[::-1], edges[::-1], 7)
    assert layout.fingerprint(ids, edges, 7) != layout.fingerprint(ids, edges[1:], 7)


def test_layout_pulls_neighbors_together() -> None:
    ids, edges = clustered(90, 3, seed=2)
    pos = layout.force_layout([*ids, 1000], edges, 1)
    joined = {(a, b) for a, b, _ in edges}
    near, far = [], []
    for a in ids:
        for b in ids:
            if a < b:
                d = float(np.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]))
                (near if (a, b) in joined else far).append(d)
    assert np.mean(near) < 0.5 * np.mean(far)
    # A picture with no edges stays on the map.
    assert 0 <= pos[1000][0] <= 1 and 0 <= pos[1000][1] <= 1


def test_layout_small() -> None:
    assert layout.force_layout([], [], 1) == {}
    assert layout.force_layout([5], [(5, 6, 1.0)], 1) == {5: (0.5, 0.5)}
    two = layout.force_layout([1, 2], [(1, 2, 1.0), (2, 1, 0.2), (1, 1, 1.0)], 1)
    assert set(two) == {1, 2} and two[1] != two[2]
