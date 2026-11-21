import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "ptp_push", Path(__file__).resolve().parent.parent / "scripts" / "ptp_push.py"
)
assert _spec and _spec.loader
ptp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ptp)

TOKEN = "s3cret-read-token"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    d = tmp_path / "share"
    d.mkdir()
    for name in ("ptp-50.png", "ptp-51.png", "ptp-51-r2.png", "ptp-52-b.png", "zm2-7.png"):
        (d / name).write_bytes(name.encode())
    (d / "PTP-53.PNG").write_bytes(b"upper")
    (d / "notes.txt").write_bytes(b"x")
    (d / "other.png").write_bytes(b"x")
    (d / "sub").mkdir()
    (d / "sub" / "ptp-99.png").write_bytes(b"x")
    return d


@pytest.fixture(autouse=True)
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PENISH_READ_TOKEN", raising=False)


def test_scan_is_flat_and_case_insensitive(folder: Path) -> None:
    names = [p.name for p in ptp.scan(folder)]
    assert "PTP-53.PNG" in names and "zm2-7.png" in names
    assert "other.png" not in names and "notes.txt" not in names and "ptp-99.png" not in names
    assert [p.name for p in ptp.scan(folder, ["other.*"])] == ["other.png"]


def test_parse_shas_json_and_text() -> None:
    a, b = sha(b"a"), sha(b"b")
    doc = json.dumps(
        {
            "images": [{"sha256": a, "status": "live", "slug": "x"}],
            "uploads": [{"sha256": b, "stem": "s", "stage": "failed", "error": "boom"}],
        }
    )
    images, uploads = ptp.parse_shas(doc)
    assert set(images) == {a} and set(uploads) == {b}
    images, uploads = ptp.parse_shas(json.dumps({"images": []}))
    assert images == {} and uploads == {}
    images, uploads = ptp.parse_shas(f"# held\n\n{a}\n  {b.upper()}  \n")
    assert set(images) == {a, b} and uploads == {}
    with pytest.raises(ptp.Usage):
        ptp.parse_shas("nonsense")


def test_classify_take_and_duplicates(folder: Path) -> None:
    (folder / "zm2-8.png").write_bytes(b"ptp-50.png")  # same bytes as ptp-50.png
    paths = ptp.scan(folder)
    shas = {p: ptp.sha256_file(p) for p in paths}
    live = shas[folder / "ptp-51.png"]
    failed = shas[folder / "ptp-52-b.png"]
    images = {live: {"sha256": live, "status": "live", "slug": "the-slug"}}
    uploads = {failed: {"sha256": failed, "stage": "failed", "error": "bad"}}
    rows = {r["name"]: r for r in ptp.classify(paths, shas, images, uploads)}
    assert rows["ptp-51.png"]["on_site"] and rows["ptp-51.png"]["slug"] == "the-slug"
    assert not rows["ptp-50.png"]["on_site"]
    assert not rows["ptp-52-b.png"]["on_site"] and rows["ptp-52-b.png"]["stage"] == "failed"
    assert "failed bad" in ptp.state_text(rows["ptp-52-b.png"])
    assert ptp.state_text(rows["ptp-51.png"]) == "on site: live the-slug"
    assert [n for n, r in rows.items() if r["take"]] == ["ptp-51-r2.png", "ptp-52-b.png"]
    assert rows["zm2-8.png"]["duplicate_of"] == "ptp-50.png"
    assert rows["ptp-50.png"]["duplicate_of"] is None


def test_sort_groups_takes_together(folder: Path) -> None:
    order = [p.name for p in sorted(ptp.scan(folder), key=ptp.sort_key)]
    assert order.index("ptp-51.png") + 1 == order.index("ptp-51-r2.png")


