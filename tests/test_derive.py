import io

import pytest
import pyvips

from penish.worker.derive import (
    MIN_CROP,
    UndecodableImage,
    blurhash_of,
    clamp_rect,
    clean_original,
    load_and_validate,
    make_derivatives,
    palette_of,
    rebuild_public,
)


def _gradient(w: int, h: int) -> pyvips.Image:
    # Non-flat content so encoders and the palette have something to chew on.
    x = pyvips.Image.xyz(w, h)
    r = (x[0] * 255 / w).cast("uchar")
    g = (x[1] * 255 / h).cast("uchar")
    b = pyvips.Image.black(w, h) + 128
    return r.bandjoin([g, b.cast("uchar")]).copy(interpretation="srgb")


def _png(w: int = 2000, h: int = 1000) -> bytes:
    out = _gradient(w, h).write_to_buffer(".png")
    assert isinstance(out, bytes)
    return out


def _gps_jpeg() -> bytes:
    img = _gradient(300, 200).copy()
    img.set_type(pyvips.GValue.gstr_type, "exif-ifd3-GPSLatitudeRef", "N")
    img.set_type(pyvips.GValue.gstr_type, "exif-ifd3-GPSLongitudeRef", "E")
    # Round-trip so the GPS tags are in the encoded file, not just in-memory fields.
    out = img.write_to_buffer(".jpg", keep="all")
    assert isinstance(out, bytes)
    return out


def _has_gps(data: bytes) -> bool:
    img = pyvips.Image.new_from_buffer(data, "")
    return any("GPS" in f for f in img.get_fields())


def test_png_roundtrip() -> None:
    d = load_and_validate(_png(800, 600))
    assert (d.width, d.height, d.mime) == (800, 600, "image/png")
    assert len(d.sha256) == 64


def test_gps_is_stripped() -> None:
    src = _gps_jpeg()
    assert _has_gps(src), "fixture must actually carry GPS"
    d = load_and_validate(src)
    out = clean_original(d.image, d.mime)
    assert not _has_gps(out)
    assert pyvips.Image.new_from_buffer(out, "").width == 300


