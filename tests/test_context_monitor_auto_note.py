"""Tests for the context-usage-monitor.sh auto-note enrichment fix
(follow-up to ms-31 / 85994cf).

Before this fix, the threshold-triggered note carried only template
placeholders ("（Claudeが記述してください）"), and the actual content was
supposed to be filled in by Claude on the next turn via a systemMessage
nudge. When Claude didn't respond (because the nudge had been softened
to avoid compaction cascades), the saved note had no real content.

These tests run the script end-to-end with a synthetic transcript that
crosses a threshold, and assert that:

  - The persisted note has substantive auto-content (not the old
    template placeholders).
  - At minimum: the "現在地" / "直近のコミット" / "未消化タスク"
    sections render with real data.
  - The legacy "（Claudeが記述してください）" placeholder strings are
    GONE — they are the diagnostic marker of the empty-note bug.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "context-usage-monitor.sh"


def _write_transcript(path: Path, *, model: str, input_tokens: int) -> None:
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


def _run_monitor(transcript: Path, project_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("BEACON_CONTEXT_LIMIT", None)
    payload = json.dumps({
        "session_id": "test-auto-note",
        "transcript_path": str(transcript),
    })
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        env=env,
        timeout=30,
    )


@pytest.fixture
def project_dir(tmp_path):
    """tmp project with an active MS so beacon status / task list returns data."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "auto-note-test",
        "milestones": [
            {
                "id": "ms-31",
                "title": "context-monitor follow-up",
                "status": "in_progress",
                "progress": 0,
                "entries": [
                    {"id": "e-1", "type": "task", "status": "todo",
                     "description": "fill the template placeholders",
                     "meta": {"priority": "high"}},
                    {"id": "e-2", "type": "task", "status": "todo",
                     "description": "another pending task",
                     "meta": {"priority": "middle"}},
                ],
            }
        ],
    }))
    (tmp_path / ".claude").mkdir()
    # init a tiny git repo so `git log --since` returns a real commit
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: fixture seed commit"],
        cwd=str(tmp_path), check=True,
    )
    return tmp_path


def _read_notes(project_dir: Path) -> list[dict]:
    path = project_dir / ".beacon" / "session_notes.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_auto_note_has_no_legacy_placeholder_text(project_dir, tmp_path):
    """Regression guard: the old template placeholder must NEVER appear in
    the saved note text. Its presence is the diagnostic marker that the
    enrichment logic regressed."""
    transcript = tmp_path / "transcript.jsonl"
    # 40K / 200K = 20% — triggers the first threshold.
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=40000)
    _run_monitor(transcript, project_dir)
    notes = _read_notes(project_dir)
    assert notes, "expected at least one auto-note to be persisted"
    body = notes[-1].get("text", "")
    assert "（Claudeが記述してください）" not in body, (
        "found legacy placeholder; auto-content enrichment regressed"
    )


def test_auto_note_includes_recent_commits_section(project_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=40000)
    _run_monitor(transcript, project_dir)
    body = _read_notes(project_dir)[-1]["text"]
    assert "### 直近のコミット" in body
    # The seed commit from the fixture must show up (last 6h).
    assert "test: fixture seed commit" in body


def test_auto_note_includes_pending_tasks_section(project_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=40000)
    _run_monitor(transcript, project_dir)
    body = _read_notes(project_dir)[-1]["text"]
    assert "### Active MS の未消化タスク" in body
    # Both fixture tasks should appear, priority sorted (high before middle).
    assert "e-1" in body and "e-2" in body
    assert body.index("e-1") < body.index("e-2")


def test_auto_note_body_contains_only_auto_captured_state(project_dir, tmp_path):
    """ms-57 e-1195: the script-written note must contain ONLY auto-captured
    state (現在地 / 直近コミット / 未消化タスク). The 'decision / discussion /
    next action' section is split out and Claude is asked to add it as a
    SEPARATE note via the Stop hook prompt. This avoids the legacy 'placeholder
    sections that nobody fills' anti-pattern that TrailNode collaborator
    criticized 2026-06-07.

    Pre-e-1195 the script-written note had '### このセッションで決めたこと
    （任意 — Claude が追記）' as a placeholder that Claude rarely filled in.
    """
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=40000)
    _run_monitor(transcript, project_dir)
    body = _read_notes(project_dir)[-1]["text"]
    # The script note must have the 3 auto-captured sections.
    assert "### 現在地" in body
    assert "### 直近のコミット" in body
    assert "### Active MS の未消化タスク" in body
    # The script note must NOT have placeholder Claude sections (they get
    # asked via the Stop hook prompt instead, written to a separate note).
    assert "決めたこと（任意" not in body, (
        "Stale optional-marked placeholder regressed; remove or rename — "
        "the script note must not invite empty fills"
    )
    assert "次のアクション（任意" not in body


def test_auto_note_fires_once_per_threshold(project_dir, tmp_path):
    """Don't re-fire the same threshold within one session — state file
    in .claude/ tracks notified_thresholds."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, model="claude-sonnet-4-5", input_tokens=40000)
    _run_monitor(transcript, project_dir)
    _run_monitor(transcript, project_dir)
    notes = _read_notes(project_dir)
    # Only one note for the 20% threshold should have been saved.
    twenty_pct = [n for n in notes if "自動記録 20%" in n.get("text", "")]
    assert len(twenty_pct) == 1
