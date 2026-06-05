"""Semantic-equivalence tests for the cross-platform PostToolUse hook
(``beacon_cli.hooks.post_commit``).

Each test mirrors the existing ``tests/test_post_commit_hook.py`` (which
shells out to the bash version) so we can prove the Python rewrite reads
identically. The bash tests document the *contract*; this file enforces
that the contract holds without bash on the runner.

We invoke the entry-point via ``python -m beacon_cli.hooks.post_commit``
so the test simulates the exact way Claude Code on Windows will spawn
it (via the pipx-installed console-script wrapping the same module).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def beacon_project_dir(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "PostCommitHookPyTest",
        "summary": "",
        "milestones": [],
    }))
    return tmp_path


def _env_with_path() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
        if env.get("PYTHONPATH") else str(_REPO_ROOT)
    )
    return env


def _run(cwd: Path, command: str) -> subprocess.CompletedProcess:
    payload = {"tool_input": {"command": command, "cwd": str(cwd)}}
    return subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.post_commit"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=_env_with_path(),
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Positive matches
# ---------------------------------------------------------------------------

def test_plain_commit_triggers_log(beacon_project_dir):
    result = _run(beacon_project_dir, "git commit -m 'fix: bug'")
    assert result.returncode == 0
    assert "Commit detected" in result.stdout
    assert "/beacon-log" in result.stdout


def test_plain_push_triggers_push(beacon_project_dir):
    result = _run(beacon_project_dir, "git push origin main")
    assert result.returncode == 0
    assert "Push detected" in result.stdout
    assert "/beacon-push" in result.stdout


def test_gcloud_deploy_triggers_deploy(beacon_project_dir):
    result = _run(beacon_project_dir, "gcloud run deploy myapi --image foo")
    assert "Deploy detected" in result.stdout
    assert "/beacon-deploy" in result.stdout


def test_aws_s3_sync_triggers_deploy(beacon_project_dir):
    result = _run(beacon_project_dir, "aws s3 sync ./dist s3://bucket/site")
    assert "Deploy detected" in result.stdout


def test_kubectl_apply_triggers_deploy(beacon_project_dir):
    result = _run(beacon_project_dir, "kubectl apply -f deployment.yaml")
    assert "Deploy detected" in result.stdout


# ---------------------------------------------------------------------------
# Heredoc body suppression (e-613)
# ---------------------------------------------------------------------------

def test_heredoc_commit_with_git_push_body_is_commit_not_push(beacon_project_dir):
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: pipeline\n\n"
        "Previously we ran `git push origin main` here but moved to CI.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert result.returncode == 0
    assert "Commit detected" in result.stdout
    assert "Push detected" not in result.stdout


def test_heredoc_commit_with_gcloud_deploy_body_is_commit_not_deploy(beacon_project_dir):
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "docs(deploy): document the rollout flow\n\n"
        "Run `gcloud run deploy beacon-api-prod ...` to ship.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout
    assert "Deploy detected" not in result.stdout


def test_quoted_commit_message_with_push_word_is_commit(beacon_project_dir):
    cmd = "git commit -m \"refactor: stop running git push from this script\""
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout
    assert "Push detected" not in result.stdout


def test_single_quoted_commit_message_with_deploy_word_is_commit(beacon_project_dir):
    cmd = "git commit -m 'docs: mention gcloud run deploy in README'"
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout
    assert "Deploy detected" not in result.stdout


def test_heredoc_cat_with_push_in_body_does_not_fire(beacon_project_dir):
    cmd = (
        "cat <<'EOF' > docs/release.md\n"
        "Release procedure:\n"
        "1. Run git push origin main\n"
        "2. Wait for CI.\n"
        "EOF"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout
    assert "Commit detected" not in result.stdout


def test_heredoc_unquoted_marker_filtered(beacon_project_dir):
    cmd = (
        "cat <<EOF > /tmp/note.txt\n"
        "We will git push origin main soon.\n"
        "EOF"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout


def test_heredoc_dash_marker_filtered(beacon_project_dir):
    cmd = (
        "cat <<-'END' > /tmp/x\n"
        "\tgit push origin main is a thing\n"
        "\tgcloud run deploy is also a thing\n"
        "\tEND"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout
    assert "Deploy detected" not in result.stdout


def test_chained_commit_with_push_in_heredoc_body(beacon_project_dir):
    cmd = (
        "cd /repo && git add . && git commit -m \"$(cat <<'EOF'\n"
        "fix: cleanup\n\n"
        "Body mentions git push origin main as a side-note.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout
    assert "Push detected" not in result.stdout


def test_actual_push_still_detected(beacon_project_dir):
    result = _run(beacon_project_dir, "git push origin ms-44/work")
    assert "Push detected" in result.stdout


# ---------------------------------------------------------------------------
# Safety: silent when no beacon project
# ---------------------------------------------------------------------------

def test_silent_when_no_beacon(tmp_path):
    result = _run(tmp_path, "git commit -m foo")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_unrelated_command_no_emit(beacon_project_dir):
    result = _run(beacon_project_dir, "ls -la")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_empty_payload_no_crash(beacon_project_dir):
    """Malformed / empty stdin must result in silent fail-safe exit."""
    result = subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.post_commit"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        env=_env_with_path(),
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_tilde_cwd_expanded(beacon_project_dir, monkeypatch):
    """``tool_input.cwd`` may start with ``~``; the hook must expand it
    so it can locate ``.beacon/project.json`` for users whose Bash tool
    sends a tilde-prefixed cwd."""
    import os
    monkeypatch.setenv("HOME", str(beacon_project_dir))
    payload = {
        "tool_input": {
            "command": "git commit -m 'fix: bug'",
            "cwd": "~",
        }
    }
    # Run from a directory that DOESN'T have .beacon so the hook is
    # forced to use the tilde-resolved cwd.
    env = _env_with_path()
    env["HOME"] = str(beacon_project_dir)
    result = subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.post_commit"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT.parent),
        timeout=10,
        env=env,
    )
    assert result.returncode == 0
    assert "Commit detected" in result.stdout


# ---------------------------------------------------------------------------
# Observability: BEACON_HOOK_DEBUG (e-940)
#
# The fail-safe contract collapses no-match / malformed / matched into the
# same silent exit-0. BEACON_HOOK_DEBUG=1 makes them inspectable in
# ~/.beacon/hook-debug.log WITHOUT changing the default (off) behavior.
# ---------------------------------------------------------------------------


def _run_debug(cwd, command, home: Path, debug: bool):
    """Run the hook with HOME redirected to a tmp dir so the debug log lands
    in an isolated location. ``command=None`` sends malformed (non-JSON) stdin.
    """
    env = _env_with_path()
    env["HOME"] = str(home)
    if debug:
        env["BEACON_HOOK_DEBUG"] = "1"
    else:
        env.pop("BEACON_HOOK_DEBUG", None)
    stdin = "not valid json {" if command is None else json.dumps(
        {"tool_input": {"command": command, "cwd": str(cwd)}}
    )
    result = subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.post_commit"],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
        timeout=15,
    )
    return result, home / ".beacon" / "hook-debug.log"


def test_debug_off_writes_no_log(beacon_project_dir, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result, log = _run_debug(beacon_project_dir, "git commit -m x", home, debug=False)
    assert result.returncode == 0
    assert "Commit detected" in result.stdout  # behavior unchanged
    assert not log.exists()  # default: no file, ever


def test_debug_on_logs_matched_and_keeps_stdout(beacon_project_dir, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result, log = _run_debug(beacon_project_dir, "git commit -m x", home, debug=True)
    assert "Commit detected" in result.stdout  # stdout contract preserved
    assert log.exists()
    assert "matched:/beacon-log" in log.read_text(encoding="utf-8")


def test_debug_on_logs_no_match_still_silent_stdout(beacon_project_dir, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result, log = _run_debug(beacon_project_dir, "ls -la", home, debug=True)
    assert result.stdout.strip() == ""  # still silent on stdout
    assert "no-match" in log.read_text(encoding="utf-8")


def test_debug_on_logs_malformed(beacon_project_dir, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result, log = _run_debug(beacon_project_dir, None, home, debug=True)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "malformed" in log.read_text(encoding="utf-8")
