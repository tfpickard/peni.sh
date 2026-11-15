"""scripts/penish-deploy run for real with bash, curl and jq against a fake GitHub.

docker, flock and sleep are PATH stubs, and curl is a PATH wrapper that logs its argv before
running the real curl; everything GitHub-shaped (the runs query, the raw
compose.yaml fetch and the Deployments API) is a local http.server so the script's own curl
and jq code is what gets exercised.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "penish-deploy"
BASH = shutil.which("bash")
# Resolved before PATH gets the stubs dir, so the curl stub can hand off to the real one.
REAL_CURL = shutil.which("curl")
OLD = "a" * 40
NEW = "b" * 40
PIN = "c" * 40
READ_TOKEN = "ghp_readtokenshouldneverprint"
DEPLOY_TOKEN = "github_pat_deploytokenshouldneverprint"
REPO = "/repos/tfpickard/peni.sh/deployments"
COMPOSE = "services:\n  api:\n    image: ghcr.io/tfpickard/penish-api:${IMAGE_TAG}\n"


def _bash_ok() -> bool:
    if BASH is None:
        return False
    # The script leans on bash 4 behaviour (empty arrays under set -u); macOS /bin/bash is 3.2.
    out = subprocess.run(
        [BASH, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4


pytestmark = pytest.mark.skipif(
    not _bash_ok() or shutil.which("jq") is None or REAL_CURL is None,
    reason="needs bash 4+, jq and curl",
)


@dataclass
class FakeGitHub:
    head_sha: str = NEW
    create_status: int = 201
    status_status: int = 201
    create_delay: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""

    def deployment_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].startswith(REPO)]


def _handler(gh: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: object) -> None:
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            gh.requests.append(
                {"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")}
            )
            if self.path == "/runs":
                self._send(200, {"workflow_runs": [{"head_sha": gh.head_sha}]})
            elif self.path.startswith("/raw/") and self.path.endswith("/compose.yaml"):
                self._send(200, COMPOSE)
            else:
                self._send(404, {})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"null")
            gh.requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            if self.path == REPO:
                if gh.create_delay:
                    time.sleep(gh.create_delay)
                # A secret-looking body checks that response bodies never reach the log.
                self._send(gh.create_status, {"id": 7, "message": "secret-body"})
            elif self.path == f"{REPO}/7/statuses":
                self._send(gh.status_status, {"id": 1})
            else:
                self._send(404, {})

    return Handler


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    gh = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(gh))
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gh.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield gh
    finally:
        server.shutdown()
        server.server_close()


DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$STUB_LOG"
[ "$1" = compose ] || exit 0
shift
case " $* " in
  *" config "*) exit "${STUB_CONFIG_RC:-0}" ;;
  *" pull"*) exit "${STUB_PULL_RC:-0}" ;;
  *" up "*)
    n=$(( $(cat "$STUB_UPS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$STUB_UPS"
    if [ -n "${STUB_UP_FAIL_FROM:-}" ] && [ "$n" -ge "$STUB_UP_FAIL_FROM" ]; then exit 1; fi
    exit 0 ;;
  *" ps "*) printf '{"Service":"api","Health":"%s"}\\n' "${STUB_HEALTH:-healthy}" ;;
esac
exit 0
"""