def test_dry_run_output(folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shas = tmp_path / "shas.txt"
    shas.write_text(sha(b"ptp-51.png") + "\n")
    assert ptp.main([str(folder), "--shas-file", str(shas)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(
        ln.startswith("ptp-51.png") and "on site: known" in ln and "take?" not in ln for ln in out
    )
    assert any(
        ln.startswith("ptp-51-r2.png") and "not on site" in ln and "take?" in ln for ln in out
    )
    assert out[-1] == "6 files, 1 on site, 5 not on site, 2 take?, 0 duplicate"
    assert ptp.main([str(folder), "--shas-file", str(shas), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["files"] == 6 and len(data["files"]) == 6


def test_files_from(folder: Path) -> None:
    got = ptp.parse_files_from(f"# finals\n\nptp-50.png\n{folder}/ptp-51.png\nptp-50.png\n", folder)
    assert [p.name for p in got] == ["ptp-50.png", "ptp-51.png"]
    with pytest.raises(ptp.Usage):
        ptp.parse_files_from("ptp-404.png\n", folder)
    with pytest.raises(ptp.Usage):
        ptp.parse_files_from("sub/ptp-99.png\n", folder)
    with pytest.raises(ptp.Usage):
        ptp.parse_files_from("/etc/hosts\n", folder)


class Runner:
    def __init__(self, codes: tuple[int, ...] = (0, 0)) -> None:
        self.calls: list[list[str]] = []
        self.sidecars: list[tuple[str, str]] = []
        self.codes = list(codes)

    def __call__(self, cmd: list[str], **_: Any) -> SimpleNamespace:
        self.calls.append(cmd)
        if len(self.calls) == 1:
            for f in cmd[2:-1]:
                self.sidecars.append((Path(f).name, Path(f).read_text()))
        return SimpleNamespace(returncode=self.codes.pop(0))


def send_args(folder: Path, shas: Path, listing: Path, *extra: str) -> list[str]:
    return [
        str(folder),
        "--shas-file",
        str(shas),
        "--send",
        "--files-from",
        str(listing),
        *extra,
    ]


def test_send_needs_files_from(
    folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Runner()
    monkeypatch.setattr(ptp.subprocess, "run", r)
    shas = tmp_path / "s.txt"
    shas.write_text("")
    assert ptp.main([str(folder), "--shas-file", str(shas), "--send"]) == 2
    assert r.calls == []


def test_missing_listed_file_fails_before_rsync(
    folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Runner()
    monkeypatch.setattr(ptp.subprocess, "run", r)
    shas = tmp_path / "s.txt"
    shas.write_text("")
    listing = tmp_path / "f.txt"
    listing.write_text("ptp-50.png\nptp-404.png\n")
    assert ptp.main(send_args(folder, shas, listing)) == 2
    assert r.calls == []


def test_send_order_sidecars_and_skips(
    folder: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    r = Runner()
    monkeypatch.setattr(ptp.subprocess, "run", r)
    shas = tmp_path / "s.txt"
    shas.write_text(sha(b"ptp-51.png") + "\n")
    listing = tmp_path / "f.txt"
    listing.write_text("ptp-50.png\nptp-51.png\nptp-52-b.png\n")
    assert ptp.main(send_args(folder, shas, listing)) == 0
    first, second = r.calls
    assert first[:2] == ["rsync", "-av"] and first[-1] == "upload@100.80.26.40:"
    assert r.sidecars == [
        ("ptp-50_collection.txt", "pansophicon\n"),
        ("ptp-52-b_collection.txt", "pansophicon\n"),
    ]
    assert all(str(folder) not in f for f in first)  # sidecars never land in Tom's folder
    assert not list(folder.glob("*_collection.txt"))
    assert second == [
        "rsync",
        "-av",
        str(folder.resolve() / "ptp-50.png"),
        str(folder.resolve() / "ptp-52-b.png"),
        "upload@100.80.26.40:",
    ]
    out = capsys.readouterr().out
    assert "skip ptp-51.png" in out and "stems: ptp-50 ptp-52-b" in out
    assert out.count("run: rsync") == 2


def test_limit_and_collection_and_dest(
    folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Runner()
    monkeypatch.setattr(ptp.subprocess, "run", r)
    shas = tmp_path / "s.txt"
    shas.write_text("")
    listing = tmp_path / "f.txt"
    listing.write_text("ptp-50.png\nptp-51.png\n")
    args = send_args(folder, shas, listing, "--limit", "1", "--collection", "zm", "--dest", "h:")
    assert ptp.main(args) == 0
    assert r.sidecars == [("ptp-50_collection.txt", "zm\n")]
    assert r.calls[1][-1] == "h:" and len(r.calls[1]) == 4


def test_rsync_failure_stops(folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Runner((23, 0))
    monkeypatch.setattr(ptp.subprocess, "run", r)
    shas = tmp_path / "s.txt"
    shas.write_text("")
    listing = tmp_path / "f.txt"
    listing.write_text("ptp-50.png\n")
    assert ptp.main(send_args(folder, shas, listing)) == 1
    assert len(r.calls) == 1


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *a: object) -> None:
        return None


def fake_urlopen(body: dict[str, Any], seen: list[Any]) -> Any:
    def opener(req: Any, timeout: float = 0) -> FakeResponse:
        seen.append(req)
        return FakeResponse(json.dumps(body).encode())

    return opener


def test_token_from_env_never_printed(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    body = {"images": [{"sha256": sha(b"ptp-50.png"), "status": "live", "slug": "fifty"}]}
    monkeypatch.setattr(ptp.urllib.request, "urlopen", fake_urlopen(body, seen))
    monkeypatch.setenv("PENISH_READ_TOKEN", f"  {TOKEN}\n")
    assert ptp.main([str(folder)]) == 0
    cap = capsys.readouterr()
    assert TOKEN not in cap.out and TOKEN not in cap.err
    assert "on site: live fifty" in cap.out
    assert seen[0].full_url == "https://peni.sh/api/admin/shas"
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_token_from_file(
    folder: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    f = tmp_path / "home" / ".config" / "penish" / "read-token"
    f.parent.mkdir(parents=True)
    f.write_text(TOKEN + "\n")
    seen: list[Any] = []
    monkeypatch.setattr(ptp.urllib.request, "urlopen", fake_urlopen({"images": []}, seen))
    assert ptp.main([str(folder), "--base", "http://x"]) == 0
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in capsys.readouterr().out


def test_no_token_is_usage_error(folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ptp.main([str(folder)]) == 2
    err = capsys.readouterr().err
    assert "PENISH_READ_TOKEN" in err and "read-token" in err and len(err.splitlines()) == 1


def test_fetch_failure_exits_1(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(req: Any, timeout: float = 0) -> None:
        raise OSError("down")

    monkeypatch.setattr(ptp.urllib.request, "urlopen", boom)
    monkeypatch.setenv("PENISH_READ_TOKEN", TOKEN)
    assert ptp.main([str(folder)]) == 1
    assert TOKEN not in capsys.readouterr().err
