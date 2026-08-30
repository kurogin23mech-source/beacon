"""Tests for bin/bcodex.

The wrapper should make a Codex TUI DM-ready without requiring the user to
manually keep an app-server terminal open. These tests use a fake ``codex``
binary and a fake bridge script so they never call the real Codex CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BCODEX = REPO_ROOT / "bin" / "bcodex"


def _make_fake_codex(bin_dir: Path, calls_path: Path) -> None:
    fake = bin_dir / "codex"
    fake.write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        set -euo pipefail
        python3 - "$@" <<'PY'
import json, os, sys
path = os.environ["FAKE_CODEX_CALLS"]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
PY
        if [ "${{1:-}}" = "app-server" ]; then
            while true; do sleep 1; done
        fi
        exit 0
    """).strip(), encoding="utf-8")
    fake.chmod(0o755)


def _run(tmp_path: Path, args: list[str] | None = None, extra_env: dict | None = None):
    project = tmp_path / "project"
    (project / ".beacon").mkdir(parents=True)
    (project / ".beacon" / "cloud.json").write_text("{}", encoding="utf-8")
    (project / "AGENTS.md").write_text("test", encoding="utf-8")

    install_root = tmp_path / "install"
    bridge_dir = install_root / "plugins" / "beacon" / "scripts"
    bridge_dir.mkdir(parents=True)
    bridge = bridge_dir / "beacon-codex-bridge"
    bridge.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    bridge.chmod(0o755)
    beacon_bin = install_root / "bin" / "beacon"
    beacon_bin.parent.mkdir(parents=True)
    # Record every `beacon ...` invocation (argv) so tests can assert on the
    # launcher's side-effects (e.g. `bus budget grant` under --armed).
    beacon_bin.write_text(textwrap.dedent("""
        #!/usr/bin/env bash
        if [ -n "${BEACON_FAKE_CALLS:-}" ]; then
            python3 - "$@" <<'PY'
import json, os, sys
with open(os.environ["BEACON_FAKE_CALLS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
PY
        fi
        exit 0
    """).strip(), encoding="utf-8")
    beacon_bin.chmod(0o755)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls_path = tmp_path / "codex_calls.json"
    calls_path.write_text("", encoding="utf-8")
    _make_fake_codex(bin_dir, calls_path)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_CODEX_CALLS"] = str(calls_path)
    env["BEACON_INSTALL_ROOT"] = str(install_root)
    env["BEACON_BIN"] = str(beacon_bin)
    env["BEACON_FAKE_CALLS"] = str(tmp_path / "beacon_calls.json")
    env["HOME"] = str(tmp_path / "home")
    # ms-160: the fake app-server never serves /readyz, so skip the readyz gate
    # here (tested separately) — otherwise every wrapper test would block on the
    # poll for the full timeout.
    env["BEACON_BCODEX_READYZ_TIMEOUT"] = "0"
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        ["bash", str(BCODEX)] + (args or []),
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text().splitlines()
        if line.strip()
    ]
    return proc, calls, project


def test_bcodex_starts_app_server_and_remote_codex(tmp_path):
    proc, calls, project = _run(tmp_path, args=["--port", "39991", "--model", "x"])
    assert proc.returncode == 0, proc.stderr
    assert ["app-server", "--listen", "ws://127.0.0.1:39991"] in calls
    remote_calls = [
        c for c in calls
        if c[:4] == ["--remote", "ws://127.0.0.1:39991", "-C", str(project)]
    ]
    assert len(remote_calls) == 1
    assert remote_calls[0][4:] == ["--model", "x"]
    state = json.loads((project / ".beacon" / "codex" / "bcodex.json").read_text())
    assert state["url"] == "ws://127.0.0.1:39991"
    assert state["armed"] is False


def test_bcodex_armed_records_armed_state(tmp_path):
    proc, _calls, project = _run(tmp_path, args=["--armed", "--port", "39992"])
    assert proc.returncode == 0, proc.stderr
    state = json.loads((project / ".beacon" / "codex" / "bcodex.json").read_text())
    assert state["armed"] is True


def test_bcodex_opt_out_launches_plain_codex(tmp_path):
    proc, calls, _project = _run(
        tmp_path,
        args=["--port", "39993", "--model", "x"],
        extra_env={"BEACON_NO_BUS": "1"},
    )
    assert proc.returncode == 0
    assert calls == [["--model", "x"]]
    assert "DM opt-out" in proc.stderr