# argv is what ps shows on the VPS, so a token that lands here is a token anyone there can read.
CURL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$CURL_ARGV_LOG"
exec "$REAL_CURL" "$@"
"""


@dataclass
class Box:
    root: Path
    state: Path
    stubs: Path
    docker_log: Path
    curl_log: Path

    def env_file(self, deploy_token: str | None = DEPLOY_TOKEN) -> None:
        lines = ["IMAGE_TAG=" + OLD, "GH_READ_TOKEN=" + READ_TOKEN]
        if deploy_token is not None:
            lines.append(f'GH_DEPLOY_TOKEN="{deploy_token}"')
        (self.root / ".env").write_text("\n".join(lines) + "\n")

    def docker_calls(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text().splitlines()

    def curl_argv(self) -> str:
        return self.curl_log.read_text() if self.curl_log.exists() else ""


@pytest.fixture
def box(tmp_path: Path) -> Box:
    root = tmp_path / "srv"
    (root / "data").mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}\n")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    (state / "deployed_sha").write_text(OLD + "\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, text in {
        "docker": DOCKER_STUB,
        "curl": CURL_STUB,
        # flock is Linux-only and the real lock is beside the point here.
        "flock": "#!/bin/sh\nexit 0\n",
        # The health wait sleeps between polls; instant keeps the timeout cases fast.
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        p = stubs / name
        p.write_text(text)
        p.chmod(0o755)
    b = Box(
        root=root,
        state=state,
        stubs=stubs,
        docker_log=tmp_path / "docker.log",
        curl_log=tmp_path / "curl.log",
    )
    b.env_file()
    return b


def run_deploy(box: Box, gh: FakeGitHub, **extra: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None and REAL_CURL is not None
    env = {
        "PATH": f"{box.stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(box.root),
        "XDG_RUNTIME_DIR": str(box.root),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "APP_ROOT": str(box.root),
        "STATE_DIRECTORY": str(box.state),
        "API_URL": f"{gh.url}/runs",
        "RAW_URL_BASE": f"{gh.url}/raw",
        "DEPLOYMENTS_URL": f"{gh.url}{REPO}",
        "GH_TIMEOUT_SECS": "1",
        "WAIT_SECS": "4",
        "STUB_LOG": str(box.docker_log),
        "STUB_UPS": str(box.root / "ups"),
        "CURL_ARGV_LOG": str(box.curl_log),
        "REAL_CURL": REAL_CURL,
        **extra,
    }
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _no_secrets(proc: subprocess.CompletedProcess[str]) -> None:
    out = proc.stdout + proc.stderr
    assert DEPLOY_TOKEN not in out
    assert READ_TOKEN not in out
    assert "secret-body" not in out


def _states(gh: FakeGitHub) -> list[str]:
    return [r["body"]["state"] for r in gh.deployment_calls() if r["path"].endswith("/statuses")]


def test_success_reports_create_progress_success(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    calls = github.deployment_calls()
    assert [c["path"] for c in calls] == [REPO, f"{REPO}/7/statuses", f"{REPO}/7/statuses"]
    assert all(c["auth"] == f"Bearer {DEPLOY_TOKEN}" for c in calls)
    # Same token, but it must have travelled on stdin: the argv curl saw never carries it.
    argv = box.curl_argv()
    assert REPO in argv
    assert DEPLOY_TOKEN not in argv
    # The runs query authenticates with the read token, which also stays off argv.
    runs = [r for r in github.requests if r["path"] == "/runs"]
    assert runs and runs[0]["auth"] == f"Bearer {READ_TOKEN}"
    assert READ_TOKEN not in argv
    create = calls[0]["body"]
    assert create == {
        "ref": NEW,
        "environment": "production",
        "auto_merge": False,
        "required_contexts": [],
        "transient_environment": False,
        "production_environment": True,
        "description": "from main",
    }
    assert calls[1]["body"] == {
        "state": "in_progress",
        "description": "pulling",
        "environment": "production",
    }
    done = calls[2]["body"]
    assert done["state"] == "success"
    assert done["environment_url"] == "https://peni.sh"
    assert done["auto_inactive"] is True
    assert "log_url" not in done
    assert done["description"].startswith("api healthy in ")
    assert done["description"].endswith("s")
    assert (box.state / "deployed_sha").read_text().strip() == NEW
    assert "WARNING" not in proc.stdout
    _no_secrets(proc)


def test_pinned_sha_says_pinned(box: Box, github: FakeGitHub) -> None:
    (box.state / "pinned_sha").write_text(PIN + "\n")
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    assert not any(r["path"] == "/runs" for r in github.requests)
    create = github.deployment_calls()[0]["body"]
    assert create["ref"] == PIN
    assert create["description"] == "pinned"
    assert _states(github) == ["in_progress", "success"]


def test_pull_failure_reports_failure(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github, STUB_PULL_RC="1")
    assert proc.returncode == 1
    assert _states(github) == ["in_progress", "failure"]
    last = github.deployment_calls()[-1]["body"]
    assert last["description"] == "pull failed; running containers untouched"
    assert "environment_url" not in last
    assert (box.state / "deployed_sha").read_text().strip() == OLD
    _no_secrets(proc)


def test_compose_validation_failure_reports_error(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github, STUB_CONFIG_RC="1")
    assert proc.returncode == 1
    assert _states(github) == ["in_progress", "error"]
    last = github.deployment_calls()[-1]["body"]
    assert last["description"] == "compose.yaml failed validation; nothing changed"
    assert not any(" pull" in c for c in box.docker_calls())


def test_health_timeout_names_rollback_sha(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github, STUB_HEALTH="starting")
    assert proc.returncode == 1
    assert _states(github) == ["in_progress", "failure"]
    last = github.deployment_calls()[-1]["body"]
    assert last["description"] == f"api not healthy within 4s; rolled back to {OLD[:7]}"
    assert (box.state / "failed_sha").read_text().strip() == NEW
    assert f"IMAGE_TAG={OLD}" in (box.root / ".env").read_text()


def test_health_timeout_without_previous_deploy(box: Box, github: FakeGitHub) -> None:
    (box.state / "deployed_sha").unlink()
    proc = run_deploy(box, github, STUB_HEALTH="starting")
    assert proc.returncode == 1
    last = github.deployment_calls()[-1]["body"]
    assert last["state"] == "failure"
    assert last["description"] == "api not healthy within 4s; nothing to roll back to"


def test_rollback_up_failure_says_so(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github, STUB_HEALTH="starting", STUB_UP_FAIL_FROM="2")
    assert proc.returncode == 1
    last = github.deployment_calls()[-1]["body"]
    assert last["state"] == "failure"
    assert last["description"] == f"api not healthy within 4s; rollback to {OLD[:7]} failed"


def test_no_deploy_token_means_no_calls(box: Box, github: FakeGitHub) -> None:
    box.env_file(deploy_token=None)
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    assert github.deployment_calls() == []
    assert (box.state / "deployed_sha").read_text().strip() == NEW
    assert "WARNING" not in proc.stdout
    assert any(c.startswith("compose up") for c in box.docker_calls())


def test_noop_poll_makes_no_calls(box: Box, github: FakeGitHub) -> None:
    (box.state / "deployed_sha").write_text(NEW + "\n")
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    assert github.deployment_calls() == []
    assert box.docker_calls() == []
    assert proc.stdout == ""


def test_paused_retry_makes_no_calls(box: Box, github: FakeGitHub) -> None:
    # A fresh failed_sha means the pause is still running; one deployment per minute would spam.
    (box.state / "failed_sha").write_text(NEW + "\n")
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    assert github.deployment_calls() == []
    assert box.docker_calls() == []


@pytest.mark.parametrize(
    ("setup", "warning"),
    [
        ({"create_status": 500}, "github deployment create failed (HTTP 500)"),
        ({"status_status": 500}, "github deployment status in_progress failed (HTTP 500)"),
        ({"create_delay": 3.0}, "github deployment create failed (no response within 1s)"),
    ],
    ids=["create-500", "status-500", "create-timeout"],
)
def test_github_trouble_does_not_change_the_deploy(
    box: Box, github: FakeGitHub, setup: dict[str, Any], warning: str
) -> None:
    for k, v in setup.items():
        setattr(github, k, v)
    proc = run_deploy(box, github)
    assert proc.returncode == 0, proc.stderr
    assert (box.state / "deployed_sha").read_text().strip() == NEW
    assert f"penish-deploy: WARNING: {warning}" in proc.stdout.splitlines()
    _no_secrets(proc)


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_refused_connection_is_not_called_a_timeout(box: Box, github: FakeGitHub) -> None:
    url = f"http://127.0.0.1:{_closed_port()}{REPO}"
    proc = run_deploy(box, github, DEPLOYMENTS_URL=url)
    assert proc.returncode == 0, proc.stderr
    assert (box.state / "deployed_sha").read_text().strip() == NEW
    warning = "penish-deploy: WARNING: github deployment create failed (no connection, curl exit 7)"
    assert warning in proc.stdout.splitlines()
    assert "no response within" not in proc.stdout
    assert url not in proc.stdout + proc.stderr
    _no_secrets(proc)


def test_github_trouble_does_not_change_a_failure(box: Box, github: FakeGitHub) -> None:
    github.create_status = 500
    proc = run_deploy(box, github, STUB_PULL_RC="1")
    assert proc.returncode == 1
    assert "penish-deploy: WARNING: github deployment create failed (HTTP 500)" in proc.stdout
    assert "pull failed" in proc.stderr
    _no_secrets(proc)


def test_dry_run_prints_calls(box: Box, github: FakeGitHub) -> None:
    proc = run_deploy(box, github, DRY_RUN="1")
    assert proc.returncode == 0, proc.stderr
    assert github.deployment_calls() == []
    lines = proc.stdout.splitlines()
    assert f"+ github deployment create ref={NEW} environment=production description=from main" in (
        lines
    )
    assert "+ github deployment status in_progress: pulling" in lines
    assert any(
        line.startswith("+ github deployment status success: api healthy in ") for line in lines
    )
    _no_secrets(proc)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_unexpected_abort_closes_the_deployment(box: Box, github: FakeGitHub) -> None:
    # set_image_tag cannot rewrite a read-only .env, so set -e ends the script mid-deploy.
    (box.root / ".env").chmod(0o400)
    proc = run_deploy(box, github)
    assert proc.returncode != 0
    assert _states(github) == ["in_progress", "error"]
    last = github.deployment_calls()[-1]["body"]
    assert last["description"] == "deploy script aborted; see journal"
