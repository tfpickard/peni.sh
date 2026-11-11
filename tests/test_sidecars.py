from __future__ import annotations

import json
from pathlib import Path

import pytest

from penish.worker.sidecars import SIDECARS, Embedding, GroupError, parse_group, stem_of


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x.png", ("x", "image")),
        ("x.PNG", ("x", "image")),
        ("x.jpg", ("x", "image")),
        ("x.JPEG", ("x", "image")),
        ("x.webp", ("x", "image")),
        ("my_cat.png", ("my_cat", "image")),
        ("my_cat_caption.txt", ("my_cat", "_caption")),
        ("my_cat_description.txt", ("my_cat", "_description")),
        ("my_cat_tags.txt", ("my_cat", "_tags")),
        ("my_cat_prompt.txt", ("my_cat", "_prompt")),
        ("my_cat_embedding.json", ("my_cat", "_embedding")),
        ("a_caption_caption.txt", ("a_caption", "_caption")),
        # The suffix decides the kind even when the image itself has a suffix-like stem.
        ("x_caption.png", ("x_caption", "image")),
        ("/some/dir/x_tags.txt", ("x", "_tags")),
    ],
)
def test_stem_of_known(name: str, expected: tuple[str, str]) -> None:
    assert stem_of(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        ".x.png.AbC123",  # rsync temp
        ".x_caption.txt.Zz9",
        ".DS_Store",
        ".hidden.png",
        "x.png~",
        "x.png.tmp",
        "x.png.part",
        "x_caption.txt.swp",
        "x.gif",
        "x.txt",
        "x_caption.json",  # wrong extension for a text sidecar
        "x_embedding.txt",
        "_caption.txt",  # empty stem
        "notes",
    ],
)
def test_stem_of_ignored(name: str) -> None:
    assert stem_of(name) == (None, None)


def test_registry_shape() -> None:
    assert set(SIDECARS) == {"_caption", "_description", "_tags", "_prompt", "_embedding"}


def _write(d: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        (d / name).write_text(body)


def test_parse_group_full(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "my_cat.png": "img",
            "my_cat_caption.txt": "  a cat  \n",
            "my_cat_description.txt": "A cat on a mat.\n",
            "my_cat_tags.txt": "Cat\n\n  mat \ncat\nMAT\n  \nfur\n",
            "my_cat_prompt.txt": "draw a cat",
            "my_cat_embedding.json": json.dumps({"model": "m", "dim": 3, "vector": [1, 0.5, 0]}),
            "readme.md": "?",
        },
    )
    g = parse_group(tmp_path)
    assert g.stem == "my_cat"
    assert g.image == tmp_path / "my_cat.png"
    assert g.values["caption"] == "a cat"
    assert g.values["description"] == "A cat on a mat."
    assert g.values["tags"] == ["cat", "mat", "fur"]
    assert g.values["prompt"] == "draw a cat"
    assert g.values["embedding"] == Embedding("m", 3, [1.0, 0.5, 0.0])
    assert g.unknown == ["readme.md"]


def test_parse_group_image_only(tmp_path: Path) -> None:
    _write(tmp_path, {"x.jpg": "img"})
    g = parse_group(tmp_path)
    assert g.stem == "x" and g.values == {} and g.unknown == []


def test_parse_group_no_image(tmp_path: Path) -> None:
    _write(tmp_path, {"x_caption.txt": "c", "junk.bin": ""})
    with pytest.raises(GroupError) as e:
        parse_group(tmp_path)
    assert e.value.reason == "no image"
    assert e.value.unknown == ["junk.bin"]


def test_parse_group_two_images(tmp_path: Path) -> None:
    _write(tmp_path, {"x.png": "a", "x.jpg": "b"})
    with pytest.raises(GroupError, match="more than one image"):
        parse_group(tmp_path)


def test_parse_group_mismatched_stem_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, {"x.png": "a", "y_caption.txt": "c"})
    g = parse_group(tmp_path)
    assert g.values == {} and g.unknown == ["y_caption.txt"]


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"dim": 2, "vector": [1, 2]}),
        json.dumps({"model": "m", "dim": 3, "vector": [1, 2]}),
        json.dumps({"model": "m", "dim": 2, "vector": [1, "a"]}),
    ],
)
def test_parse_group_bad_embedding(tmp_path: Path, body: str) -> None:
    _write(tmp_path, {"x.png": "a", "x_embedding.json": body})
    with pytest.raises(GroupError, match="x_embedding.json"):
        parse_group(tmp_path)