def _make_ws_probe(tmp_path: Path, *, present: bool) -> Path:
    """A stub interpreter for the websockets pre-flight probe (e-2536 B-2).

    bcodex runs ``$BEACON_CODEX_WS_PROBE_PY -c "import websockets"``. Exit 0
    simulates websockets present, exit 1 simulates it absent — so both branches
    are testable regardless of whether the test env actually has websockets.
    """
    probe = tmp_path / ("ws_probe_present" if present else "ws_probe_missing")
    probe.write_text(
        f"#!/usr/bin/env bash\nexit {0 if present else 1}\n", encoding="utf-8"
    )
    probe.chmod(0o755)
    return probe


def test_bcodex_warns_when_websockets_missing(tmp_path):
    # e-2536 B-2: a fresh install whose daemon python lacks websockets must be
    # warned at launch that DM wake silently degrades to pull-only.
    probe = _make_ws_probe(tmp_path, present=False)
    proc, _calls, _project = _run(
        tmp_path,
        args=["--port", "39994"],
        extra_env={"BEACON_CODEX_WS_PROBE_PY": str(probe)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "websockets" in proc.stderr
    assert "pull-only" in proc.stderr
    assert "pip install" in proc.stderr


def test_bcodex_no_websockets_warning_when_present(tmp_path):
    # When the daemon python has websockets, no scary wake warning is emitted.
    probe = _make_ws_probe(tmp_path, present=True)
    proc, _calls, _project = _run(
        tmp_path,
        args=["--port", "39995"],
        extra_env={"BEACON_CODEX_WS_PROBE_PY": str(probe)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "websockets が見つかりません" not in proc.stderr


def _beacon_calls(tmp_path: Path) -> list[list[str]]:
    p = tmp_path / "beacon_calls.json"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_bcodex_armed_auto_grants_budget(tmp_path):
    # 穴① (ms-93 e-2992): --armed must be self-sufficient. The launcher grants
    # the reply budget so the user no longer runs `beacon bus budget grant` by
    # hand; the daemon deliberately never self-grants (T2 can't self-escalate).
    proc, _calls, _project = _run(tmp_path, args=["--armed", "--port", "39994"])
    assert proc.returncode == 0, proc.stderr
    assert ["bus", "budget", "grant", "--turns", "20"] in _beacon_calls(tmp_path)


def test_bcodex_armed_turns_overrides_default(tmp_path):
    proc, _calls, _project = _run(
        tmp_path, args=["--armed", "--armed-turns", "5", "--port", "39995"])
    assert proc.returncode == 0, proc.stderr
    assert ["bus", "budget", "grant", "--turns", "5"] in _beacon_calls(tmp_path)


def test_bcodex_non_armed_does_not_grant_budget(tmp_path):
    # Without --armed the session is DM-ready (receives) but must NOT grant an
    # autonomous-reply budget — mirrors bclaude (no armed by default).
    proc, _calls, _project = _run(tmp_path, args=["--port", "39996"])
    assert proc.returncode == 0, proc.stderr
    grants = [c for c in _beacon_calls(tmp_path)
              if c[:3] == ["bus", "budget", "grant"]]
    assert grants == []


def test_bcodex_armed_turns_rejects_non_integer(tmp_path):
    proc, _calls, _project = _run(
        tmp_path, args=["--armed", "--armed-turns", "abc"])
    assert proc.returncode == 2
    assert "positive integer" in proc.stderr


# ms-160: the readyz gate — bcodex must wait for /readyz before launching the
# TUI so `codex --remote` doesn't connect during the "listening but not ready"
# window (codex 0.14x) and die with "failed to connect to remote app server".

def _make_fake_codex_with_readyz(bin_dir, calls_path):
    """A fake codex whose `app-server --listen ws://HOST:PORT` also serves a
    200 /readyz on that PORT, so the bcodex readyz gate can go green."""
    fake = bin_dir / "codex"
    fake.write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        set -euo pipefail
        python3 - "$@" <<'PY'
import json, os, sys
with open(os.environ["FAKE_CODEX_CALLS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
PY
        if [ "${{1:-}}" = "app-server" ]; then
            # args: app-server --listen ws://127.0.0.1:PORT
            url="${{3:-}}"
            port="${{url##*:}}"
            python3 - "$port" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/readyz" else 404
        self.send_response(code); self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
        fi
        exit 0
    """).strip(), encoding="utf-8")
    fake.chmod(0o755)


def _run_readyz(tmp_path, args):
    project = tmp_path / "project"
    (project / ".beacon").mkdir(parents=True)
    (project / ".beacon" / "cloud.json").write_text("{}", encoding="utf-8")
    (project / "AGENTS.md").write_text("test", encoding="utf-8")
    install_root = tmp_path / "install"
    bridge_dir = install_root / "plugins" / "beacon" / "scripts"
    bridge_dir.mkdir(parents=True)
    bridge = bridge_dir / "beacon-codex-bridge"
    bridge.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    bridge.chmod(0o755)
    beacon_bin = install_root / "bin" / "beacon"
    beacon_bin.parent.mkdir(parents=True)
    beacon_bin.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    beacon_bin.chmod(0o755)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls_path = tmp_path / "codex_calls.json"
    calls_path.write_text("", encoding="utf-8")
    _make_fake_codex_with_readyz(bin_dir, calls_path)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_CODEX_CALLS"] = str(calls_path)
    env["BEACON_INSTALL_ROOT"] = str(install_root)
    env["BEACON_BIN"] = str(beacon_bin)
    env["HOME"] = str(tmp_path / "home")
    env["BEACON_BCODEX_READYZ_TIMEOUT"] = "10"  # gate ON
    proc = subprocess.run(["bash", str(BCODEX)] + args, cwd=str(project),
                          env=env, capture_output=True, text=True, timeout=20)
    calls = [json.loads(l) for l in calls_path.read_text().splitlines() if l.strip()]
    return proc, calls, project


def test_bcodex_waits_for_readyz_then_launches(tmp_path):
    """With the gate ON and readyz served, bcodex reaches the --remote launch
    (it neither hangs nor skips) and prints no readyz-timeout warning."""
    proc, calls, project = _run_readyz(tmp_path, ["--port", "39987"])
    assert proc.returncode == 0, proc.stderr
    assert ["app-server", "--listen", "ws://127.0.0.1:39987"] in calls
    assert any(c[:2] == ["--remote", "ws://127.0.0.1:39987"] for c in calls), calls
    assert "readyz not green" not in proc.stderr, proc.stderr


def test_bcodex_readyz_gate_fails_open_when_never_ready(tmp_path):
    """If /readyz never goes green, bcodex must still launch after the cap
    (fail-open) rather than hang. Uses the plain fake (no readyz) + short cap."""
    project = tmp_path / "project"
    (project / ".beacon").mkdir(parents=True)
    (project / ".beacon" / "cloud.json").write_text("{}", encoding="utf-8")
    (project / "AGENTS.md").write_text("test", encoding="utf-8")
    install_root = tmp_path / "install"
    bd = install_root / "plugins" / "beacon" / "scripts"
    bd.mkdir(parents=True)
    (bd / "beacon-codex-bridge").write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    (bd / "beacon-codex-bridge").chmod(0o755)
    bb = install_root / "bin" / "beacon"
    bb.parent.mkdir(parents=True)
    bb.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    bb.chmod(0o755)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls_path = tmp_path / "codex_calls.json"
    calls_path.write_text("", encoding="utf-8")
    _make_fake_codex(bin_dir, calls_path)  # never serves readyz
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_CODEX_CALLS"] = str(calls_path)
    env["BEACON_INSTALL_ROOT"] = str(install_root)
    env["BEACON_BIN"] = str(bb)
    env["HOME"] = str(tmp_path / "home")
    env["BEACON_BCODEX_READYZ_TIMEOUT"] = "1"  # 1s cap → fail-open fast
    proc = subprocess.run(["bash", str(BCODEX), "--port", "39986"], cwd=str(project),
                          env=env, capture_output=True, text=True, timeout=20)
    calls = [json.loads(l) for l in calls_path.read_text().splitlines() if l.strip()]
    assert proc.returncode == 0, proc.stderr
    assert any(c[:2] == ["--remote", "ws://127.0.0.1:39986"] for c in calls), calls
    assert "readyz not green" in proc.stderr, proc.stderr
