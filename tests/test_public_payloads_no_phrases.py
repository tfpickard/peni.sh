"""Need and offer phrases are admin-only: a crawl of every public GET route finds none.

Pictures get distinctive phrases (stored as phrases and in their raw extract), real ticks
grow need edges from them, and a need gallery is made public. Then every GET route the
app serves outside /api/admin is called with its path parameters filled from what was
seeded, once bare and once per known query value, and every JSON body, HTML shell (og
tags and the inline haiku included) and header is scanned for any phrase. The admin
payload is scanned the same way as a control: there the phrases must be found.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from psycopg.types.json import Jsonb

from penish.api import admin, embed_client, routes_search
from penish.api.app import create_app
from penish.config import get_settings
from penish.mycelium import store, tick
from penish.mycelium.graph import NEED
from penish.sort import SORTS
from tests.test_admin import PW, XHR, _login
from tests.test_api_images import DIM, add_image, sha
from tests.test_mycelium_tick import FAST, T0, TICK, sql

N = 9
TAG = "lichen"
CATEGORY = "fine-art"
NEED_SERIAL = 901
DEAD_SERIAL = 902

# Words that occur nowhere else, so a hit can only be a phrase or a piece of one.
NEED_WORD = "vorpalquince"
OFFER_WORD = "glimmerwick"
# A phrase with characters HTML and JSON escape, so an escaped leak is caught too.
ODD_NEED = 'a "brass" & <ferrule> of zentrumbly'


def need_text(k: int) -> str:
    return f"a {NEED_WORD} {k}" if k else ODD_NEED


def offer_text(k: int) -> str:
    return f"one {OFFER_WORD} {k}"


def axis(i: int, wobble: float = 0.0) -> list[float]:
    v = np.zeros(DIM)
    v[i] = 1.0
    v[(i + 1) % DIM] = wobble
    return [float(x) for x in v]


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    admin._failures.clear()
    routes_search._vectors.clear()
    routes_search.search_limit.clear()
    yield
    routes_search._vectors.clear()


@pytest.fixture
async def app(clean_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><html><head><!--og--></head><body><div id=app></div></body></html>"
    )
    data = tmp_path / "data"
    (data / "media" / "pub").mkdir(parents=True)
    (data / "media" / "orig").mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WEB_BUILD_DIR", str(web))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    get_settings.cache_clear()

    # Search's nearest half asks the worker for a query vector; answer it here.
    async def fake(text: str, **kw: Any) -> dict[str, Any]:
        return {"vector": axis(0), "model": get_settings().embed_model, "dim": DIM}

    monkeypatch.setattr(embed_client, "embed_query_full", fake)
    a = create_app()
    async with a.router.lifespan_context(a):
        yield a
    get_settings.cache_clear()


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t")


# -- seeding ----------------------------------------------------------------------


def phrase(dsn: str, image_id: int, role: str, text: str, vec: list[float]) -> None:
    sql(
        dsn,
        "insert into offers_needs (image_id, role, phrase, vec, model, dim)"
        " values (%s, %s, %s, %s::vector, %s, %s)",
        image_id,
        role,
        text,
        str(vec),
        get_settings().embed_model,
        DIM,
    )


def extract(dsn: str, image_id: int, k: int) -> None:
    body = {
        "subject": f"subject {k}",
        "setting": "a room",
        "style": "flat light",
        "mood": "still",
        "offers": [offer_text(k)],
        "needs": [need_text(k)],
    }
    sql(
        dsn,
        "insert into image_extracts (image_id, model, body) values (%s, 'm', %s)",
        image_id,
        Jsonb(body),
    )


def seed(dsn: str) -> tuple[list[int], int]:
    """Live pictures with captions, descriptions, tags, a category and phrases; plus one
    unpublished picture with phrases of its own. Picture k needs what k + 1 offers."""
    ids: list[int] = []
    for k in range(N):
        i = add_image(
            dsn,
            k + 1,
            caption=f"caption {k + 1}",
            description=f"A plain picture number {k + 1}.",
            vec=axis(k % 3, 0.1 * k),
        )
        ids.append(i)
        sql(dsn, "insert into tags (image_id, tag, source) values (%s, %s, 'model')", i, TAG)
        sql(
            dsn,
            "insert into image_categories (image_id, category, source) values (%s, %s, 'model')",
            i,
            CATEGORY,
        )
    for k, i in enumerate(ids):
        phrase(dsn, i, "need", need_text(k), axis(100 + k))
        phrase(dsn, i, "offer", offer_text(k), axis(100 + (k - 1) % N, 0.05))
        extract(dsn, i, k)
    sql(dsn, "insert into slug_aliases values ('old-name', %s)", ids[0])
    hidden = add_image(dsn, 50, caption="caption 50", vec=axis(1), status="unpublished")
    phrase(dsn, hidden, "need", f"a {NEED_WORD} hidden", axis(100))
    phrase(dsn, hidden, "offer", f"one {OFFER_WORD} hidden", axis(101))
    extract(dsn, hidden, 50)
    return ids, hidden


async def grow(app: Any, dsn: str) -> None:
    """Real ticks build the need graph from the phrases; then a few need edges are made
    visible for certain, whatever the walkers did."""
    pool = app.state.pool
    async with pool.connection() as conn:
        await store.set_tunables(conn, FAST, "fast")
        await store.save_state(conn, running=True, next_due=T0)
    for k in range(8):
        async with pool.connection() as conn:
            assert await tick.run_one(conn, now=T0 + k * TICK) is not None
    assert sql(dsn, "select count(*) from candidate_edges where layer = %s", NEED)[0][0] > 0
    sql(
        dsn,
        "insert into edge_state (layer, src, dst, strength, latched, fatigue, fused, idle)"
        " select layer, src, dst, 0.9, true, 0, false, 0 from candidate_edges"
        " where layer = %s order by src, dst limit 4"
        " on conflict (layer, src, dst) do update set strength = 0.9, latched = true",
        NEED,
    )
    sql(dsn, "update offers_needs set satisfaction = 0.5 where role = 'need'")


def need_gallery(dsn: str, members: list[int]) -> None:
    """One living need gallery holding the visible need edges, one dead one, events and
    sparkline rows for both, and the public gate open."""
    tick0 = sql(dsn, "select min(tick), max(tick) from mycelium_ticks")[0]
    born, last = int(tick0[0]), int(tick0[1])
    peak = [
        {"id": i, "slug": s}
        for i, s in sql(dsn, "select id, slug from images where id = any(%s) order by id", members)
    ]
    for cid, serial, state, died in (
        (9001, NEED_SERIAL, "alive", None),
        (9002, DEAD_SERIAL, "dead", last),
    ):
        sql(
            dsn,
            "insert into colonies (id, layer, state, serial, born_tick, fruited_tick, died_tick,"
            " cause, cover_image_id, peak, peak_members, simulated)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)",
            cid,
            NEED,
            state,
            serial,
            born,
            born + 1,
            died,
            "starved" if died is not None else None,
            members[0],
            len(members),
            Jsonb(peak),
        )
        for i in members:
            sql(
                dsn,
                "insert into colony_members (colony_id, image_id, slug, founder, joined_tick,"
                " left_tick) select %s, id, slug, true, %s, %s from images where id = %s",
                cid,
                born + 1,
                died,
                i,
            )
        for t in range(born, last + 1):
            sql(
                dsn,
                "insert into colony_ticks (colony_id, tick, size, mean_strength, total_strength)"
                " values (%s, %s, %s, 0.4, 1.2)",
                cid,
                t,
                len(members),
            )
        kinds = [("formed", born, None), ("fruited", born + 1, None)]
        kinds += [("joined", born + 1, i) for i in members]
        if died is not None:
            kinds.append(("starved", died, None))
        for kind, t, image in kinds:
            sql(
                dsn,
                "insert into colony_events (tick, scheduled_at, kind, colony_id, layer, image_id,"
                " simulated) select %s, scheduled_at, %s, %s, %s, %s, false"
                " from mycelium_ticks where tick = %s",
                t,
                kind,
                cid,
                NEED,
                image,
                t,
            )
    sql(dsn, "update mycelium_state set public_since = coalesce(public_since, now()) where id = 1")


# -- the routes -------------------------------------------------------------------

_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


def public_gets(app: Any) -> list[str]:
    """Every GET route outside /api/admin, as path templates, in registration order.
    Routers are included under wrappers in this FastAPI, plain routes in older ones."""
    out: list[str] = []
    for r in app.routes:
        contexts = getattr(r, "effective_route_contexts", None)
        found = (
            [(c.path, c.methods) for c in contexts()]
            if callable(contexts)
            else [(getattr(r, "path", ""), getattr(r, "methods", None) or set())]
        )
        for path, methods in found:
            if "GET" in methods and not path.startswith("/api/admin") and path not in out:
                out.append(path)
    return out


def expand(template: str, fill: dict[str, list[str]]) -> list[str]:
    names = _PARAM.findall(template)
    missing = [n for n in names if n not in fill]
    assert not missing, f"no seeded values for {missing} in {template}: add them to this test"
    paths = [template]
    for name in names:
        pat = re.compile(r"\{" + name + r"(?::\w+)?\}")
        paths = [pat.sub(v, p, count=1) for p in paths for v in fill[name]]
    # The catch-all's `{path}` carries its own leading part.
    return [re.sub(r"^//", "/", p) for p in paths]


# -- the scan ---------------------------------------------------------------------


def walk(value: Any, needles: list[str], where: str = "$") -> list[str]:
    """Every place in a JSON value (keys too) where a needle occurs, case folded."""
    hits: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            hits += walk(k, needles, f"{where}.<key>")
            hits += walk(v, needles, f"{where}.{k}")
    elif isinstance(value, list):
        for n, v in enumerate(value):
            hits += walk(v, needles, f"{where}[{n}]")
    elif isinstance(value, str):
        low = value.casefold()
        hits += [f"{where}: {n!r}" for n in needles if n in low]
    return hits


_META = re.compile(r"<meta\b[^>]*>", re.I)
_CONTENT = re.compile(r"""content\s*=\s*"([^"]*)"|content\s*=\s*'([^']*)'""", re.I)
_HAIKU = re.compile(r"window\.__HAIKU__=(.*?)</script>", re.S)


