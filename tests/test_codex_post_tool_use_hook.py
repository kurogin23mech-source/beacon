"""Codex wrapper contract for the Beacon PostToolUse hook."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "codex-post-tool-use-hook.py"


def _run(project: Path, command: str) -> subprocess.CompletedProcess:
    (project / ".beacon").mkdir(exist_ok=True)
    (project / ".beacon" / "project.json").write_text("{}", encoding="utf-8")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": command, "cwd": str(project)},
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=10,
    )


def test_codex_wrapper_classifies_commit_push_and_deploy(tmp_path):
    cases = [
        ("git commit -m 'x'", "/beacon-log"),
        ("git push origin main", "/beacon-push"),
        ("gcloud run deploy api", "/beacon-deploy"),
    ]
    for command, expected in cases:
        result = _run(tmp_path, command)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        assert output["hookEventName"] == "PostToolUse"
        assert expected in output["additionalContext"]


def test_codex_wrapper_ignores_unrelated_command(tmp_path):
    result = _run(tmp_path, "git status --short")
    assert result.returncode == 0
    assert result.stdout == ""


def test_wheel_layout_imports_beacon_cli_when_run_as_direct_script(tmp_path):
    """System Python starts with _bundled_scripts, not its owning venv, on sys.path."""
    site = tmp_path / "site-packages"
    package = site / "beacon_cli"
    shutil.copytree(ROOT / "beacon_cli", package)
    bundled = package / "_bundled_scripts"
    bundled.mkdir()
    wheel_script = bundled / SCRIPT.name
    shutil.copy2(SCRIPT, wheel_script)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}", encoding="utf-8")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": "git commit -m x", "cwd": str(project)},
    }
    result = subprocess.run(
        [sys.executable, str(wheel_script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": ""},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "/beacon-log" in result.stdout
