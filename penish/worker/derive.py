import hashlib
import io
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np
import pyvips
from blurhash import encode as _blurhash_encode

log = logging.getLogger(__name__)

WIDTHS = (480, 960, 1600)
# Smaller than this and the 480 rung, the blurhash and the palette are all mush.
MIN_CROP = 32

# Keyed on vips' own loader name rather than the file extension or sniffed
# magic bytes, so what we accept is exactly what libvips actually decoded.
_LOADERS = {
    "pngload_buffer": ("image/png", "png"),
    "jpegload_buffer": ("image/jpeg", "jpg"),
    "webpload_buffer": ("image/webp", "webp"),
}
_SAVE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class UndecodableImage(Exception):
    pass


@dataclass
class Decoded:
    width: int
    height: int
    mime: str
    sha256: str
    image: pyvips.Image


@dataclass
class Derivative:
    key: str
    data: bytes
    width: int
    height: int
    fmt: str


def load_and_validate(data: bytes) -> Decoded:
    try:
        # fail=True makes libvips error on truncation instead of padding with grey.
        img = pyvips.Image.new_from_buffer(data, "", access="sequential", fail=True)
    except pyvips.Error as e:
        raise UndecodableImage("not a readable image") from e

    loader = str(img.get("vips-loader")) if img.get_typeof("vips-loader") else ""
    if loader not in _LOADERS:
        raise UndecodableImage(
            f"unsupported format ({loader or 'unknown'}); need png, jpeg or webp"
        )
    mime, _ = _LOADERS[loader]

    try:
        # Forces a full decode now, so a truncated file fails here and not mid-pipeline.
        img = img.copy_memory()
        img = img.autorot()
        if img.get_typeof("icc-profile-data"):
            # Metadata is stripped later, so bake the profile into pixels first or
            # wide-gamut images would shift colour once the ICC block is gone.
            try:
                img = img.icc_transform("srgb")
            except pyvips.Error:
                log.warning("icc transform failed; keeping pixels as-is")
    except pyvips.Error as e:
        raise UndecodableImage("image data is truncated or corrupt") from e

    return Decoded(
        width=img.width,
        height=img.height,
        mime=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        image=img,
    )


def _save(img: pyvips.Image, suffix: str, **opts: object) -> bytes:
    try:
        out = img.write_to_buffer(suffix, keep="none", **opts)
    except TypeError:
        # Older pyvips/libvips predate `keep`.
        out = img.write_to_buffer(suffix, strip=True, **opts)
    assert isinstance(out, bytes)
    return out


def clean_original(img: pyvips.Image, mime: str) -> bytes:
    try:
        suffix = _SAVE_SUFFIX[mime]
    except KeyError:
        raise ValueError(f"unsupported mime {mime!r}") from None
    if mime == "image/jpeg" and img.hasalpha():
        img = img.flatten(background=[255, 255, 255])
    opts: dict[str, object] = {}
    if mime == "image/jpeg":
        opts["Q"] = 92
    elif mime == "image/webp":
        # Lossless so the "original" is not degraded a second time.
        opts["lossless"] = True
    return _save(img, suffix, **opts)


