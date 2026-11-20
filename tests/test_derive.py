import hashlib
import io
import sys

import numpy as np
import pillow_heif
import pytest
import pyvips
from PIL import Image

from penish.worker import derive
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


def test_unsupported_format_names_what_is_accepted() -> None:
    tif = _gradient(32, 32).write_to_buffer(".tif")
    assert isinstance(tif, bytes)
    with pytest.raises(UndecodableImage, match=r"need png, jpeg, webp, heic or avif$"):
        load_and_validate(tif)


# -- iPhone and iPad formats --------------------------------------------------

# Registered once for the fixtures only; derive itself goes through open_heif.
pillow_heif.register_heif_opener()


def _loader(data: bytes) -> str:
    return str(pyvips.Image.new_from_buffer(data, "").get("vips-loader"))


def _uhdr() -> bytes:
    xy = pyvips.Image.xyz(256, 256)
    hdr = (
        (xy[0] / 255.0)
        .bandjoin([xy[1] / 255.0 * 2.0, (xy[0] + xy[1]) / 510.0])
        .copy(interpretation="scrgb")
    )
    out = hdr.uhdrsave_buffer()
    assert isinstance(out, bytes)
    return out


def _heic(im: Image.Image, **opts: object) -> bytes:
    b = io.BytesIO()
    im.save(b, "HEIF", quality=95, **opts)
    return b.getvalue()


def _px(img: pyvips.Image, x: int, y: int) -> list[float]:
    return list(img.getpoint(x, y))


def _near(got: list[float], want: list[float], tol: float = 8) -> bool:
    return len(got) == len(want) and all(abs(g - w) <= tol for g, w in zip(got, want, strict=True))


def _p3_to_srgb(rgb: tuple[int, int, int]) -> list[float]:
    one = (pyvips.Image.black(1, 1, bands=3) + list(rgb)).cast("uchar")
    return _px(one.copy(interpretation="srgb").icc_transform("srgb", input_profile="p3"), 0, 0)


def test_ultra_hdr_decodes_as_its_jpeg_base() -> None:
    data = _uhdr()
    assert _loader(data) == "uhdrload_buffer", "fixture must be an Ultra HDR jpeg"
    d = load_and_validate(data)
    assert (d.width, d.height, d.mime) == (256, 256, "image/jpeg")
    assert d.sha256 == hashlib.sha256(data).hexdigest()
    assert (d.image.bands, d.image.format) == (3, "uchar")
    out = clean_original(d.image, d.mime)
    assert pyvips.Image.new_from_buffer(out, "").get("vips-loader") == "jpegload_buffer"


def test_heic_decodes_to_png() -> None:
    data = _heic(Image.new("RGB", (64, 48), (200, 30, 90)))
    assert data[4:12] == b"ftypheic"
    d = load_and_validate(data)
    assert (d.width, d.height, d.mime) == (64, 48, "image/png")
    assert d.sha256 == hashlib.sha256(data).hexdigest()
    assert (d.image.bands, d.image.format) == (3, "uchar")
    assert _near(_px(d.image, 10, 10), [200, 30, 90])
    out = clean_original(d.image, d.mime)
    assert out[:4] == b"\x89PNG"
    kept = pyvips.Image.new_from_buffer(out, "")
    assert (kept.width, kept.height) == (64, 48)
    ders = make_derivatives(d.image, d.sha256)
    assert {x.width for x in ders} == {64} and {x.fmt for x in ders} >= {"webp"}
    # The kept PNG goes back through load_and_validate on every crop.
    assert rebuild_public(out, None, False).width == 64


def test_heic_rotation_applied_once() -> None:
    # 80x40, red in the top-left corner, stored with EXIF orientation 6 which the
    # encoder turns into an irot box. Shown, it is 40x80 with the red at the top right.
    im = Image.new("RGB", (80, 40), (10, 200, 30))
    im.paste((255, 0, 0), (0, 0, 16, 16))
    exif = Image.Exif()
    exif[0x0112] = 6
    d = load_and_validate(_heic(im, exif=exif.tobytes()))
    assert (d.width, d.height) == (40, 80)
    assert _near(_px(d.image, 36, 4), [255, 0, 0], tol=30)
    assert _near(_px(d.image, 4, 4), [10, 200, 30], tol=30)
    assert _near(_px(d.image, 36, 76), [10, 200, 30], tol=30)


def test_heic_icc_profile_is_baked_in() -> None:
    p3 = pyvips.Image.black(1, 1, bands=3).cast("uchar").copy(interpretation="srgb")
    p3_icc = p3.icc_transform("p3").get("icc-profile-data")
    rgb = (200, 30, 90)
    want = _p3_to_srgb(rgb)
    assert not _near(want, list(map(float, rgb))), "p3 and srgb must differ for this colour"
    d = load_and_validate(_heic(Image.new("RGB", (64, 48), rgb), icc_profile=p3_icc))
    assert _near(_px(d.image, 10, 10), want)


