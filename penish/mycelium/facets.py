"""Facets: four short bodies per picture, one per labelled part of the analysis.

Pure: no database, no model. The names here are shared by the migration's checks,
the worker jobs, the store and the graph, so they live in one place.

Each facet grows its own layer (`facet:subject` and so on). A recipe is how the
bodies were made: `split` cuts the private analysis at its labels, `extract` is a
tailored vision call. Each (facet, recipe) is its own embedding kind, so
corpus_stats keeps a mean per kind and every recipe is centered on its own.
"""

from __future__ import annotations

import re

from penish.worker.describe import ANALYSIS_LABELS

FACETS: tuple[str, ...] = tuple(label.lower() for label in ANALYSIS_LABELS)
RECIPES: tuple[str, ...] = ("split", "extract")
SPLIT, EXTRACT = RECIPES

LAYERS: tuple[str, ...] = tuple(f"facet:{f}" for f in FACETS)

_KIND = re.compile(rf"^facet:({'|'.join(FACETS)})/({'|'.join(RECIPES)})$")
# The same rule describe.check_analysis applies, whitespace class and all: an analysis
# that passed validation but failed to split would buy a capped extract call for words
# that were labelled correctly. Anything before the first label belongs to no facet.
_LABEL = re.compile(rf"^\s*({'|'.join(ANALYSIS_LABELS)})\s*:", re.M)


def layer(facet: str) -> str:
    if facet not in FACETS:
        raise ValueError(f"unknown facet {facet}")
    return f"facet:{facet}"


def facet_of(layer_name: str) -> str:
    """`facet:mood` to `mood`."""
    name = layer_name.removeprefix("facet:")
    if layer_name == name or name not in FACETS:
        raise ValueError(f"not a facet layer {layer_name}")
    return name


def kind(facet: str, recipe: str) -> str:
    """The embedding kind of one facet under one recipe: `facet:mood/split`."""
    if facet not in FACETS:
        raise ValueError(f"unknown facet {facet}")
    if recipe not in RECIPES:
        raise ValueError(f"unknown recipe {recipe}")
    return f"facet:{facet}/{recipe}"


def kinds(recipe: str | None = None) -> tuple[str, ...]:
    """Every facet kind, or one recipe's four, in facet order."""
    recipes = RECIPES if recipe is None else (recipe,)
    return tuple(kind(f, r) for r in recipes for f in FACETS)


def is_kind(name: str) -> bool:
    return _KIND.match(name) is not None


def split(analysis: str | None) -> dict[str, str] | None:
    """The four bodies of a labelled analysis, keyed by facet in facet order.

    None when the labels are missing, repeated or out of order, or a part is empty:
    an owner may have written free text, and a guess at where one facet ends would
    put the wrong words in the wrong layer. The caller falls back to the extract call.
    """
    if not analysis:
        return None
    found = list(_LABEL.finditer(analysis))
    if tuple(m.group(1) for m in found) != ANALYSIS_LABELS:
        return None
    out: dict[str, str] = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(analysis)
        body = analysis[m.end() : end].strip()
        if not body:
            return None
        out[FACETS[i]] = body
    return out
