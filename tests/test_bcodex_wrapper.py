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
    beacon_bin.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
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
    env["HOME"] = str(tmp_path / "home")
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
