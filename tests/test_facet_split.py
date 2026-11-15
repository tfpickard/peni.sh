"""The facet splitter and the names it shares with the store. Pure."""

from __future__ import annotations

import pytest

from penish.mycelium import facets
from penish.worker.describe import check_analysis

LABELLED = (
    "Subject: a bulb on a flex, unlit.\n\n"
    "Setting: a fern house, wet glass.\n\n"
    "Style: phone photo, flash, slight blur.\n\n"
    "Mood: patient."
)


def test_labelled_analysis_splits_in_facet_order() -> None:
    got = facets.split(LABELLED)
    assert got == {
        "subject": "a bulb on a flex, unlit.",
        "setting": "a fern house, wet glass.",
        "style": "phone photo, flash, slight blur.",
        "mood": "patient.",
    }
    assert got is not None and list(got) == list(facets.FACETS)


def test_multiline_parts_keep_their_lines() -> None:
    text = "Subject: one\nline two\n\nSetting: s\nStyle: t\nMood: m\n"
    got = facets.split(text)
    assert got is not None
    assert got["subject"] == "one\nline two"
    assert (got["setting"], got["style"], got["mood"]) == ("s", "t", "m")


def test_extra_whitespace_is_trimmed() -> None:
    text = (
        "\n\n   Subject :   a cup  \n\n\n\t Setting:\ta table\n Style :photo\nMood:   flat   \n\n"
    )
    assert facets.split(text) == {
        "subject": "a cup",
        "setting": "a table",
        "style": "photo",
        "mood": "flat",
    }


@pytest.mark.parametrize(
    "text",
    [
        "\u00a0Subject: a goat.\nSetting: a lawn.\nStyle: flash.\nMood: calm.",
        "Subject: a goat.\nSetting: a lawn.\nStyle\u00a0: flash.\nMood: calm.",
        "Subject: a goat.\n\u2003Setting: a lawn.\nStyle: flash.\nMood: calm.",
    ],
)
def test_what_describe_accepts_splits(text: str) -> None:
    # Validation and the splitter must agree, or a correctly labelled analysis buys a
    # capped extract call.
    check_analysis(text)
    got = facets.split(text)
    assert got is not None and got["subject"] == "a goat." and got["mood"] == "calm."


def test_a_preamble_belongs_to_no_facet() -> None:
    got = facets.split("Notes first.\n" + LABELLED)
    assert got is not None and got["subject"] == "a bulb on a flex, unlit."


def test_reordered_labels_do_not_split() -> None:
    text = "Setting: s\nSubject: a\nStyle: t\nMood: m"
    assert facets.split(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Subject: a\nSetting: s\nStyle: t",
        "Subject: a\nSetting: s\nStyle: t\nMood: m\nMood: again",
        "Subject: a\nSubject: b\nSetting: s\nStyle: t\nMood: m",
        "subject: a\nsetting: s\nstyle: t\nmood: m",
    ],
)
def test_missing_repeated_or_lowercase_labels_do_not_split(text: str) -> None:
    assert facets.split(text) is None


def test_an_empty_part_does_not_split() -> None:
    assert facets.split("Subject: a\nSetting:\nStyle: t\nMood: m") is None
    assert facets.split("Subject: a\nSetting: s\nStyle: t\nMood:   \n") is None


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Subject: mine.",
        "A bulb in a fern house. I took it the week the heating broke.",
        # A label word inside a line is prose, not a label.
        "The subject: a bulb. The setting: ferns. The style: flash. The mood: still.",
    ],
)
def test_owner_free_text_does_not_split(text: str | None) -> None:
    assert facets.split(text) is None


def test_kinds_and_layers() -> None:
    assert facets.FACETS == ("subject", "setting", "style", "mood")
    assert facets.LAYERS == ("facet:subject", "facet:setting", "facet:style", "facet:mood")
    assert facets.kind("mood", "split") == "facet:mood/split"
    assert facets.kinds("extract") == (
        "facet:subject/extract",
        "facet:setting/extract",
        "facet:style/extract",
        "facet:mood/extract",
    )
    assert len(facets.kinds()) == 8
    assert all(facets.is_kind(k) for k in facets.kinds())
    for junk in ("description", "facet:mood", "facet:mood/other", "facet:taste/split", ""):
        assert not facets.is_kind(junk)
    assert facets.layer("style") == "facet:style"
    assert facets.facet_of("facet:style") == "style"
    with pytest.raises(ValueError):
        facets.kind("taste", "split")
    with pytest.raises(ValueError):
        facets.kind("mood", "guess")
    with pytest.raises(ValueError):
        facets.facet_of("whole")
