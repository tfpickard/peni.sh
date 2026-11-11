import html
import json
import re
from pathlib import Path

from penish.api.haiku_render import render_haiku_html

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "haiku_seed_vectors.json").read_text())
_SPAN = re.compile(
    r'<span aria-hidden="true" style="font-variation-settings: '
    r"'grow' ([\d.]+), 'THCK' ([\d.]+)\">(.*?)</span>"
)


def test_matches_typescript_vectors() -> None:
    assert VECTORS
    for case in VECTORS:
        out = render_haiku_html(case["lines"], seed=case["seed"], gen=case["gen"])
        got = [(html.unescape(c), g, t) for g, t, c in _SPAN.findall(out)]
        want = [tuple(c) for line in case["chars"] for c in line]
        assert got == want, case


def test_markup_shape() -> None:
    out = render_haiku_html(["a <b>", "c"], seed=1, gen="noise")
    assert out.startswith('<h1 class="haiku" aria-label="a &lt;b&gt; / c">')
    assert out.count('<span class="haiku-line">') == 2
    assert "&lt;" in out and "<b>" not in out
    # The space between words is plain text, not a span.
    assert "</span> <span" in out