def scan(r: httpx.Response, needles: list[str], sent_q: str | None = None) -> list[str]:
    hits = walk(dict(r.headers), needles, "headers")
    kind = r.headers.get("content-type", "")
    if "json" in kind:
        body = r.json()
        # Search hands back what the visitor typed; that is theirs, not a leak.
        if isinstance(body, dict) and sent_q is not None and body.get("q") == sent_q:
            body = {k: v for k, v in body.items() if k != "q"}
        return hits + walk(body, needles, "json")
    text = r.content.decode("utf-8", "replace")
    if "html" in kind:
        for tag in _META.findall(text):
            for a, b in _CONTENT.findall(tag):
                hits += walk(html.unescape(a or b), needles, f"meta {tag[:40]}")
        for blob in _HAIKU.findall(text):
            hits += walk(json.loads(blob), needles, "__HAIKU__")
        hits += walk(html.unescape(text), needles, "html")
    return hits + walk(text, needles, "body")


async def needles(dsn: str) -> list[str]:
    phrases = [p for (p,) in sql(dsn, "select phrase from offers_needs order by id")]
    assert len(phrases) == 2 * (N + 1)
    words = [NEED_WORD, OFFER_WORD, "zentrumbly", "ferrule"]
    return sorted({p.casefold() for p in phrases} | set(words))