def test_truncated_jpeg_raises() -> None:
    img = _gradient(600, 400)
    data = img.write_to_buffer(".jpg", Q=90)
    assert isinstance(data, bytes)
    with pytest.raises(UndecodableImage):
        load_and_validate(data[: len(data) // 2])


def test_text_renamed_png_raises() -> None:
    with pytest.raises(UndecodableImage):
        load_and_validate(b"this is definitely not a png\n" * 10)


def test_unsupported_format_raises() -> None:
    gif_like = _gradient(32, 32).write_to_buffer(".tif")
    assert isinstance(gif_like, bytes)
    with pytest.raises(UndecodableImage):
        load_and_validate(gif_like)


def test_derivative_widths_and_no_upscale() -> None:
    d = load_and_validate(_png(1000, 500))
    ders = make_derivatives(d.image, d.sha256)
    assert sorted({x.width for x in ders}) == [480, 960]
    assert {x.fmt for x in ders} == {"avif", "webp"}
    assert all(x.key == f"pub/{d.sha256}-{x.width}.{x.fmt}" for x in ders)
    assert all(x.height == round(x.width / 2) for x in ders)


def test_full_ladder() -> None:
    d = load_and_validate(_png(2000, 1000))
    assert sorted({x.width for x in make_derivatives(d.image, d.sha256)}) == [480, 960, 1600]


def test_small_source_gets_one_native_width() -> None:
    d = load_and_validate(_png(200, 100))
    ders = make_derivatives(d.image, d.sha256)
    assert {x.width for x in ders} == {200}
    assert len(ders) == 2


def test_avif_decodes() -> None:
    d = load_and_validate(_png(600, 300))
    avif = next(x for x in make_derivatives(d.image, d.sha256) if x.fmt == "avif")
    img = pyvips.Image.new_from_buffer(avif.data, "")
    assert img.width == avif.width
    img.avg()


def test_watermark_changes_pixels() -> None:
    d = load_and_validate(_png(600, 300))
    plain = next(x for x in make_derivatives(d.image, d.sha256) if x.fmt == "webp")
    marked = next(x for x in make_derivatives(d.image, d.sha256, watermark=True) if x.fmt == "webp")
    assert plain.data != marked.data
    assert (marked.width, marked.height) == (plain.width, plain.height)


def test_blurhash() -> None:
    d = load_and_validate(_png(400, 300))
    h = blurhash_of(d.image)
    assert isinstance(h, str) and len(h) > 6


def test_palette() -> None:
    d = load_and_validate(_png(400, 300))
    p = palette_of(d.image)
    assert len(p) == 5
    assert all(len(c) == 7 and c[0] == "#" and int(c[1:], 16) >= 0 for c in p)
    assert p == palette_of(d.image)


def test_palette_flat_image() -> None:
    flat = (pyvips.Image.black(50, 50, bands=3) + 200).cast("uchar")
    assert len(palette_of(flat)) == 5


def test_blurhash_input_is_png_buffer() -> None:
    # Guards the assumption that the thumbnail encodes cleanly as PNG.
    buf = _gradient(40, 40).write_to_buffer(".png")
    assert isinstance(buf, bytes)
    assert io.BytesIO(buf).read(4) == b"\x89PNG"


def _clean(w: int = 2000, h: int = 1000) -> bytes:
    d = load_and_validate(_png(w, h))
    return clean_original(d.image, d.mime)


def test_rebuild_public_uncropped() -> None:
    data = _clean()
    pub = rebuild_public(data, None, False)
    assert (pub.width, pub.height) == (2000, 1000)
    assert sorted({d.width for d in pub.derivatives}) == [480, 960, 1600]
    assert all(d.key.startswith(f"pub/{pub.pub_sha}-") for d in pub.derivatives)
    assert len(pub.pub_sha) == 64 and len(pub.palette) == 5 and pub.blurhash
    # Deterministic, so a retried crop job writes the same file names.
    assert rebuild_public(data, None, False).pub_sha == pub.pub_sha
    # The watermark changes the pixels, so it must change the immutable URLs too.
    assert rebuild_public(data, None, True).pub_sha != pub.pub_sha


def test_rebuild_public_cropped() -> None:
    data = _clean()
    full = rebuild_public(data, None, False)
    pub = rebuild_public(data, {"x": 100, "y": 50, "w": 700, "h": 400}, False)
    assert (pub.width, pub.height) == (700, 400)
    assert pub.pub_sha != full.pub_sha
    # 700 wide: only the 480 rung fits under the source width.
    assert sorted({d.width for d in pub.derivatives}) == [480]
    # Red ramps with x, so the left strip has a different palette from the whole frame.
    assert pub.palette != full.palette


def test_rebuild_public_full_frame_rect_is_uncropped() -> None:
    data = _clean(800, 600)
    whole = rebuild_public(data, {"x": -10, "y": -10, "w": 5000, "h": 5000}, False)
    assert whole.pub_sha == rebuild_public(data, None, False).pub_sha


def test_clamp_rect() -> None:
    assert clamp_rect({"x": 10, "y": 20, "w": 100, "h": 50}, 800, 600) == {
        "x": 10,
        "y": 20,
        "w": 100,
        "h": 50,
    }
    # Spills past the edges are clipped, floats rounded.
    assert clamp_rect({"x": -5.4, "y": 550, "w": 900, "h": 100.6}, 800, 600) == {
        "x": 0,
        "y": 550,
        "w": 800,
        "h": 50,
    }
    assert clamp_rect({"x": 0, "y": 0, "w": 800, "h": 600}, 800, 600) is None
    for bad in (
        {"x": 0, "y": 0, "w": MIN_CROP - 1, "h": 100},
        {"x": 900, "y": 0, "w": 100, "h": 100},
        {"x": 0, "y": 0, "w": 100},
        {"x": "1", "y": 0, "w": 100, "h": 100},
        {"x": True, "y": 0, "w": 100, "h": 100},
        {"x": float("nan"), "y": 0, "w": 100, "h": 100},
    ):
        with pytest.raises(ValueError):
            clamp_rect(bad, 800, 600)
