from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from penish.api import media
from penish.config import get_settings

SHA = "d" * 64


@pytest.fixture
def pub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    d = get_settings().media_pub
    d.mkdir(parents=True)
    yield d
    get_settings.cache_clear()


@pytest.fixture
async def client(pub: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(media.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def ladder(pub: Path, sha: str = SHA) -> None:
    for w in (480, 960, 1600):
        for fmt in ("avif", "webp"):
            (pub / f"{sha}-{w}.{fmt}").write_bytes(b"x")


async def test_og_jpeg_is_served_as_jpeg(pub: Path, client: httpx.AsyncClient) -> None:
    (pub / f"{SHA}-1200.jpg").write_bytes(b"\xff\xd8jpeg")
    r = await client.get(f"/media/pub/{SHA}-1200.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"]
    assert (await client.head(f"/media/pub/{SHA}-1200.jpg")).status_code == 200
    for bad in (f"{SHA}-1200.jpeg", f"{SHA}-1200.JPG", f"{SHA}-og.jpg", f"{SHA}-1300.jpg"):
        assert (await client.get(f"/media/pub/{bad}")).status_code == 404


async def test_webp_still_served(pub: Path, client: httpx.AsyncClient) -> None:
    ladder(pub)
    r = await client.get(f"/media/pub/{SHA}-480.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"


def test_og_jpeg_is_not_a_variant(pub: Path) -> None:
    ladder(pub)
    (pub / f"{SHA}-1200.jpg").write_bytes(b"x")
    assert [v["w"] for v in media.variants_for(SHA, 2000, 1000)] == [480, 960, 1600]


def test_og_image(pub: Path) -> None:
    assert media.og_image(SHA, 2000, 1000) is None
    (pub / f"{SHA}-1200.jpg").write_bytes(b"x")
    assert media.og_image(SHA, 2000, 1000) == (f"/media/pub/{SHA}-1200.jpg", 1200, 600)
    assert media.og_image(SHA, 4032, 3024) == (f"/media/pub/{SHA}-1200.jpg", 1200, 900)
    # Narrower than 1200: its own width, never upscaled.
    (pub / f"{SHA}-800.jpg").write_bytes(b"x")
    assert media.og_image(SHA, 800, 600) == (f"/media/pub/{SHA}-800.jpg", 800, 600)
    for args in ((None, 2000, 1000), (SHA, None, 1000), (SHA, 2000, None), (SHA, 0, 0)):
        assert media.og_image(*args) is None
    assert media.og_image("../../etc", 2000, 1000) is None
    assert media.og_image("D" * 64, 2000, 1000) is None


def test_og_image_held_is_none(pub: Path) -> None:
    # Unpublished pictures keep their files in the holding dir, which nothing serves.
    held = get_settings().media_held
    held.mkdir(parents=True)
    (held / f"{SHA}-1200.jpg").write_bytes(b"x")
    assert media.og_image(SHA, 2000, 1000) is None
