"""Tests for the context-usage-monitor.sh dynamic context-limit detection (e-561).

These tests exercise the shell script end-to-end with synthetic transcript
JSONL files, asserting that the percent calculation uses the correct denominator
for each model class.

The script self-skips when there is no .beacon/project.json in the CWD, so each
test sets up a temp dir with one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "context-usage-monitor.sh"


def _write_transcript(path: Path, *, model: str, input_tokens: int) -> None:
    """Write a minimal JSONL transcript with one assistant message having `usage`.

    The script reads in reverse, so we just emit one line — that's what it'll
    pick up.
    """
    entry = {
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    }
    path.write_text(json.dumps(entry) + "\n")


def _run_monitor(transcript: Path, project_dir: Path, env_extra: dict | None = None) -> str:
    """Run the script and capture stderr (which has the 'context_limit=...' log).

    Returns stderr. Stdout is ignored — it only fires when a threshold is
    triggered.
    """
    env = os.environ.copy()
    env.pop("BEACON_CONTEXT_LIMIT", None)  # tests set this explicitly when needed
    if env_extra:
        env.update(env_extra)
    payload = json.dumps({
        "session_id": "test-session",
        "transcript_path": str(transcript),
    })
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        env=env,
        timeout=20,
    )
    return result.stderr + "\n" + result.stdout


@pytest.fixture
def project_dir(tmp_path):
    """tmp dir with a minimal .beacon/project.json so the script doesn't early-exit."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({"name": "t", "milestones": []}))
    # Also create .claude dir so the state file write doesn't fail
    (tmp_path / ".claude").mkdir()
    return tmp_path


def test_200k_model_uses_200000_denominator(project_dir, tmp_path):
    """Models without [1m] suffix should report against 200K."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=100000)

    out = _run_monitor(transcript, project_dir)
    # 100K / 200K = 50%
    assert "context_limit=200000" in out
    assert "percent=50%" in out


def test_1m_model_uses_1000000_denominator(project_dir, tmp_path):
    """Opus / Sonnet with the [1m] suffix should report against 1M.

    Crucially: at 200K input the old hardcoded denominator would have reported
    100% and the Stop hook would skip entirely (the bug e-561 fixes). With the
    new logic we should see 20% — exactly the first threshold.
    """
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-opus-4-7[1m]", input_tokens=200000)

    out = _run_monitor(transcript, project_dir)
    assert "context_limit=1000000" in out
    assert "percent=20%" in out


def test_env_var_override_takes_precedence(project_dir, tmp_path):
    """BEACON_CONTEXT_LIMIT must beat the model-name inference."""
    transcript = tmp_path / "transcript.jsonl"
    # Even though the model says 1M, the env var should override.
    _write_transcript(transcript, model="claude-opus-4-7[1m]", input_tokens=100000)

    out = _run_monitor(transcript, project_dir, env_extra={"BEACON_CONTEXT_LIMIT": "500000"})
    assert "context_limit=500000" in out
    # 100K / 500K = 20%
    assert "percent=20%" in out


def test_fallback_to_200k_when_model_unknown(project_dir, tmp_path):
    """Unknown / empty model string falls back to 200K (safe legacy default)."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="", input_tokens=80000)

    out = _run_monitor(transcript, project_dir)
    assert "context_limit=200000" in out
    assert "percent=40%" in out


def test_no_skip_at_300k_with_1m_model(project_dir, tmp_path):
    """Reproduce the original bug scenario in reverse.

    Before the fix: 300K input → percent = 150% → script skips ("post-compaction"
    branch).
    After the fix: with 1M denominator, percent = 30% → no skip, threshold 20%
    fires.
    """
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-opus-4-7[1m]", input_tokens=300000)

    out = _run_monitor(transcript, project_dir)
    assert "percent=30%" in out
    assert "post-compaction" not in out  # must NOT have skipped
