from __future__ import annotations

import json
from pathlib import Path

import pytest

from penish.worker.sidecars import (
    SIDECARS,
    Arrival,
    Embedding,
    GroupError,
    arrivals,
    parse_group,
    stem_of,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x.png", ("x", "image")),
        ("x.PNG", ("x", "image")),
        ("x.jpg", ("x", "image")),
        ("x.JPEG", ("x", "image")),
        ("x.webp", ("x", "image")),
        ("x.heic", ("x", "image")),
        ("IMG_0938.HEIC", ("IMG_0938", "image")),
        ("x.heif", ("x", "image")),
        ("x.HEIF", ("x", "image")),
        ("my_cat.png", ("my_cat", "image")),
        ("my_cat_caption.txt", ("my_cat", "_caption")),
        ("my_cat_description.txt", ("my_cat", "_description")),
        ("my_cat_analysis.txt", ("my_cat", "_analysis")),
        ("my_cat_tags.txt", ("my_cat", "_tags")),
        ("my_cat_prompt.txt", ("my_cat", "_prompt")),
        ("my_cat_embedding.json", ("my_cat", "_embedding")),
        ("my_cat_collection.txt", ("my_cat", "_collection")),
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
    assert set(SIDECARS) == {
        "_caption",
        "_description",
        "_analysis",
        "_tags",
        "_prompt",
        "_embedding",
        "_collection",
        "_note",
    }


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
            "my_cat_analysis.txt": "Subject: a cat.\n\nMood: flat.\n",
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
    assert g.values["analysis"] == "Subject: a cat.\n\nMood: flat."
    assert g.values["tags"] == ["cat", "mat", "fur"]
    assert g.values["prompt"] == "draw a cat"
    assert g.values["embedding"] == Embedding("m", 3, [1.0, 0.5, 0.0])
    assert g.unknown == ["readme.md"]


def test_parse_group_image_only(tmp_path: Path) -> None:
    _write(tmp_path, {"x.jpg": "img"})
    g = parse_group(tmp_path)
    assert g.stem == "x" and g.values == {} and g.unknown == []


@pytest.mark.parametrize("image", ["x.heic", "IMG_0938.HEIC", "x.heif"])
def test_parse_group_heic_with_collection(tmp_path: Path, image: str) -> None:
    stem = image.rsplit(".", 1)[0]
    _write(tmp_path, {image: "img", f"{stem}_collection.txt": "  the line  \n"})
    g = parse_group(tmp_path)
    assert g.stem == stem and g.image == tmp_path / image
    assert g.values == {"collection": "the line"} and g.unknown == []


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


def test_arrivals_ignores_a_bad_embedding(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"x.png": "a", "x_collection.txt": "pansophicon iii\n", "x_embedding.json": "not json"},
    )
    assert arrivals(tmp_path) == [Arrival("x", tmp_path / "x.png", "pansophicon iii")]


def test_arrivals_lists_every_image(tmp_path: Path) -> None:
    _write(tmp_path, {"a.png": "a", "b.jpg": "b", "b_collection.txt": "c"})
    assert arrivals(tmp_path) == [
        Arrival("a", tmp_path / "a.png", None),
        Arrival("b", tmp_path / "b.jpg", "c"),
    ]


def test_arrivals_without_an_image(tmp_path: Path) -> None:
    _write(tmp_path, {"x_collection.txt": "c"})
    assert arrivals(tmp_path) == []


def test_arrivals_skips_another_stems_collection(tmp_path: Path) -> None:
    _write(tmp_path, {"x.png": "a", "y_collection.txt": "c"})
    assert arrivals(tmp_path) == [Arrival("x", tmp_path / "x.png", None)]


def test_arrivals_reads_a_collection_that_is_not_utf8(tmp_path: Path) -> None:
    _write(tmp_path, {"x.png": "a"})
    (tmp_path / "x_collection.txt").write_bytes(b"pans\xffphicon")
    [got] = arrivals(tmp_path)
    assert got.collection == "pans\ufffdphicon"


def test_arrivals_of_a_missing_folder(tmp_path: Path) -> None:
    assert arrivals(tmp_path / "gone") == []


def test_arrivals_reads_an_empty_collection_as_no_line(tmp_path: Path) -> None:
    (tmp_path / "x.png").write_bytes(b"x")
    (tmp_path / "x_collection.txt").write_text("  \n")
    assert arrivals(tmp_path) == [Arrival("x", tmp_path / "x.png", None)]