# -- the test ---------------------------------------------------------------------


async def test_no_public_payload_carries_a_phrase(app: Any, clean_db: str) -> None:
    ids, hidden = seed(clean_db)
    await grow(app, clean_db)
    visible = sorted(
        {
            int(x)
            for row in sql(
                clean_db,
                "select src, dst from edge_state where layer = %s and strength >= 0.15",
                NEED,
            )
            for x in row
        }
    )
    assert len(visible) >= 3
    need_gallery(clean_db, visible)
    found = await needles(clean_db)

    slugs = [s for (s,) in sql(clean_db, "select slug from images order by id")]
    serials = [
        str(s)
        for (s,) in sql(
            clean_db, "select serial from colonies where serial is not null order by serial"
        )
    ]
    assert str(NEED_SERIAL) in serials and str(DEAD_SERIAL) in serials
    last_event = sql(clean_db, "select max(id) from colony_events")[0][0]
    fill = {
        "slug": [*slugs, "old-name", "nothing"],
        "serial": [*serials, "0", "99999"],
        "name": [f"{sha(1)}-480.webp", f"{sha(1)}-480.avif", "nothing.webp"],
        "path": [
            "",
            "galleries",
            "nothing/here",
            "g/abc",
            *(f"i/{s}" for s in [*slugs, "old-name", "nothing"]),
            *(f"g/{s}" for s in [*serials, "0", "99999"]),
        ],
    }
    # Each known query parameter is tried value by value on every route that takes it.
    queries: dict[str, list[str]] = {
        "sort": list(SORTS),
        "seed": ["7"],
        "tag": [TAG],
        "category": [CATEGORY],
        "limit": ["2"],
        "sim": ["raw", "centered"],
        "q": [TAG, "picture", NEED_WORD, need_text(1)],
        "state": ["all", "living", "fading", "dead"],
        "before": [str(last_event)],
    }

    templates = public_gets(app)
    # The routes this increment is about must be among them.
    for must in ("/api/images/{slug}", "/api/galleries", "/api/galleries/{serial}"):
        assert must in templates
    ok: dict[str, int] = {t: 0 for t in templates}
    leaks: list[str] = []
    calls = 0
    async with client(app) as c:
        for template in templates:
            takes = _query_names(app, template)
            for path in expand(template, fill):
                variants: list[dict[str, str]] = [{}]
                variants += [{k: v} for k in takes for v in queries.get(k, [])]
                if "sort" in takes:
                    variants += [{"sort": s, "seed": "7"} for s in SORTS]
                for params in variants:
                    r = await c.get(path, params=params)
                    calls += 1
                    if r.status_code == 200:
                        ok[template] += 1
                    leaks += [f"GET {path} {params}: {h}" for h in scan(r, found, params.get("q"))]
    assert not leaks, "\n".join(leaks[:40])
    never = [t for t, n in ok.items() if n == 0]
    assert not never, f"routes never answered 200, so never really crawled: {never}"
    assert calls > 100

    # The crawl really saw the need layer in public: a hypha, both galleries, the preview.
    async with client(app) as c:
        hyphae = []
        for s in slugs:
            r = await c.get(f"/api/images/{s}")
            if r.status_code == 200:
                hyphae += r.json()["hyphae"]
        assert any(h["layer"] == NEED for h in hyphae)
        listed = (await c.get("/api/galleries")).json()
        assert {g["serial"]: g["layer"] for g in listed if g["serial"] >= NEED_SERIAL} == {
            NEED_SERIAL: "need",
            DEAD_SERIAL: "need",
        }
        g = (await c.get(f"/api/galleries/{NEED_SERIAL}")).json()
        assert g["layer"] == "need" and g["edges"] and len(g["members"]) == len(visible)
        dead = (await c.get(f"/api/galleries/{DEAD_SERIAL}")).json()
        assert dead["state"] == "dead" and dead["former_members"]
        page = await c.get(f"/g/{NEED_SERIAL}")
        assert page.status_code == 200 and 'property="og:title"' in page.text
        assert (await c.get("/api/mycelium/log")).json()["events"]

    # Control: the scan does find them where they are meant to be.
    async with client(app) as c:
        await _login(c)
        r = await c.get("/api/admin/mycelium", headers=XHR)
        assert r.status_code == 200
        hits = scan(r, found)
    assert any(NEED_WORD in h for h in hits) and any(OFFER_WORD in h for h in hits)
    assert any("zentrumbly" in h for h in hits)
    # The unpublished picture's phrases reach no one, not even the admin listing.
    assert not any("hidden" in h for h in hits)
    assert hidden not in ids


def _query_names(app: Any, template: str) -> list[str]:
    """Query parameter names the GET route at `template` declares."""
    for r in app.routes:
        contexts = getattr(r, "effective_route_contexts", None)
        found = [c for c in contexts()] if callable(contexts) else [r]
        for c in found:
            if getattr(c, "path", None) == template and "GET" in (
                getattr(c, "methods", None) or ()
            ):
                return [q.name for q in c.dependant.query_params]
    return []
