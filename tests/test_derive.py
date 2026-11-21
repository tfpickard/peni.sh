import hashlib
import io
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, cast

import numpy as np
import pillow_heif
import pytest
import pyvips
from PIL import Image

from penish.config import Settings
from penish.storage import LocalStorage
from penish.worker import admin_jobs, derive, ingest
from penish.worker.derive import (
    MIN_CROP,
    UndecodableImage,
    blurhash_of,
    clamp_rect,
    clean_original,
    load_and_validate,
    make_derivatives,
    make_og,
    palette_of,
    rebuild_public,
)
from penish.worker.jobs import HardError
from tests.test_admin_jobs import crop, job, live, row
from tests.test_ingest import DIM, q


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
    with pytest.raises(UndecodableImage, match=r"need png, jpeg, webp or heic$"):
        load_and_validate(tif)


# -- the pixel cap ------------------------------------------------------------------


def _png_claiming(width: int, height: int) -> bytes:
    """A 1-bit grey PNG of the stated size. Blank rows deflate to almost nothing, so the
    file is a few kilobytes while the header claims any number of pixels."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    packer = zlib.compressobj(9)
    row = bytes((width + 7) // 8 + 1)
    idat = b"".join(packer.compress(row) for _ in range(height)) + packer.flush()
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    sig = b"\x89PNG\r\n\x1a\n"
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_cap_is_above_every_real_upload() -> None:
    # The largest seen is 6048x6048; iPhones make 48 megapixel files.
    derive._check_size(6048, 6048)
    derive._check_size(8064, 6048)
    derive._check_size(derive.MAX_PIXELS, 1)
    with pytest.raises(UndecodableImage):
        derive._check_size(derive.MAX_PIXELS, 2)


def test_oversized_header_is_refused_before_decode_in_libvips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def decoded(*_: object) -> None:
        raise AssertionError("decode reached")

    monkeypatch.setattr(pyvips.Image, "copy_memory", decoded)
    data = _png_claiming(20000, 6000)
    assert len(data) < 100_000
    with pytest.raises(UndecodableImage, match=r"picture is 120\.0 megapixels; the limit is 100$"):
        load_and_validate(data)


def test_a_48_megapixel_picture_is_decoded() -> None:
    d = load_and_validate(_png_claiming(8064, 6048))
    assert (d.width, d.height) == (8064, 6048)


def test_oversized_heic_is_refused_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _heic(Image.new("RGB", (64, 48), (200, 30, 90)))

    def decoded(*_: object, **__: object) -> None:
        raise AssertionError("decode reached")

    # A real 100 megapixel HEIC is too much to build in a test, so the cap comes down to
    # 3071 pixels instead of the picture going up; the check is the same comparison.
    monkeypatch.setattr(derive, "MAX_PIXELS", 64 * 48 - 1)
    monkeypatch.setattr(pillow_heif.HeifFile, "to_pillow", decoded)
    with pytest.raises(UndecodableImage, match=r"picture is 0\.0 megapixels"):
        derive._heif_via_pillow(data)
    # And the whole way in, whichever loader reads the header first.
    monkeypatch.setattr(pyvips.Image, "copy_memory", decoded)
    with pytest.raises(UndecodableImage, match="megapixels"):
        load_and_validate(data)


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


# -- og jpeg ------------------------------------------------------------------


def _exif_fields(data: bytes) -> list[str]:
    img = pyvips.Image.new_from_buffer(data, "")
    return [f for f in img.get_fields() if f.startswith(("exif", "xmp", "iptc", "icc"))]


def _gps_jpeg_wide() -> bytes:
    img = _gradient(2000, 1000).copy()
    img.set_type(pyvips.GValue.gstr_type, "exif-ifd3-GPSLatitudeRef", "N")
    img.set_type(pyvips.GValue.gstr_type, "exif-ifd3-GPSLongitudeRef", "E")
    out = img.write_to_buffer(".jpg", keep="all")
    assert isinstance(out, bytes)
    return out


def test_og_is_a_1200_progressive_jpeg_without_metadata() -> None:
    src = _gps_jpeg_wide()
    assert _has_gps(src), "fixture must actually carry GPS"
    d = load_and_validate(src)
    og = make_og(d.image, d.sha256)
    assert og is not None
    assert (og.key, og.fmt) == (f"pub/{d.sha256}-1200.jpg", "jpg")
    assert (og.width, og.height) == (1200, 600)
    img = pyvips.Image.new_from_buffer(og.data, "")
    assert str(img.get("vips-loader")) == "jpegload_buffer"
    assert (img.width, img.height, img.bands) == (1200, 600, 3)
    assert img.get("interlaced") == 1
    assert not _has_gps(og.data) and _exif_fields(og.data) == []


def test_og_never_upscales() -> None:
    d = load_and_validate(_png(800, 600))
    og = make_og(d.image, d.sha256)
    assert og is not None
    assert og.key == f"pub/{d.sha256}-800.jpg" and (og.width, og.height) == (800, 600)
    img = pyvips.Image.new_from_buffer(og.data, "")
    assert (img.width, img.height) == (800, 600)


@pytest.mark.parametrize("w,h", [(4032, 3024), (3000, 1999), (1201, 7), (1999, 3001), (1200, 1)])
def test_og_size_is_the_file_size(w: int, h: int) -> None:
    # The api states og_size in the head without opening the file, so they must agree.
    og = make_og(_gradient(w, h), "a" * 64)
    assert og is not None
    img = pyvips.Image.new_from_buffer(og.data, "")
    assert (img.width, img.height) == (og.width, og.height) == derive.og_size(w, h)
    assert og.key == derive.og_key("a" * 64, w)


def test_og_flattens_alpha_and_grey_to_rgb() -> None:
    rgba = _gradient(300, 200).bandjoin(128).copy(interpretation="srgb")
    grey = _gradient(300, 200).colourspace("b-w")
    for src in (rgba, grey):
        data = src.write_to_buffer(".png")
        d = load_and_validate(data)
        og = make_og(d.image, d.sha256, watermark=True)
        assert og is not None
        img = pyvips.Image.new_from_buffer(og.data, "")
        assert (img.bands, img.interpretation) == (3, "srgb")


def _noise(w: int, h: int) -> pyvips.Image:
    # Busy on purpose: noise is the worst case for a JPEG encoder.
    arr = np.random.default_rng(7).integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    return pyvips.Image.new_from_memory(arr.tobytes(), w, h, 3, "uchar").copy(interpretation="srgb")


def test_og_over_the_cap_is_re_encoded_at_lower_quality() -> None:
    img = _noise(1200, 1600)
    q85 = len(derive._save(img, ".jpg", Q=85, interlace=True, optimize_coding=True))
    assert q85 > derive.OG_MAX_BYTES, "fixture must exceed the cap at Q85"
    og = make_og(img, "a" * 64)
    assert og is not None
    # Stepped down the ladder, never past it: the first encode under the cap, else the last.
    sizes = [
        len(derive._save(img, ".jpg", Q=q, interlace=True, optimize_coding=True))
        for q in (85, *derive.OG_FALLBACK_QUALITIES)
    ]
    first_under = next((n for n in sizes if n <= derive.OG_MAX_BYTES), sizes[-1])
    assert len(og.data) == first_under
    assert len(og.data) < q85


def test_og_under_the_cap_stays_at_q85(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    real = derive._save

    def spy(img: pyvips.Image, suffix: str, **opts: object) -> bytes:
        seen.append(opts.get("Q"))
        return real(img, suffix, **opts)

    monkeypatch.setattr(derive, "_save", spy)
    assert make_og(_gradient(300, 200), "a" * 64) is not None
    assert seen == [derive.OG_QUALITY]


def test_og_keeps_the_last_encode_when_none_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derive, "OG_MAX_BYTES", 10)
    og = make_og(_gradient(300, 200), "a" * 64)
    assert og is not None
    expected = derive._save(
        _gradient(300, 200).colourspace("srgb"),
        ".jpg",
        Q=derive.OG_FALLBACK_QUALITIES[-1],
        interlace=True,
        optimize_coding=True,
    )
    assert og.data == expected


def test_og_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: object, **kw: object) -> bytes:
        raise pyvips.Error("no jpeg today")

    monkeypatch.setattr(derive, "_save", broken)
    assert make_og(_gradient(300, 200), "a" * 64) is None


def test_rebuild_public_carries_og() -> None:
    data = _clean()
    pub = rebuild_public(data, None, False)
    assert pub.og is not None and pub.og.key == f"pub/{pub.pub_sha}-1200.jpg"
    # Not in the ladder: the publish gate and the variants never see it.
    assert all(x.fmt in ("avif", "webp") for x in pub.derivatives)
    cropped = rebuild_public(data, {"x": 100, "y": 50, "w": 700, "h": 400}, False)
    assert cropped.og is not None
    assert cropped.og.key == f"pub/{cropped.pub_sha}-700.jpg"
    assert (cropped.og.width, cropped.og.height) == (700, 400)


# -- og jpeg through the worker -------------------------------------------------------
# Here rather than beside the ingest and crop tests: the og JPEG's whole lifecycle in one
# place, from the derive that makes it to the sweep that backfills it.


@pytest.fixture
def p(pool: object) -> Any:
    return cast(Any, pool)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, embed_model="test/embed", embed_dim=DIM)


def _jpgs(settings: Settings) -> list[str]:
    return sorted(f.name for f in settings.media_pub.iterdir() if f.name.endswith(".jpg"))


async def _og_jobs(p: Any) -> list[dict[str, Any]]:
    return await q(p, "select * from jobs where type = %s order by id", admin_jobs.OG_JOB)


async def test_og_written_at_ingest(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings, w=1600, h=1000)
    r = await row(p, image_id)
    assert _jpgs(settings) == [f"{r['pub_sha']}-1200.jpg"]
    img = pyvips.Image.new_from_file(str(settings.media_pub / f"{r['pub_sha']}-1200.jpg"))
    assert (img.width, img.height) == (1200, 750)


async def test_og_failure_never_blocks_go_live(
    p: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest, "make_og", lambda *a, **kw: None)
    first = await live(p, settings, seed=1)
    assert (await row(p, first))["status"] == "live" and _jpgs(settings) == []
    monkeypatch.undo()

    real = LocalStorage.put_bytes

    def no_jpeg(self: LocalStorage, key: str, data: bytes) -> None:
        if key.endswith(".jpg"):
            raise OSError("disk full")
        real(self, key, data)

    monkeypatch.setattr(LocalStorage, "put_bytes", no_jpeg)
    second = await live(p, settings, seed=2)
    assert (await row(p, second))["status"] == "live" and _jpgs(settings) == []


async def test_og_sweep_queues_once_and_staggers(p: Any, settings: Settings) -> None:
    a = await live(p, settings, seed=1)
    b = await live(p, settings, seed=2)
    c = await live(p, settings, seed=3)
    rows = {i: await row(p, i) for i in (a, b, c)}
    async with p.connection() as conn:
        # A second picture on a's pub_sha shares a's file, and a hidden one is not swept.
        await conn.execute(
            "insert into images (sha256, pub_sha, status, width, height)"
            " values (%s, %s, 'live', 1200, 800)",
            ("f" * 64, rows[a]["pub_sha"]),
        )
        await conn.execute("update images set status = 'unpublished' where id = %s", (c,))
    for f in settings.media_pub.glob("*.jpg"):
        f.unlink()

    assert await admin_jobs.enqueue_missing_og(p, settings) == 2
    assert await admin_jobs.enqueue_missing_og(p, settings) == 0
    js = await _og_jobs(p)
    assert [j["payload"]["image_id"] for j in js] == [a, b]
    assert [j["dedupe_key"] for j in js] == [
        f"og_image:{rows[a]['pub_sha']}",
        f"og_image:{rows[b]['pub_sha']}",
    ]
    late = await q(p, "select now() + interval '2 minutes' as t")
    # Behind anything an upload queues now, and one after another rather than all at once.
    assert all(j["run_after"] > late[0]["t"] for j in js)
    assert js[0]["run_after"] < js[1]["run_after"]

    for j in js:
        await admin_jobs.handle_og(
            job(admin_jobs.OG_JOB, j["payload"], j["id"]), pool=p, settings=settings
        )
    assert _jpgs(settings) == sorted(f"{rows[i]['pub_sha']}-1200.jpg" for i in (a, b))
    async with p.connection() as conn:
        await conn.execute("update jobs set status = 'done'")
    assert await admin_jobs.enqueue_missing_og(p, settings) == 0

    # A failed job is not retried at every start.
    (settings.media_pub / f"{rows[b]['pub_sha']}-1200.jpg").unlink()
    async with p.connection() as conn:
        await conn.execute(
            "update jobs set status = 'failed' where dedupe_key = %s",
            (f"og_image:{rows[b]['pub_sha']}",),
        )
    assert await admin_jobs.enqueue_missing_og(p, settings) == 0


async def test_og_job_for_a_cropped_picture(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    await crop(p, settings, image_id, {"x": 100, "y": 50, "w": 600, "h": 500})
    r = await row(p, image_id)
    name = f"{r['pub_sha']}-600.jpg"
    made = (settings.media_pub / name).read_bytes()
    (settings.media_pub / name).unlink()
    assert await admin_jobs.enqueue_missing_og(p, settings) == 1
    (j,) = await _og_jobs(p)
    await admin_jobs.handle_og(
        job(admin_jobs.OG_JOB, j["payload"], j["id"]), pool=p, settings=settings
    )
    # From the cropped pixels, the same file the crop wrote.
    assert (settings.media_pub / name).read_bytes() == made


async def test_og_follows_the_crop(p: Any, settings: Settings) -> None:
    image_id = await live(p, settings)
    old = (await row(p, image_id))["pub_sha"]
    assert _jpgs(settings) == [f"{old}-1200.jpg"]
    await admin_jobs.enqueue_missing_og(p, settings)

    await crop(p, settings, image_id, {"x": 100, "y": 50, "w": 600, "h": 500})
    new = (await row(p, image_id))["pub_sha"]
    assert _jpgs(settings) == [f"{new}-600.jpg"]
    img = pyvips.Image.new_from_file(str(settings.media_pub / f"{new}-600.jpg"))
    assert (img.width, img.height) == (600, 500)

    # A job queued for the old framing finds the picture moved on and leaves it be.
    stale = job(admin_jobs.OG_JOB, {"image_id": image_id, "pub_sha": old})
    await admin_jobs.handle_og(stale, pool=p, settings=settings)
    assert _jpgs(settings) == [f"{new}-600.jpg"]


async def test_og_job_for_a_deleted_picture(p: Any, settings: Settings) -> None:
    gone = job(admin_jobs.OG_JOB, {"image_id": 999, "pub_sha": "a" * 64})
    await admin_jobs.handle_og(gone, pool=p, settings=settings)
    with pytest.raises(HardError):
        await admin_jobs.handle_og(job(admin_jobs.OG_JOB, {}), pool=p, settings=settings)


def test_delete_takes_the_og(tmp_path: Path) -> None:
    sha = "b" * 64
    for name in (f"{sha}-480.webp", f"{sha}-480.avif", f"{sha}-1200.jpg", f"{'c' * 64}-1200.jpg"):
        (tmp_path / name).write_bytes(b"x")
    assert admin_jobs.remove_pub(tmp_path, sha) == 3
    assert [f.name for f in tmp_path.iterdir()] == [f"{'c' * 64}-1200.jpg"]
