"""Galleries in public: list, detail and tombstone, the log, image detail, /g/<n>.

Colony rows are written straight into the projection tables so each shape is
exact; the last tests run real ticks to check what the projection itself writes.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest

from penish.config import get_settings
from penish.db import make_pool
from penish.mycelium import genesis, store, tick
from tests.test_api_images import add_image, client, sha  # noqa: F401

STEP = timedelta(minutes=10)
GRID_KEYS = {"slug", "width", "height", "blurhash", "palette", "variants", "overgrowth"}


def sql(dsn: str, q: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(q.encode(), args)
        return cur.fetchall() if cur.description else []


def ticks(dsn: str, n: int, start: datetime) -> None:
    for k in range(n):
        sql(
            dsn,
            "insert into mycelium_ticks (tick, scheduled_at, simulated, state_hash,"
            " colonies_hash, stats) values (%s, %s, false, 'h', 'h', '{}')",
            k,
            start + k * STEP,
        )


def public(dsn: str) -> None:
    sql(dsn, "update mycelium_state set public_since = now() where id = 1")


def colony(
    dsn: str,
    cid: int,
    serial: int | None,
    state: str,
    *,
    members: tuple[int, ...] = (),
    founders: tuple[int, ...] = (),
    layer: str = "whole",
    born: int = 0,
    fruited: int | None = 1,
    faded: int | None = None,
    died: int | None = None,
    cause: str | None = None,
    parent: int | None = None,
    absorbed_by: int | None = None,
    cover: int | None = None,
    peak_members: list[dict[str, Any]] | None = None,
    title: str | None = None,
) -> None:
    sql(
        dsn,
        "insert into colonies (id, layer, state, serial, title, title_source, born_tick,"
        " fruited_tick, faded_tick, died_tick, cause, parent_id, absorbed_by, cover_image_id,"
        " peak, peak_members, simulated)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
        cid,
        layer,
        state,
        serial,
        title,
        "model" if title else None,
        born,
        fruited,
        faded,
        died,
        cause,
        parent,
        absorbed_by,
        cover,
        len(members),
        None if peak_members is None else json.dumps(peak_members),
    )
    for image in members:
        member(dsn, cid, image, founder=image in founders, joined=fruited or born)


def member(
    dsn: str,
    cid: int,
    image: int,
    *,
    founder: bool = False,
    joined: int = 1,
    left: int | None = None,
) -> None:
    sql(
        dsn,
        "insert into colony_members (colony_id, image_id, slug, founder, joined_tick, left_tick)"
        " select %s, id, slug, %s, %s, %s from images where id = %s",
        cid,
        founder,
        joined,
        left,
        image,
    )


def event(
    dsn: str,
    cid: int,
    kind: str,
    t: int,
    *,
    image: int | None = None,
    other: int | None = None,
    start: datetime,
) -> int:
    rows = sql(
        dsn,
        "insert into colony_events (tick, scheduled_at, kind, colony_id, layer, image_id,"
        " other_colony_id, simulated) values (%s, %s, %s, %s, 'whole', %s, %s, true)"
        " returning id",
        t,
        start + t * STEP,
        kind,
        cid,
        image,
        other,
    )
    return int(rows[0][0])


def edge(dsn: str, a: int, b: int, s: float, layer: str = "whole") -> None:
    sql(
        dsn,
        "insert into edge_state (layer, src, dst, strength, latched, fatigue, fused, idle)"
        " values (%s, %s, %s, %s, true, 0, false, 0)",
        layer,
        a,
        b,
        s,
    )


START = datetime(2026, 3, 1, tzinfo=UTC)


# -- before public -------------------------------------------------------------


async def test_nothing_before_galleries_are_public(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_image(clean_db, 1)
    ticks(clean_db, 3, START)
    colony(clean_db, 1, 1, "alive", members=(a,), cover=a)
    event(clean_db, 1, "fruited", 1, start=START)
    assert (await client.get("/api/galleries")).json() == []
    r = await client.get("/api/galleries/1")
    assert r.status_code == 404 and r.json()["detail"] == "not here"
    assert (await client.get("/api/mycelium/log")).json() == {"events": [], "next": None}
    assert (await client.get("/api/images/img-1")).json()["galleries"] == []
    assert (await client.get("/g/1")).status_code == 404

    public(clean_db)
    assert [g["serial"] for g in (await client.get("/api/galleries")).json()] == [1]
    assert (await client.get("/g/1")).status_code == 200


# -- list --------------------------------------------------------------------


async def test_list_order_states_and_filter(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    ids = [add_image(clean_db, n) for n in range(1, 9)]
    hidden = add_image(clean_db, 9, status="unpublished")
    ticks(clean_db, 12, START)
    colony(clean_db, 1, 1, "alive", members=(ids[0], ids[1], hidden), fruited=2, cover=ids[0])
    colony(clean_db, 2, 3, "alive", members=(ids[2], ids[3], ids[4]), fruited=5, layer="facet:mood")
    colony(clean_db, 3, 2, "fading", members=(ids[5], ids[6]), fruited=3, faded=6, cover=hidden)
    colony(
        clean_db,
        4,
        4,
        "dead",
        members=(ids[7],),
        fruited=4,
        died=8,
        cause="starved",
        peak_members=[{"id": ids[7], "slug": "img-8"}, {"id": 999, "slug": "gone"}],
    )
    colony(clean_db, 5, 5, "dead", members=(), fruited=6, died=9, cause="absorbed", absorbed_by=1)
    colony(clean_db, 6, None, "forming", members=(ids[0],), fruited=None)
    colony(clean_db, 7, None, "failed", fruited=None, died=3)
    public(clean_db)

    body = (await client.get("/api/galleries")).json()
    assert [g["serial"] for g in body] == [3, 1, 2, 5, 4]
    assert [g["state"] for g in body] == ["living", "living", "fading", "dead", "dead"]
    assert set(body[0]) == {
        "serial",
        "title",
        "state",
        "layer",
        "size",
        "fruited_at",
        "died_at",
        "cause",
        "cover",
        "age_days",
    }
    first = body[1]
    assert first["title"] == "gallery 1"
    assert first["layer"] == "whole" and body[0]["layer"] == "mood"
    # The unpublished member is not counted.
    assert first["size"] == 2
    assert set(first["cover"]) == GRID_KEYS and first["cover"]["slug"] == "img-1"
    assert first["fruited_at"] == (START + 2 * STEP).isoformat()
    assert first["died_at"] is None and first["cause"] is None
    # An unpublished cover is no cover.
    assert body[2]["cover"] is None
    dead = body[4]
    assert dead["died_at"] == (START + 8 * STEP).isoformat() and dead["cause"] == "starved"
    # A dead gallery counts its peak.
    assert dead["size"] == 2 and dead["age_days"] == 0

    for state, want in (
        ("living", [3, 1]),
        ("fading", [2]),
        ("dead", [5, 4]),
        ("all", [3, 1, 2, 5, 4]),
    ):
        got = (await client.get("/api/galleries", params={"state": state})).json()
        assert [g["serial"] for g in got] == want, state
    r = await client.get("/api/galleries", params={"state": "alive"})
    assert r.status_code == 400 and r.json()["detail"] == "bad state"


# -- detail ------------------------------------------------------------------


async def test_detail_of_a_living_gallery(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    m1 = add_image(clean_db, 1, caption="a cup")
    m2 = add_image(clean_db, 2)
    m3 = add_image(clean_db, 3)
    hidden = add_image(clean_db, 4, status="unpublished")
    gone = add_image(clean_db, 5)
    ticks(clean_db, 10, START)
    colony(clean_db, 1, 4, "fading", members=(m1,), fruited=1, faded=2)
    colony(
        clean_db,
        2,
        9,
        "alive",
        members=(m1, m2, m3, hidden),
        founders=(m1, m3),
        born=2,
        fruited=4,
        parent=1,
        cover=m1,
        title="cups",
    )
    member(clean_db, 2, gone, joined=5, left=6)
    # m1 and m2 are elsewhere too: a living gallery in another layer, a dead one, and a
    # living one m3 left.
    colony(clean_db, 3, 7, "alive", members=(m1, m2), layer="facet:mood", fruited=3)
    colony(clean_db, 4, 8, "dead", members=(m2,), fruited=3, died=5, cause="starved")
    colony(clean_db, 5, 10, "alive", members=(), fruited=3)
    member(clean_db, 5, m3, joined=3, left=4)
    # A child with an address and one without.
    colony(clean_db, 6, 11, "alive", members=(), born=6, fruited=8, parent=2)
    colony(clean_db, 7, None, "forming", members=(), born=7, fruited=None, parent=2)

    edge(clean_db, m1, m2, 0.4)
    edge(clean_db, m2, m1, 0.5)
    edge(clean_db, m2, m3, 0.2)
    # Too weak to draw, from a hidden member, and in another layer.
    edge(clean_db, m1, m3, 0.05)
    edge(clean_db, m1, hidden, 0.9)
    edge(clean_db, m1, m3, 0.9, layer="facet:mood")
    sql(
        clean_db,
        "insert into colony_ticks (colony_id, tick, size, mean_strength, total_strength)"
        " values (2, 2, 3, 0.3, 0.6), (2, 3, 4, 0.35, 1.05), (2, 4, 4, 0.4, null)",
    )
    event(clean_db, 2, "formed", 2, other=1, start=START)
    event(clean_db, 2, "fruited", 4, start=START)
    event(clean_db, 2, "joined", 5, image=gone, start=START)
    event(clean_db, 2, "joined", 5, image=hidden, start=START)
    event(clean_db, 2, "joined", 6, image=m2, start=START)
    sql(clean_db, "delete from images where id = %s", gone)
    public(clean_db)

    r = await client.get("/api/galleries/9")
    assert r.status_code == 200
    g = r.json()
    assert g["serial"] == 9 and g["title"] == "cups" and g["state"] == "living"
    assert g["layer"] == "whole" and g["size"] == 3 and g["cause"] is None
    assert g["formed_at"] == (START + 2 * STEP).isoformat()
    assert g["fruited_at"] == (START + 4 * STEP).isoformat()
    assert g["faded_at"] is None and g["died_at"] is None
    assert g["parent"] == {"serial": 4, "title": "gallery 4"}
    assert g["absorbed_by"] is None
    assert g["children"] == [{"serial": 11, "title": "gallery 11"}]
    assert g["cover"]["slug"] == "img-1"

    # Founders first, then by joining; the unpublished member is absent.
    assert [m["slug"] for m in g["members"]] == ["img-1", "img-3", "img-2"]
    first = g["members"][0]
    assert set(first) == GRID_KEYS | {"caption", "founder", "others"}
    assert first["caption"] == "a cup" and first["founder"] is True
    others = {m["slug"]: m["others"] for m in g["members"]}
    # Living galleries only (fading counts), never this one, the dead one or one left.
    assert others == {
        "img-1": [{"serial": 4, "title": "gallery 4"}, {"serial": 7, "title": "gallery 7"}],
        "img-3": [],
        "img-2": [{"serial": 7, "title": "gallery 7"}],
    }
    assert g["shares"] == [
        {"serial": 7, "title": "gallery 7", "n": 2},
        {"serial": 4, "title": "gallery 4", "n": 1},
    ]
    assert g["edges"] == [
        {"a": "img-1", "b": "img-2", "strength": 0.5},
        {"a": "img-2", "b": "img-3", "strength": 0.2},
    ]
    assert g["series"] == [
        {"tick": 2, "size": 3, "mean_strength": 0.3, "total_strength": 0.6},
        {"tick": 3, "size": 4, "mean_strength": 0.35, "total_strength": 1.05},
        {"tick": 4, "size": 4, "mean_strength": 0.4, "total_strength": None},
    ]
    # Newest first; joins by pictures that are not live are not shown.
    assert g["events"] == [
        {"at": (START + 6 * STEP).isoformat(), "kind": "joined", "slug": "img-2", "other": None},
        {"at": (START + 4 * STEP).isoformat(), "kind": "fruited", "slug": None, "other": None},
        {
            "at": (START + 2 * STEP).isoformat(),
            "kind": "formed",
            "slug": None,
            "other": {"serial": 4, "title": "gallery 4"},
        },
    ]
    assert g["former_members"] == []
    assert g["peak"] == 4
    text = json.dumps(g)
    assert "img-4" not in text and "simulated" not in text and '"id"' not in text


async def test_tombstone(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    m1 = add_image(clean_db, 1, caption="a cup")
    hidden = add_image(clean_db, 2, status="unpublished")
    later = add_image(clean_db, 3)
    ticks(clean_db, 300, START)
    colony(clean_db, 1, 1, "alive", members=(later,), fruited=2)
    colony(
        clean_db,
        2,
        2,
        "dead",
        members=(m1, hidden),
        born=1,
        fruited=3,
        died=200,
        cause="absorbed",
        absorbed_by=1,
        cover=m1,
        peak_members=[
            {"id": m1, "slug": "img-1-old"},
            {"id": 12345, "slug": "deleted-one"},
            {"id": hidden, "slug": "img-2"},
        ],
    )
    colony(clean_db, 3, 3, "alive", members=(), born=200, fruited=210, parent=2)
    edge(clean_db, m1, hidden, 0.9)
    event(clean_db, 2, "fruited", 3, start=START)
    event(clean_db, 2, "absorbed", 200, other=1, start=START)
    public(clean_db)

    g = (await client.get("/api/galleries/2")).json()
    assert g["state"] == "dead" and g["cause"] == "absorbed"
    assert g["died_at"] == (START + 200 * STEP).isoformat()
    assert g["age_days"] == 1
    assert g["absorbed_by"] == {"serial": 1, "title": "gallery 1"}
    assert g["children"] == [{"serial": 3, "title": "gallery 3"}]
    assert g["members"] == [] and g["edges"] == [] and g["shares"] == []
    # The cover is frozen at death.
    assert g["cover"]["slug"] == "img-1"
    assert g["peak"] == 3 and g["size"] == 3
    former = g["former_members"]
    assert former[0]["slug"] == "img-1" and former[0]["live"] is True
    assert former[0]["caption"] == "a cup"
    assert former[1:] == [{"slug": "deleted-one", "live": False}, {"slug": "img-2", "live": False}]
    assert [e["kind"] for e in g["events"]] == ["absorbed", "fruited"]
    assert g["events"][0]["other"] == {"serial": 1, "title": "gallery 1"}


@pytest.mark.parametrize("path", ["99", "0", "01", "abc", "1234567890", "-1"])
async def test_detail_not_here(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    path: str,
) -> None:
    ticks(clean_db, 2, START)
    colony(clean_db, 1, 1, "alive")
    colony(clean_db, 2, None, "forming", fruited=None)
    public(clean_db)
    r = await client.get(f"/api/galleries/{path}")
    assert r.status_code == 404 and r.json()["detail"] == "not here"


# -- log ---------------------------------------------------------------------


async def test_log_pages_newest_first(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    a = add_image(clean_db, 1)
    hidden = add_image(clean_db, 2, status="unpublished")
    ticks(clean_db, 200, START)
    colony(clean_db, 1, 1, "alive", members=(a,), fruited=1)
    colony(clean_db, 2, 2, "dead", fruited=2, died=150, cause="absorbed", absorbed_by=1)
    colony(clean_db, 3, None, "forming", fruited=None)
    event(clean_db, 1, "fruited", 1, start=START)
    event(clean_db, 2, "fruited", 2, start=START)
    for t in range(3, 123):
        event(clean_db, 1, "joined" if t % 2 else "left", t, image=a, start=START)
    # Not in the log: a hidden picture, a forming colony, a kind it does not carry.
    event(clean_db, 1, "joined", 140, image=hidden, start=START)
    event(clean_db, 3, "formed", 141, start=START)
    event(clean_db, 1, "faded", 142, start=START)
    event(clean_db, 2, "absorbed", 150, other=1, start=START)
    # Written later at an earlier tick, as a layer replay does: ordered by tick.
    late = event(clean_db, 1, "joined", 2, image=a, start=START)
    public(clean_db)

    pages: list[list[dict[str, Any]]] = []
    before: int | None = None
    while True:
        params = {} if before is None else {"before": before}
        body = (await client.get("/api/mycelium/log", params=params)).json()
        pages.append(body["events"])
        before = body["next"]
        if before is None:
            break
    assert [len(p) for p in pages] == [50, 50, 24]
    flat = [e for p in pages for e in p]
    assert flat[0] == {
        "id": flat[0]["id"],
        "at": (START + 150 * STEP).isoformat(),
        "kind": "died",
        "cause": "absorbed",
        "gallery": {"serial": 2, "title": "gallery 2"},
        "slug": None,
        "other": {"serial": 1, "title": "gallery 1"},
    }
    assert flat[1]["kind"] == "left" and flat[1]["slug"] == "img-1"
    assert flat[1]["at"] == (START + 122 * STEP).isoformat()
    ats = [e["at"] for e in flat]
    assert ats == sorted(ats, reverse=True)
    assert {e["kind"] for e in flat} == {"died", "joined", "left", "fruited"}
    assert [e["id"] for e in flat if e["at"] == (START + 2 * STEP).isoformat()] == [
        late,
        flat[-2]["id"],
    ]
    assert len({e["id"] for e in flat}) == len(flat)
    assert (await client.get("/api/mycelium/log", params={"before": 999999})).json() == {
        "events": [],
        "next": None,
    }


# -- image detail -----------------------------------------------------------


async def test_image_detail_lists_galleries(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    me = add_image(clean_db, 1)
    ticks(clean_db, 10, START)
    colony(clean_db, 1, 1, "dead", members=(me,), fruited=1, died=5, cause="starved")
    colony(clean_db, 2, 2, "alive", members=(me,), fruited=2)
    colony(clean_db, 3, 3, "fading", members=(me,), fruited=3, faded=6)
    colony(clean_db, 4, 4, "alive", members=(), fruited=4)
    member(clean_db, 4, me, joined=4, left=7)
    # Left before it fruited: never in the gallery.
    colony(clean_db, 5, 5, "alive", members=(), born=1, fruited=6)
    member(clean_db, 5, me, joined=1, left=3)
    colony(clean_db, 6, None, "forming", members=(me,), fruited=None)
    public(clean_db)

    got = (await client.get("/api/images/img-1")).json()["galleries"]
    assert got == [
        {"serial": 2, "title": "gallery 2", "state": "living", "current": True},
        {"serial": 3, "title": "gallery 3", "state": "fading", "current": True},
        {"serial": 4, "title": "gallery 4", "state": "living", "current": False},
        {"serial": 1, "title": "gallery 1", "state": "dead", "current": False},
    ]


# -- /g/<n> ------------------------------------------------------------------


async def test_gallery_page_og_tags(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
) -> None:
    ids = [add_image(clean_db, n) for n in range(1, 4)]
    ticks(clean_db, 3, START)
    colony(clean_db, 1, 12, "alive", members=tuple(ids), cover=ids[1], title='a "quoted" one')
    colony(clean_db, 2, 13, "alive", members=(ids[0],))
    public(clean_db)

    r = await client.get("/g/12")
    assert r.status_code == 200
    assert r.text.count('property="og:title"') == 1
    assert '<meta property="og:title" content="a &quot;quoted&quot; one">' in r.text
    assert '<meta property="og:description" content="3 pictures">' in r.text
    assert f'<meta property="og:image" content="http://t/media/pub/{sha(2)}-1600.webp">' in r.text
    assert 'property="og:image:width" content="1600"' in r.text
    assert 'property="og:image:height" content="1200"' in r.text
    page = (await client.get("/g/13")).text
    assert '<meta property="og:description" content="1 picture">' in page
    assert "og:image" not in page

    head = await client.head("/g/12")
    assert head.status_code == 200


@pytest.mark.parametrize(
    ("ago", "line"),
    [
        (timedelta(days=3, hours=1), "died 3 days ago"),
        (timedelta(days=1, hours=1), "died 1 day ago"),
        (timedelta(hours=1), "died today"),
    ],
)
async def test_dead_gallery_page(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    ago: timedelta,
    line: str,
) -> None:
    a = add_image(clean_db, 1)
    now = datetime.now(UTC)
    for k, at in enumerate((now - timedelta(days=9), now - ago)):
        sql(
            clean_db,
            "insert into mycelium_ticks (tick, scheduled_at, simulated, state_hash,"
            " colonies_hash, stats) values (%s, %s, false, 'h', 'h', '{}')",
            k,
            at,
        )
    colony(
        clean_db, 1, 1, "dead", members=(a,), born=0, fruited=0, died=1, cause="starved", cover=a
    )
    public(clean_db)
    r = await client.get("/g/1")
    assert r.status_code == 200
    assert '<meta property="og:title" content="gallery 1">' in r.text
    assert f'<meta property="og:description" content="{line}">' in r.text
    assert "og:image" in r.text
    # Fruited 9 days ago: the age counts to death, not to now.
    assert (await client.get("/api/galleries/1")).json()["age_days"] == (
        timedelta(days=9) - ago
    ).days


@pytest.mark.parametrize("path", ["/g/99999", "/g/0", "/g/007", "/g/1234567890", "/g/2"])
async def test_gallery_page_not_here(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    path: str,
) -> None:
    ticks(clean_db, 2, START)
    colony(clean_db, 1, 1, "alive")
    # A colony without an address has no page.
    colony(clean_db, 2, None, "forming", fruited=None)
    public(clean_db)
    r = await client.get(path)
    assert r.status_code == 404
    assert "og:description" not in r.text
    assert (await client.head(path)).status_code == 404


@pytest.mark.parametrize("path", ["/g/abc", "/g/1/x", "/g/", "/g"])
async def test_non_digit_gallery_paths_fall_through(
    client: httpx.AsyncClient,  # noqa: F811
    clean_db: str,
    path: str,
) -> None:
    r = await client.get(path)
    assert r.status_code == 200
    assert re.search(r'og:title" content="peni.sh"', r.text)


# -- the projection ----------------------------------------------------------

MODEL = "test/embed"
DIM = 4
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FAST = {"rho": 0.2, "colony_enter": 0.1, "colony_leave": 0.08, "fruit_ticks": 3, "admit_ticks": 1}


@pytest.fixture
def _embed_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EMBED_MODEL", MODEL)
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def p(clean_db: str, _embed_settings: None) -> AsyncIterator[Any]:
    pool = make_pool(clean_db)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _vec(n: int) -> list[float]:
    c, k = divmod(n - 1, 4)
    v = [0.1 * ((k * 7 + c * 3 + j) % 5) / 5 for j in range(DIM)]
    v[c % DIM] += 1.0
    return v


def _corpus(dsn: str, n: int = 12) -> list[int]:
    out = []
    for i in range(1, n + 1):
        rows = sql(
            dsn,
            "insert into images (sha256, pub_sha, slug, status, published_at)"
            " values (%s, %s, %s, 'live', %s) returning id",
            sha(i),
            sha(i),
            f"img-{i}",
            T0 - timedelta(days=30) + timedelta(minutes=i),
        )
        image_id = int(rows[0][0])
        sql(
            dsn,
            "insert into embeddings (image_id, kind, vec, model, dim)"
            " values (%s, 'description', %s::vector, %s, %s)",
            image_id,
            str(_vec(i)),
            MODEL,
            DIM,
        )
        out.append(image_id)
    return out


async def test_tick_writes_public_since_cover_peak_and_total(p: Any, clean_db: str) -> None:
    _corpus(clean_db)
    async with p.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
        await store.save_state(conn, running=True, next_due=T0)
    fruited_at: int | None = None
    for k in range(12):
        await _run_one(p, k)
        if sql(clean_db, "select 1 from colonies where serial is not null") and fruited_at is None:
            fruited_at = k
            st = await _state(p)
            # The tick that brings the first serial makes addresses public.
            assert st.public_since == T0 + k * STEP
        elif fruited_at is None:
            assert (await _state(p)).public_since is None
    assert fruited_at is not None

    async with p.connection() as conn:
        st = await store.read_state(conn)
    cs = st.colonies_state()
    alive = [c for c in cs.colonies if c.serial is not None and c.state == "alive"]
    assert alive
    rows = {
        r[0]: r[1:]
        for r in sql(
            clean_db,
            "select id, cover_image_id, peak_members, peak_tick from colonies"
            " where serial is not null",
        )
    }
    for c in alive:
        cover, peak_members, peak_tick = rows[c.id]
        assert cover in c.images()
        assert peak_members is not None and len(peak_members) == c.peak
        assert {m["slug"] for m in peak_members} <= {f"img-{i}" for i in range(1, 13)}
        assert peak_tick is not None
    totals = sql(
        clean_db,
        "select mean_strength, total_strength from colony_ticks where total_strength > 0",
    )
    assert totals and all(t >= m for m, t in totals)
    assert sql(clean_db, "select count(*) from colony_ticks where total_strength is null") == [(0,)]

    # Once public, a full genesis refuses with that reason.
    async with p.connection() as conn:
        await store.save_state(conn, running=False)
    async with p.connection() as conn:
        with pytest.raises(genesis.GenesisRefused, match="public"):
            await genesis.run(conn)


def test_cover_is_strongest_member_lowest_id_on_ties() -> None:
    from penish.mycelium import colonies as col

    c = col.Colony(
        id=1,
        layer="whole",
        state=col.ALIVE,
        born=0,
        members=[col.Member(i, True, 0) for i in (5, 3, 9)],
    )
    latched = {"whole": [(3, 5, 0.4), (5, 9, 0.4), (3, 9, 0.1), (5, 77, 0.9)]}
    assert store.cover_of(c, latched) == 5
    tie = {"whole": [(3, 5, 0.4), (5, 9, 0.2), (3, 9, 0.2)]}
    # 3: 0.6, 5: 0.6, 9: 0.4.
    assert store.cover_of(c, tie) == 3
    assert store.cover_of(c, {}) == 3
    c.members = []
    assert store.cover_of(c, latched) is None


async def _run_one(p: Any, k: int) -> None:
    async with p.connection() as conn:
        assert await tick.run_one(conn, now=T0 + k * STEP) is not None


async def _state(p: Any) -> store.State:
    async with p.connection() as conn:
        return await store.read_state(conn)


async def test_peak_snapshot_and_frozen_cover(p: Any, clean_db: str) -> None:
    """Hand-built states through the projection: the tombstone keeps the members at the
    largest size ever reached, from the tick it was first reached, and the cover it had
    when it died."""
    from penish.mycelium import colonies as col

    ids = _corpus(clean_db, 6)
    slugs = {i: f"img-{n}" for n, i in enumerate(ids, 1)}
    founders = set(ids[:3])

    def gallery(n: int, state: str = col.ALIVE, died: int | None = None) -> col.Colony:
        return col.Colony(
            id=1,
            layer="whole",
            state=state,
            born=0,
            serial=1,
            fruited=1,
            died=died,
            cause="starved" if died is not None else None,
            members=[col.Member(i, i in founders, 1) for i in ids[:n]],
            founder_ids=sorted(founders),
            peak=6 if n == 6 or died is not None else n,
        )

    # ids[1] has the most strength inside while the gallery lives.
    lives = {"whole": [(ids[0], ids[1], 0.5), (ids[1], ids[2], 0.5), (ids[2], ids[3], 0.1)]}
    # After death ids[2] would win, if anything still looked.
    after = {"whole": [(ids[2], ids[3], 0.9)]}
    # Fruits at 3, grows to 6, shrinks to 3, regrows to 4, dies, then a tick passes.
    steps = [
        (1, gallery(3), lives),
        (2, gallery(6), lives),
        (3, gallery(3), lives),
        (4, gallery(4), lives),
        (5, gallery(4, col.DEAD, died=5), lives),
        (6, gallery(4, col.DEAD, died=5), after),
    ]
    prev = col.ColoniesState()
    for k, c, latched in steps:
        new = col.ColoniesState(colonies=[c], next_id=2, next_serial=2, tick=k)
        async with p.connection() as conn, conn.transaction():
            await store.apply_colonies(
                conn,
                prev,
                new,
                [],
                tick=k,
                scheduled_at=T0 + k * STEP,
                simulated=True,
                slugs=slugs,
                latched=latched,
            )
        prev = new
        if k == 4:
            assert sql(clean_db, "select cover_image_id from colonies") == [(ids[1],)]

    [(cover, peak_members, peak_tick)] = sql(
        clean_db, "select cover_image_id, peak_members, peak_tick from colonies"
    )
    assert [m["id"] for m in peak_members] == ids[:6]
    assert [m["slug"] for m in peak_members] == [f"img-{n}" for n in range(1, 7)]
    assert peak_tick == 2
    assert cover == ids[1]