def test_heic_nclx_p3_is_baked_in() -> None:
    rgb = (200, 30, 90)
    nclx = {
        "color_primaries": 12,
        "transfer_characteristics": 13,
        "matrix_coefficients": 6,
        "full_range_flag": 1,
    }
    d = load_and_validate(_heic(Image.new("RGB", (64, 48), rgb), nclx_profile=nclx))
    assert _near(_px(d.image, 10, 10), _p3_to_srgb(rgb))


@pytest.mark.parametrize("transfer", [16, 18, 2])  # PQ, HLG, unspecified
def test_heic_nclx_p3_other_curve_left_as_decoded(transfer: int) -> None:
    rgb = (200, 30, 90)
    nclx = {
        "color_primaries": 12,
        "transfer_characteristics": transfer,
        "matrix_coefficients": 6,
        "full_range_flag": 1,
    }
    data = _heic(Image.new("RGB", (64, 48), rgb), nclx_profile=nclx)
    info = pillow_heif.open_heif(io.BytesIO(data)).info
    assert info["nclx_profile"]["transfer_characteristics"] == transfer
    assert not info.get("icc_profile")
    d = load_and_validate(data)
    # The P3 bake would move red by about 18; untouched it stays near the source.
    assert not _near(_p3_to_srgb(rgb), list(map(float, rgb)))
    assert _near(_px(d.image, 10, 10), [200, 30, 90])


def test_heic_alpha_survives() -> None:
    d = load_and_validate(_heic(Image.new("RGBA", (64, 48), (200, 30, 90, 128))))
    assert (d.image.bands, d.image.format, d.mime) == (4, "uchar", "image/png")
    assert _near(_px(d.image, 10, 10), [200, 30, 90, 128])
    kept = pyvips.Image.new_from_buffer(clean_original(d.image, d.mime), "")
    assert kept.bands == 4


def test_heic_10_bit_comes_out_8_bit() -> None:
    px = np.zeros((48, 64, 3), np.uint16)
    px[...] = (200 * 257, 30 * 257, 90 * 257)
    b = io.BytesIO()
    pillow_heif.from_bytes(mode="RGB;16", size=(64, 48), data=px.tobytes()).save(b, quality=95)
    assert pillow_heif.open_heif(io.BytesIO(b.getvalue())).info["bit_depth"] == 10
    d = load_and_validate(b.getvalue())
    assert (d.image.bands, d.image.format) == (3, "uchar")
    assert _near(_px(d.image, 10, 10), [200, 30, 90])


def test_heic_10_bit_grey_comes_out_8_bit() -> None:
    px = np.full((40, 64), 120 * 257, np.uint16)
    b = io.BytesIO()
    pillow_heif.from_bytes(mode="L;16", size=(64, 40), data=px.tobytes()).save(b, quality=95)
    d = load_and_validate(b.getvalue())
    assert (d.image.bands, d.image.format) == (1, "uchar")
    assert _near(_px(d.image, 5, 5), [120])


def test_avif_input_decodes() -> None:
    data = _gradient(64, 32).write_to_buffer(".avif")
    assert isinstance(data, bytes)
    assert _loader(data) == "heifload_buffer"
    d = load_and_validate(data)
    assert (d.width, d.height, d.mime) == (64, 32, "image/png")
    assert d.image.format == "uchar"


def test_avif_10_bit_comes_out_8_bit() -> None:
    src = (pyvips.Image.black(64, 32, bands=3) + [200 * 257, 30 * 257, 90 * 257]).cast("ushort")
    data = src.copy(interpretation="rgb16").write_to_buffer(".avif", bitdepth=10, lossless=True)
    assert isinstance(data, bytes)
    d = load_and_validate(data)
    assert (d.image.bands, d.image.format) == (3, "uchar")
    assert _near(_px(d.image, 5, 5), [200, 30, 90])


def test_truncated_heic_raises() -> None:
    data = _heic(Image.new("RGB", (64, 48), (200, 30, 90)))
    with pytest.raises(UndecodableImage, match="heif"):
        load_and_validate(data[: len(data) // 2])


def test_heic_without_pillow_heif_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _heic(Image.new("RGB", (64, 48), (200, 30, 90)))
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    derive._open_heif.cache_clear()
    try:
        with pytest.raises(UndecodableImage, match="heic decoder not installed"):
            load_and_validate(data)
    finally:
        monkeypatch.undo()
        derive._open_heif.cache_clear()
    assert load_and_validate(data).mime == "image/png"


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
