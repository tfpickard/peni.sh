"""Send chosen Pansophicon pictures to the inbox, skipping what the site already holds.

Usage:
    uv run python scripts/ptp_push.py [folder] [--shas-file PATH] [--json]
    uv run python scripts/ptp_push.py [folder] --send --files-from finals.txt [--limit N]

Dry run by default: hashes every ptp-*.png and zm*.png in the folder and says which the site
holds. The script never chooses finals. Names that look like alternate takes are marked, and
sending needs a list (--files-from) that Tom wrote.

The site's sha256s come from GET /api/admin/shas with a read token (PENISH_READ_TOKEN, else
~/.config/penish/read-token), or from --shas-file before that endpoint is live. The admin
password is never used here.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_FOLDER = "~/src/claude-share"
DEFAULT_GLOBS = ("ptp-*.png", "zm*.png")
DEFAULT_DEST = "upload@100.80.26.40:"
UA = {"User-Agent": "penish-ptp-push/1 (read-only)"}
TOKEN_ENV = "PENISH_READ_TOKEN"
TOKEN_HINT = f"no read token: set {TOKEN_ENV} or put it in ~/.config/penish/read-token"

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# prefix-number, then whatever follows the number: -r2, -b, -jazari
NAME_RE = re.compile(r"^(?P<prefix>[a-z]+\d*)-(?P<num>\d+)(?P<rest>.*)$")


class Usage(Exception):
    """A mistake in how the script was called; exit 2."""


class Fetch(Exception):
    """The site's sha list could not be read; exit 1."""


