"""Sidecar files that ride along with an inbox image, and grouping them by stem.

A drop is `x.png` plus any of `x_caption.txt`, `x_tags.txt`, ... `x_embedding.json`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# Editors and partial transfers leave these next to real files. Dotfiles are
# handled separately and also cover rsync temp names like `.x.png.AbC123`.
IGNORED_ENDINGS = ("~", ".tmp", ".part", ".swp")


class GroupError(Exception):
    """A processing dir that cannot become one image. `reason` goes into the .err file."""

    def __init__(self, reason: str, unknown: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unknown = unknown or []


@dataclass(frozen=True)
class Embedding:
    model: str
    dim: int
    vector: list[float]


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").strip()


def _lines(p: Path) -> list[str]:
    # dict.fromkeys keeps first-seen order, so the owner's ordering survives dedupe.
    tags = (line.strip().lower() for line in p.read_text(encoding="utf-8").splitlines())
    return list(dict.fromkeys(t for t in tags if t))


def _embedding(p: Path) -> Embedding:
    # JSON only. A .npy variant would need a second file for model and dim, and
    # nothing produces .npy sidecars yet; add it here if that changes.
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("embedding sidecar must be a json object")
    model, dim, vector = raw.get("model"), raw.get("dim"), raw.get("vector")
    if not isinstance(model, str) or not model:
        raise ValueError("embedding sidecar needs a model")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise ValueError("embedding sidecar needs a positive integer dim")
    if not isinstance(vector, list) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in vector
    ):
        raise ValueError("embedding vector must be a list of numbers")
    if len(vector) != dim:
        raise ValueError(f"embedding vector has {len(vector)} values, dim says {dim}")
    return Embedding(model=model, dim=dim, vector=[float(v) for v in vector])


@dataclass(frozen=True)
class SidecarSpec:
    field: str
    parser: Callable[[Path], Any]
    ext: str = ".txt"


# One line per sidecar type. The key is the suffix that sits between the stem
# and the extension.
SIDECARS: dict[str, SidecarSpec] = {
    "_caption": SidecarSpec("caption", _text),
    "_description": SidecarSpec("description", _text),
    "_tags": SidecarSpec("tags", _lines),
    "_prompt": SidecarSpec("prompt", _text),
    "_embedding": SidecarSpec("embedding", _embedding, ".json"),
}


def is_ignored(name: str) -> bool:
    return name.startswith(".") or name.endswith(IGNORED_ENDINGS)


def stem_of(filename: str) -> tuple[str, str] | tuple[None, None]:
    """Return (stem, kind) where kind is "image" or a SIDECARS key; (None, None) otherwise."""
    name = Path(filename).name
    if is_ignored(name):
        return None, None
    lower = name.lower()
    dot = name.rfind(".")
    if dot > 0 and lower[dot:] in IMAGE_EXTS:
        return name[:dot], "image"
    # Longest first, so a future `_alt_caption` beats `_caption` on `x_alt_caption.txt`.
    for suffix in sorted(SIDECARS, key=len, reverse=True):
        ending = suffix + SIDECARS[suffix].ext
        if lower.endswith(ending) and len(name) > len(ending):
            return name[: -len(ending)], suffix
    return None, None


@dataclass
class Group:
    stem: str
    image: Path
    # Keyed by SidecarSpec.field: caption, description, tags, prompt, embedding.
    values: dict[str, Any] = field(default_factory=dict)
    sidecars: dict[str, Path] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)


def parse_group(directory: Path) -> Group:
    """Read a claimed processing dir. Raises GroupError if it is not exactly one image."""
    images: list[Path] = []
    sidecars: dict[str, Path] = {}
    stems: dict[str, str] = {}
    unknown: list[str] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            unknown.append(p.name)
            continue
        stem, kind = stem_of(p.name)
        if stem is None or kind is None:
            unknown.append(p.name)
        elif kind == "image":
            images.append(p)
        elif kind in sidecars:
            # Only reachable via case variants (x_caption.txt vs x_CAPTION.txt).
            raise GroupError(f"duplicate {kind} sidecar: {p.name}", unknown)
        else:
            sidecars[kind] = p
            stems[kind] = stem

    if not images:
        raise GroupError("no image", unknown)
    if len(images) > 1:
        raise GroupError("more than one image: " + ", ".join(p.name for p in images), unknown)
    image = images[0]
    image_stem, _ = stem_of(image.name)
    assert image_stem is not None

    group = Group(stem=image_stem, image=image, unknown=unknown)
    for kind, p in sidecars.items():
        if stems[kind] != image_stem:
            group.unknown.append(p.name)
            continue
        spec = SIDECARS[kind]
        try:
            group.values[spec.field] = spec.parser(p)
        except (ValueError, UnicodeDecodeError) as e:
            raise GroupError(f"{p.name}: {e}", group.unknown) from e
        group.sidecars[spec.field] = p
    return group
