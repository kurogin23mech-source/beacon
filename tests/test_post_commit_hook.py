"""Tests for bin/beacon-post-commit-hook.sh (ms-43 e-613).

The hook should:
  - emit /beacon-log prompt when Bash runs `git commit ...`
  - emit /beacon-push prompt when Bash runs `git push ...`
  - emit /beacon-deploy prompt for recognized deploy commands
  - NOT misfire when those patterns appear inside *quoted strings or heredoc
    bodies* of a commit message. e-613: heredoc commit messages like
    `git commit -m "$(cat <<'EOF' ... git push ... EOF )"` were tripping the
    push/deploy detection because the entire substitution body was visible to
    the matcher.

The hook is a fairly thin sed-and-grep wrapper, so the tests just exercise
those pattern checks via subprocess against representative stdin payloads.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "beacon-post-commit-hook.sh"


@pytest.fixture
def beacon_project_dir(tmp_path):
    """tmp dir with a minimal .beacon/project.json so the hook proceeds."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "PostCommitHookTest",
        "summary": "",
        "milestones": [],
    }))
    return tmp_path


def _run(cwd: Path, command: str) -> subprocess.CompletedProcess:
    payload = {"tool_input": {"command": command, "cwd": str(cwd)}}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Positive: real commit / push / deploy commands should trigger
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


# ---------------------------------------------------------------------------
# Negative (e-613): patterns inside heredoc / quoted commit body must not fire
# ---------------------------------------------------------------------------

def test_heredoc_commit_with_git_push_body_is_commit_not_push(beacon_project_dir):
    """Commit message body mentioning 'git push' must trigger COMMIT, not PUSH."""
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
    assert "Deploy detected" not in result.stdout


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
    assert "Push detected" not in result.stdout


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


def test_heredoc_body_with_inner_double_quote_is_commit_not_push(beacon_project_dir):
    """The 'real' e-613 case: heredoc body contains an inner double quote so the
    simple `s/"[^"]*"//g` strip leaves an unquoted 'git push' fragment behind.
    """
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: rename a flag\n\n"
        "Previously the docs said \"run beacon X\". "
        "We then forgot to run git push origin main and shipped a stale README.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Push detected" not in result.stdout, result.stdout


def test_heredoc_body_with_inner_double_quote_no_deploy(beacon_project_dir):
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "docs: clarify deploy notes\n\n"
        "Mentions \"gcloud run deploy\" as the canonical command.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Deploy detected" not in result.stdout, result.stdout


def test_heredoc_body_with_odd_inner_quotes_no_push(beacon_project_dir):
    """Hard e-613 case: body has an ODD count of inner double quotes, so the
    naive `s/"[^"]*"//g` leaves an unpaired half — and a subsequent
    `git push` line outside any remaining quote pair could fool the matcher.

    The body below contains exactly 3 unescaped double-quotes before the trail.
    """
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: rename a flag from \"foo\" to \"bar\n\n"
        "Then we ran git push origin main and noticed regression.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Push detected" not in result.stdout, result.stdout


def test_heredoc_body_with_odd_inner_quotes_no_deploy(beacon_project_dir):
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "docs(deploy): correct the \"--cache\" flag and other typos\n\n"
        "We should use gcloud run deploy beacon-api-prod (NOT staging).\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Deploy detected" not in result.stdout, result.stdout


# Real-world e-613 cases: the BARE bash command runs a chain like
# `cd X && git add . && git commit -m "$(cat <<'EOF' ... git push ... EOF )"`.
# The first branch checked is `git commit ` so this still wins, but a body that
# CONTAINS `git push` (with the cmd ALSO not strictly starting with
# `git commit`) can leak through. Make sure the precedence is robust.

def test_chained_commit_with_push_in_heredoc_body(beacon_project_dir):
    """A real failure mode: the parent command chains other operations before
    the commit, then the heredoc body mentions 'git push'. The commit pattern
    must still win over the push pattern."""
    cmd = (
        "cd /repo && git add . && git commit -m \"$(cat <<'EOF'\n"
        "fix: cleanup\n\n"
        "Body mentions git push origin main as a side-note.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Push detected" not in result.stdout, result.stdout


def test_chained_commit_with_deploy_in_heredoc_body(beacon_project_dir):
    cmd = (
        "git add -A && git commit -m \"$(cat <<'EOF'\n"
        "chore: tidy up\n\n"
        "Notes: gcloud run deploy beacon-api-prod was rolled back yesterday.\n"
        "EOF\n"
        ")\""
    )
    result = _run(beacon_project_dir, cmd)
    assert "Commit detected" in result.stdout, result.stdout
    assert "Deploy detected" not in result.stdout, result.stdout


def test_actual_push_still_detected(beacon_project_dir):
    """Make sure we don't over-suppress: a legitimate plain push still fires."""
    cmd = "git push origin ms-43/work"
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" in result.stdout


# ---------------------------------------------------------------------------
# The REAL e-613 case: a plain heredoc Bash command (NOT a git commit) whose
# body happens to contain `git push` / deploy strings. Without filtering the
# heredoc body, the hook misfires.
# ---------------------------------------------------------------------------

def test_heredoc_cat_with_push_in_body_does_not_fire(beacon_project_dir):
    """A `cat <<EOF > file ... EOF` writing a doc that mentions `git push`
    must NOT trigger /beacon-push."""
    cmd = (
        "cat <<'EOF' > docs/release.md\n"
        "Release procedure:\n"
        "1. Run git push origin main\n"
        "2. Wait for CI.\n"
        "EOF"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout, result.stdout
    assert "Commit detected" not in result.stdout, result.stdout


def test_heredoc_cat_with_deploy_in_body_does_not_fire(beacon_project_dir):
    cmd = (
        "cat <<'EOF' > docs/deploy.md\n"
        "How to deploy:\n"
        "  gcloud run deploy beacon-api-prod --region us-central1\n"
        "EOF"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Deploy detected" not in result.stdout, result.stdout


def test_heredoc_unquoted_marker_also_filtered(beacon_project_dir):
    """Some heredocs use unquoted markers: `<<EOF ... EOF`. Must still filter."""
    cmd = (
        "cat <<EOF > /tmp/note.txt\n"
        "We will git push origin main soon.\n"
        "EOF"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout, result.stdout


def test_heredoc_dash_marker_also_filtered(beacon_project_dir):
    """Indented heredocs use <<- to allow tabs."""
    cmd = (
        "cat <<-'END' > /tmp/x\n"
        "\tgit push origin main is a thing\n"
        "\tgcloud run deploy is also a thing\n"
        "\tEND"
    )
    result = _run(beacon_project_dir, cmd)
    assert "Push detected" not in result.stdout, result.stdout
    assert "Deploy detected" not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_silent_when_no_beacon(tmp_path):
    """No .beacon/project.json → silent exit."""
    result = _run(tmp_path, "git commit -m foo")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_unrelated_command_no_emit(beacon_project_dir):
    result = _run(beacon_project_dir, "ls -la")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