def scan(folder: Path, globs: tuple[str, ...] | list[str] = DEFAULT_GLOBS) -> list[Path]:
    # Matched by hand: Path.glob is case-sensitive on Linux and Tom's files come off a Mac.
    pats = [g.lower() for g in globs]
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and any(fnmatch.fnmatchcase(p.name.lower(), g) for g in pats)
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_shas(text: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (images, uploads), keyed by sha256, from the api's JSON or one sha per line."""
    stripped = text.strip()
    images: dict[str, dict[str, Any]] = {}
    uploads: dict[str, dict[str, Any]] = {}
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise Usage(f"sha list is not valid json: {e}") from e
        for row in data.get("images") or []:
            images[str(row["sha256"]).lower()] = row
        for row in data.get("uploads") or []:
            uploads[str(row["sha256"]).lower()] = row
        return images, uploads
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        if not SHA_RE.match(line):
            raise Usage(f"sha list line {n} is not a sha256")
        images[line] = {"sha256": line, "status": "known", "slug": ""}
    return images, uploads


def read_token(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    tok = env.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    # Path.home() follows HOME, which is what lets tests point it elsewhere.
    f = Path.home() / ".config" / "penish" / "read-token"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


def fetch_shas(base: str, token: str) -> str:
    req = urllib.request.Request(
        base.rstrip("/") + "/api/admin/shas",
        headers={**UA, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body: bytes = r.read()
    except urllib.error.HTTPError as e:
        raise Fetch(f"fetch failed: http {e.code}") from e
    except OSError as e:
        raise Fetch(f"fetch failed: {e.__class__.__name__}") from e
    return body.decode()


def split_name(name: str) -> tuple[str, int, str]:
    """(prefix, number, rest) from a file name; rest is non-empty for alternate takes."""
    stem = name.rsplit(".", 1)[0].lower()
    m = NAME_RE.match(stem)
    if not m:
        return stem, 0, ""
    return m["prefix"], int(m["num"]), m["rest"]


def is_take(name: str) -> bool:
    return bool(split_name(name)[2])


def sort_key(path: Path) -> tuple[str, int, str, str]:
    prefix, num, rest = split_name(path.name)
    # The plain name leads its group: ptp-51 before ptp-51-b.
    return prefix, num, rest, path.name.lower()


def classify(
    paths: list[Path],
    shas: dict[Path, str],
    images: dict[str, dict[str, Any]],
    uploads: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    first: dict[str, str] = {}
    for p in sorted(paths, key=sort_key):
        sha = shas[p]
        row: dict[str, Any] = {
            "name": p.name,
            "sha256": sha,
            "on_site": sha in images,
            "status": None,
            "slug": None,
            "stage": None,
            "error": None,
            "take": is_take(p.name),
            "duplicate_of": first.get(sha),
        }
        first.setdefault(sha, p.name)
        if sha in images:
            row["status"] = images[sha].get("status")
            row["slug"] = images[sha].get("slug")
        elif sha in uploads:
            row["stage"] = uploads[sha].get("stage")
            row["error"] = uploads[sha].get("error")
        rows.append(row)
    return rows


def state_text(row: dict[str, Any]) -> str:
    if row["on_site"]:
        return " ".join(x for x in ("on site:", row["status"], row["slug"]) if x)
    if row["stage"]:
        return " ".join(x for x in (row["stage"], row["error"]) if x)
    return "not on site"


def line(row: dict[str, Any]) -> str:
    parts = [row["name"], row["sha256"][:12], state_text(row)]
    if row["take"]:
        parts.append("take?")
    if row["duplicate_of"]:
        parts.append(f"duplicate of {row['duplicate_of']}")
    return "  ".join(parts)


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    on = sum(r["on_site"] for r in rows)
    return {
        "files": len(rows),
        "on_site": on,
        "not_on_site": len(rows) - on,
        "takes": sum(r["take"] for r in rows),
        "duplicates": sum(bool(r["duplicate_of"]) for r in rows),
    }


def summary_text(s: dict[str, int]) -> str:
    return (
        f"{s['files']} files, {s['on_site']} on site, {s['not_on_site']} not on site, "
        f"{s['takes']} take?, {s['duplicates']} duplicate"
    )


def parse_files_from(text: str, folder: Path) -> list[Path]:
    """Resolve the owner's list of finals; every entry must be a file directly in the folder."""
    root = folder.resolve()
    out: list[Path] = []
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        p = Path(entry).expanduser()
        if not p.is_absolute():
            p = root / p
        if p.resolve().parent != root:
            raise Usage(f"{entry}: not in {folder}")
        if not p.is_file():
            raise Usage(f"{entry}: no such file")
        if p not in out:
            out.append(p)
    return out


def sidecar_name(path: Path) -> str:
    return f"{path.stem}_collection.txt"


def rsync_args(files: list[Path], dest: str) -> list[str]:
    return ["rsync", "-av", *map(str, files), dest]


def build_runs(images: list[Path], sidecars: list[Path], dest: str) -> list[list[str]]:
    # Sidecars first: the server settles a stem after 10 s of quiet and a picture picked up
    # without its sidecar would publish with no collection request.
    return [rsync_args(sidecars, dest), rsync_args(images, dest)]


def send(rows: list[dict[str, Any]], wanted: list[Path], collection: str, dest: str) -> int:
    by_name = {r["name"]: r for r in rows}
    todo: list[Path] = []
    for p in wanted:
        r = by_name[p.name]
        if r["on_site"]:
            print(f"skip {p.name}  {state_text(r)}")
        elif r["duplicate_of"] and any(t.name == r["duplicate_of"] for t in todo):
            print(f"skip {p.name}  duplicate of {r['duplicate_of']}")
        else:
            todo.append(p)
    if not todo:
        print("0 to send")
        return 0
    with tempfile.TemporaryDirectory(prefix="ptp-sidecars-") as tmp:
        sidecars = []
        for p in todo:
            s = Path(tmp) / sidecar_name(p)
            s.write_text(collection + "\n")
            sidecars.append(s)
        runs = build_runs(todo, sidecars, dest)
        for cmd in runs:
            print("run: " + " ".join(cmd))
        names = ", ".join(p.name for p in todo)
        for n, cmd in enumerate(runs):
            code = subprocess.run(cmd).returncode
            if code:
                if n == 0:
                    print(f"rsync exit {code}: sidecars failed, no pictures sent")
                else:
                    print(f"rsync exit {code}: sidecars sent, pictures may be partial: {names}")
                return 1
    print(f"sent {len(todo)}")
    print("stems: " + " ".join(p.stem for p in todo))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Send chosen Pansophicon pictures to the inbox.")
    ap.add_argument("folder", nargs="?", default=DEFAULT_FOLDER)
    ap.add_argument("--glob", action="append", help="file pattern, repeatable")
    ap.add_argument("--base", default="https://peni.sh")
    ap.add_argument("--shas-file", help="json or one sha256 per line, instead of the api")
    ap.add_argument("--json", action="store_true", help="print json instead of text")
    ap.add_argument("--send", action="store_true", help="rsync the files named by --files-from")
    ap.add_argument("--files-from", help="the finals, one file name per line")
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--collection", default="pansophicon")
    ap.add_argument("--limit", type=int, help="send at most N files")
    return ap


def run(args: argparse.Namespace) -> int:
    if args.send and not args.files_from:
        raise Usage("--send needs --files-from: the script does not choose finals")
    if args.limit is not None and args.limit < 1:
        raise Usage("--limit must be at least 1")
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        raise Usage(f"{folder}: not a folder")
    wanted: list[Path] = []
    if args.files_from:
        try:
            text = Path(args.files_from).expanduser().read_text()
        except OSError as e:
            raise Usage(f"{args.files_from}: {e.strerror}") from e
        wanted = parse_files_from(text, folder)

    if args.shas_file:
        try:
            raw = Path(args.shas_file).expanduser().read_text()
        except OSError as e:
            raise Usage(f"{args.shas_file}: {e.strerror}") from e
    else:
        token = read_token()
        if not token:
            raise Usage(TOKEN_HINT)
        raw = fetch_shas(args.base, token)
    images, uploads = parse_shas(raw)

    paths = scan(folder, args.glob or DEFAULT_GLOBS)
    # Listed finals may sit outside the default patterns; they still need a sha.
    for p in wanted:
        if p not in paths:
            paths.append(p)
    shas = {p: sha256_file(p) for p in paths}
    rows = classify(paths, shas, images, uploads)

    if args.send:
        if args.limit is not None:
            # Applied after the on-site skip so the limit counts pictures actually sent.
            wanted = _limit(wanted, rows, args.limit)
        return send(rows, wanted, args.collection, args.dest)

    if args.json:
        print(json.dumps({"files": rows, "summary": summary(rows)}, indent=2))
    else:
        for r in rows:
            print(line(r))
        print(summary_text(summary(rows)))
    return 0


def _limit(wanted: list[Path], rows: list[dict[str, Any]], n: int) -> list[Path]:
    by_name = {r["name"]: r for r in rows}
    kept: list[Path] = []
    sent = 0
    for p in wanted:
        if by_name[p.name]["on_site"]:
            kept.append(p)  # still reported as skipped
        elif sent < n:
            kept.append(p)
            sent += 1
    return kept


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Usage as e:
        print(str(e), file=sys.stderr)
        return 2
    except Fetch as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
