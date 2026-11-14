import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "web" / "src" / "lib"

# Node strips the types itself; only the extensionless import needs rewriting, so the
# test runs the shipped source rather than a copy of its logic.
SCRIPT = """
import { pattern, N } from './hyphae.ts';
const lens = [];
for (let i = 0; i < 4000; i++) lens.push(pattern(`slug-${i.toString(36)}-${i % 97}`).length);
for (const s of ['green-field-1739', 'night-street-2888', 'empty-room-905', 'slug-e2p-x'])
  lens.push(pattern(s).length);
const a = JSON.stringify(pattern('green-field-1739'));
const b = JSON.stringify(pattern('green-field-1739'));
console.log(JSON.stringify({ N, min: Math.min(...lens), max: Math.max(...lens), same: a === b }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_pattern_reaches_n(tmp_path: Path) -> None:
    src = (LIB / "hyphae.ts").read_text().replace("'./haiku/rng'", "'./rng.ts'")
    (tmp_path / "hyphae.ts").write_text(src)
    shutil.copy(LIB / "haiku" / "rng.ts", tmp_path / "rng.ts")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    (tmp_path / "run.mjs").write_text(SCRIPT)
    out = subprocess.run(
        ["node", "--no-warnings", "run.mjs"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    got = json.loads(out.stdout)
    assert got["min"] == got["max"] == got["N"]
    assert got["same"]