def _watermark(img: pyvips.Image) -> pyvips.Image:
    h = max(12, img.height // 24)
    try:
        text = pyvips.Image.text("peni.sh", font=f"sans bold {h}", rgba=True)
    except pyvips.Error:
        log.warning("vips lacks text rendering; skipping watermark")
        return img
    text = text.copy(interpretation="srgb")
    # Alpha is scaled down so it stays a quiet mark rather than a banner.
    alpha = (text[3] * 0.35).cast("uchar")
    mark = text[:3].bandjoin(alpha)
    margin = max(4, h // 2)
    x = max(0, img.width - mark.width - margin)
    y = max(0, img.height - mark.height - margin)
    base = img if img.hasalpha() else img.bandjoin(255)
    # composite2 uses the premultiplied-alpha-aware blend; bands must match.
    return base.composite2(mark, "over", x=x, y=y)


def make_derivatives(img: pyvips.Image, sha: str, watermark: bool = False) -> list[Derivative]:
    src_w = img.width
    widths = [w for w in WIDTHS if w <= src_w]
    if not widths:
        # Smaller than the smallest rung: ship one derivative at native size.
        widths = [src_w]

    out: list[Derivative] = []
    for w in widths:
        scaled = img if w == src_w else img.thumbnail_image(w, size="down")
        if watermark:
            scaled = _watermark(scaled)
        for fmt in ("avif", "webp"):
            try:
                if fmt == "avif":
                    data = _save(scaled, ".avif", Q=50, effort=4, compression="av1")
                else:
                    data = _save(scaled, ".webp", Q=80)
            except pyvips.Error:
                if fmt == "avif":
                    log.warning("AVIF encoding unavailable in this libvips; skipping avif")
                    continue
                raise
            out.append(
                Derivative(
                    key=f"pub/{sha}-{w}.{fmt}",
                    data=data,
                    width=scaled.width,
                    height=scaled.height,
                    fmt=fmt,
                )
            )
    return out


def _small_rgb(img: pyvips.Image, size: int) -> pyvips.Image:
    t = img.thumbnail_image(size, size="down")
    if t.hasalpha():
        t = t.flatten(background=[0, 0, 0])
    t = t.colourspace("srgb")
    if t.bands == 1:
        t = t.bandjoin([t, t])
    return t[:3].cast("uchar")


def blurhash_of(img: pyvips.Image) -> str:
    t = _small_rgb(img, 32)
    # blurhash-python only takes a PIL image or a file-like, so go through PNG.
    buf = t.write_to_buffer(".png")
    return str(_blurhash_encode(io.BytesIO(buf), 4, 3))


def palette_of(img: pyvips.Image, n: int = 5) -> list[str]:
    t = _small_rgb(img, 64)
    px = np.frombuffer(t.write_to_memory(), dtype=np.uint8).reshape(-1, 3).astype(np.float64)
    # Fixed seed: the same image must always yield the same palette.
    rng = np.random.default_rng(0)
    uniq = np.unique(px, axis=0)
    k = min(n, len(uniq))
    centers = uniq[rng.choice(len(uniq), size=k, replace=False)]
    for _ in range(20):
        d = ((px[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels = d.argmin(axis=1)
        new = centers.copy()
        for i in range(k):
            members = px[labels == i]
            if len(members):
                new[i] = members.mean(axis=0)
        if np.allclose(new, centers):
            break
        centers = new
    counts = np.bincount(labels, minlength=k)
    order = np.argsort(-counts, kind="stable")
    hexes = ["#{:02x}{:02x}{:02x}".format(*(int(round(c)) for c in centers[i])) for i in order]
    # Flat images have fewer distinct colours than n; repeat the dominant so callers get n.
    while len(hexes) < n:
        hexes.append(hexes[0])
    return hexes


class Public(NamedTuple):
    pub_sha: str
    derivatives: list[Derivative]
    width: int
    height: int
    blurhash: str
    palette: list[str]


def clamp_rect(rect: Mapping[str, Any], width: int, height: int) -> dict[str, int] | None:
    """Clamp {x,y,w,h} (original pixels) into the frame.

    Raises ValueError for a malformed rect or one that ends up under MIN_CROP on a side.
    Returns None when the clamped rect is the whole frame, so callers store it as uncropped.
    """
    vals: dict[str, int] = {}
    for k in ("x", "y", "w", "h"):
        v = rect.get(k) if isinstance(rect, Mapping) else None
        # bool is an int subclass; True as a coordinate is a client bug, not a pixel.
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise ValueError(f"crop {k} must be a finite number")
        vals[k] = round(v)
    x0 = min(max(vals["x"], 0), width)
    y0 = min(max(vals["y"], 0), height)
    x1 = min(max(vals["x"] + vals["w"], 0), width)
    y1 = min(max(vals["y"] + vals["h"], 0), height)
    w, h = x1 - x0, y1 - y0
    if w < MIN_CROP or h < MIN_CROP:
        raise ValueError(f"crop must be at least {MIN_CROP}x{MIN_CROP} inside the image")
    if (x0, y0, w, h) == (0, 0, width, height):
        return None
    return {"x": x0, "y": y0, "w": w, "h": h}


def pub_hash(img: pyvips.Image, watermark: bool) -> str:
    # Hash of the displayed pixels, not of any encoded file: deterministic, so a retried
    # crop job lands on the same names, and the watermark flag is folded in because the
    # files are served immutable and a toggle must change every URL.
    h = hashlib.sha256(b"penish-pub-v1\0")
    h.update(f"{img.width}x{img.height}x{img.bands}:{img.format}:{int(watermark)}\0".encode())
    h.update(img.write_to_memory())
    return h.hexdigest()


def rebuild_public(
    original_bytes: bytes, crop_rect: Mapping[str, Any] | None, watermark: bool
) -> Public:
    """Everything public about an image, recomputed from the clean original.

    crop_rect is in original pixels and is clamped first (ValueError if unusable). The
    caller writes the derivatives, then sets pub_sha, width, height, blurhash and palette.
    """
    img = load_and_validate(original_bytes).image
    if crop_rect is not None:
        rect = clamp_rect(crop_rect, img.width, img.height)
        if rect is not None:
            img = img.crop(rect["x"], rect["y"], rect["w"], rect["h"]).copy_memory()
    sha = pub_hash(img, watermark)
    return Public(
        pub_sha=sha,
        derivatives=make_derivatives(img, sha, watermark),
        width=img.width,
        height=img.height,
        blurhash=blurhash_of(img),
        palette=palette_of(img),
    )
