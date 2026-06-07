"""Tests for the Python port of context-usage-monitor (ms-44 e-854).

The bash version (`bin/context-usage-monitor.sh`) doesn't work on Windows
because Claude Code's hook runner has no bash + jq + sed/awk. The Python
port at ``beacon_cli.hooks.context_monitor`` reuses the same stdin
contract / state file path / additionalContext JSON shape so existing
hook configurations and end-to-end behavior carry over.

These tests target the pure logic (threshold computation, template
generation, dry-run side-effect suppression) without spawning a real
subprocess — that keeps them fast and works regardless of whether bash
is installed on the test host.

The Mac/Linux end-to-end tests against the bash script still live in
``test_context_monitor_limit.py`` / ``test_context_monitor_auto_note.py``
and remain the source of truth for behavioral parity.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import pytest

# Make beacon_cli importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beacon_cli.hooks import context_monitor as cm  # noqa: E402


# ---------------------------------------------------------------------------
# _resolve_context_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4-7[1m]", cm.DEFAULT_CONTEXT_LIMIT_1M),
        ("claude-sonnet-4-6[1m]", cm.DEFAULT_CONTEXT_LIMIT_1M),
        ("claude-sonnet-4-5", cm.DEFAULT_CONTEXT_LIMIT_200K),
        ("claude-opus-4", cm.DEFAULT_CONTEXT_LIMIT_200K),
        ("", cm.DEFAULT_CONTEXT_LIMIT_200K),
        ("foo-1m-bar", cm.DEFAULT_CONTEXT_LIMIT_1M),
    ],
)
def test_resolve_context_limit_model_inference(monkeypatch, model, expected):
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)
    assert cm._resolve_context_limit(model) == expected


def test_resolve_context_limit_env_var_overrides_model(monkeypatch):
    """BEACON_CONTEXT_LIMIT must beat the model-name inference. Same
    contract as the bash version's test_env_var_override_takes_precedence."""
    monkeypatch.setenv("BEACON_CONTEXT_LIMIT", "500000")
    assert cm._resolve_context_limit("claude-opus-4-7[1m]") == 500_000


def test_resolve_context_limit_env_var_malformed_falls_through(monkeypatch):
    """Garbage env var must NOT crash — fall back to inference / default."""
    monkeypatch.setenv("BEACON_CONTEXT_LIMIT", "not-a-number")
    assert cm._resolve_context_limit("claude-opus-4-7[1m]") == cm.DEFAULT_CONTEXT_LIMIT_1M
    assert cm._resolve_context_limit("") == cm.DEFAULT_CONTEXT_LIMIT_200K


# ---------------------------------------------------------------------------
# _current_context_tokens
# ---------------------------------------------------------------------------


def test_current_context_sums_three_buckets():
    """Mirrors the bash jq: input + cache_read + cache_creation. Output
    tokens are intentionally excluded — they don't return to the context
    window on the next turn."""
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 3000,
        "output_tokens": 9999,  # must NOT be counted
    }
    assert cm._current_context_tokens(usage) == 6000


def test_current_context_handles_missing_fields():
    assert cm._current_context_tokens({}) == 0
    assert cm._current_context_tokens({"input_tokens": None}) == 0
    assert cm._current_context_tokens({"input_tokens": "12"}) == 12


# ---------------------------------------------------------------------------
# _classify_thresholds
# ---------------------------------------------------------------------------


def test_classify_thresholds_first_threshold_fires_at_20():
    triggered, crossed = cm._classify_thresholds(20, [])
    assert triggered == 20
    assert crossed == [20]


def test_classify_thresholds_jump_to_67_marks_all_below_and_triggers_highest():
    """The bash precedent: a session that jumps from 0 to 67% should mark
    20/40/60 all as notified AND emit the 60% threshold notification.
    Re-test that contract exactly so test_context_monitor_limit.py edge cases
    stay covered by the Python port too."""
    triggered, crossed = cm._classify_thresholds(67, [])
    assert triggered == 60
    assert crossed == [20, 40, 60]


def test_classify_thresholds_already_notified_returns_none():
    triggered, crossed = cm._classify_thresholds(25, [20])
    # 20 already notified, 40 not yet reached → no new threshold to fire.
    assert triggered is None
    assert crossed == [20]


def test_classify_thresholds_below_first_threshold_is_silent():
    triggered, crossed = cm._classify_thresholds(15, [])
    assert triggered is None
    assert crossed == []


def test_classify_thresholds_100_percent_included():
    """100% must be a real threshold — the bash recently added it (ms-31)
    and the Python port must match."""
    assert 100 in cm.THRESHOLDS
    triggered, crossed = cm._classify_thresholds(100, [20, 40, 60, 80])
    assert triggered == 100
    assert 100 in crossed


# ---------------------------------------------------------------------------
# _load_state / _save_state (filesystem side effects)
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_when_file_missing(tmp_path):
    state_path = tmp_path / ".claude" / "context-usage-state.json"
    prev, notified = cm._load_state(state_path, "session-A")
    assert prev == ""
    assert notified == []


def test_load_state_resets_when_session_changed(tmp_path):
    state_path = tmp_path / ".claude" / "context-usage-state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"session_id": "old", "notified_thresholds": [20, 40]}))
    prev, notified = cm._load_state(state_path, "new")
    assert prev == "old"
    # Reset because session changed — bash does this too.
    assert notified == []


def test_load_state_preserves_when_same_session(tmp_path):
    state_path = tmp_path / ".claude" / "context-usage-state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"session_id": "S", "notified_thresholds": [20, 40]}))
    prev, notified = cm._load_state(state_path, "S")
    assert prev == "S"
    assert notified == [20, 40]


def test_save_state_creates_parent_dir(tmp_path):
    state_path = tmp_path / "fresh" / "context-usage-state.json"
    cm._save_state(state_path, "S", [20, 40, 60])
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["session_id"] == "S"
    assert data["notified_thresholds"] == [20, 40, 60]


# ---------------------------------------------------------------------------
# _latest_assistant_usage (transcript parsing)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_latest_assistant_usage_picks_last_assistant_with_usage(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {"message": {"role": "user", "content": "hi"}},
            {
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 100},
                }
            },
            {
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-7[1m]",
                    "usage": {"input_tokens": 500_000},
                }
            },
        ],
    )
    usage, model = cm._latest_assistant_usage(str(transcript))
    assert usage == {"input_tokens": 500_000}
    assert model == "claude-opus-4-7[1m]"


def test_latest_assistant_usage_missing_file_returns_none(tmp_path):
    usage, model = cm._latest_assistant_usage(str(tmp_path / "nope.jsonl"))
    assert usage is None
    assert model == ""


def test_latest_assistant_usage_handles_garbage_lines(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "this is not json\n"
        + json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "model": "x",
                    "usage": {"input_tokens": 1},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage, model = cm._latest_assistant_usage(str(transcript))
    assert usage == {"input_tokens": 1}
    assert model == "x"


# ---------------------------------------------------------------------------
# Note body template
# ---------------------------------------------------------------------------


def test_build_note_body_has_three_auto_sections():
    """ms-57 e-1195: script note must contain ONLY auto-captured state.
    Mirrors test_auto_note_body_contains_only_auto_captured_state from the
    bash suite so a regression in either implementation is caught."""
    body = cm._build_note_body(
        percent=20,
        note_date="2026-06-08 09:00",
        location_text="- Active MS: ms-44 \"x\" (50% / 1/2タスク完了)",
        recent_commits_text="- abc1234: test commit",
        pending_tasks_text="- [high   ] e-1: pending",
    )
    assert "## コンテキストサマリー（自動記録 20% / 2026-06-08 09:00）" in body
    assert "### 現在地" in body
    assert "### 直近のコミット" in body
    assert "### Active MS の未消化タスク" in body
    # Anti-empty-placeholder guard (e-1195).
    assert "（任意" not in body, "must not invite empty fills"
    assert "（Claudeが記述してください）" not in body


def test_build_note_body_uses_fallback_when_location_empty():
    body = cm._build_note_body(
        percent=40,
        note_date="2026-06-08 09:00",
        location_text="",
        recent_commits_text="（直近6時間にコミットなし）",
        pending_tasks_text="（active MS なし）",
    )
    assert "（取得失敗）" in body


# ---------------------------------------------------------------------------
# Template instruction (Claude-facing additionalContext)
# ---------------------------------------------------------------------------


def test_build_template_instruction_mentions_beacon_note():
    """test_context_monitor_limit::test_threshold_notification_uses_hook_specific_output
    asserts the additionalContext contains the phrase ``beacon note``. Lock
    the same contract for the Python port."""
    text = cm._build_template_instruction(
        percent=20,
        current_context=200_000,
        context_limit=1_000_000,
        triggered_threshold=20,
        note_date="2026-06-08 09:00",
    )
    assert "beacon note" in text
    # The three decision-section headers (post-e-1195) must be present.
    assert "### 決定事項" in text
    assert "### 議論の要点" in text
    assert "### 次のアクション" in text


def test_build_template_instruction_formats_token_count_with_commas():
    text = cm._build_template_instruction(
        percent=20,
        current_context=200_000,
        context_limit=1_000_000,
        triggered_threshold=20,
        note_date="2026-06-08 09:00",
    )
    # bash uses python3 `print(f'{int(...):,}')` — Python port must match.
    assert "1,000,000 tokens" in text


# ---------------------------------------------------------------------------
# End-to-end: _main_impl with dry-run (no side effects)
# ---------------------------------------------------------------------------


@pytest.fixture
def beacon_project(tmp_path, monkeypatch):
    """Mimic the bash fixture: tmp dir with .beacon/project.json so the
    project-root guard passes."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "dry-run-test", "milestones": []})
    )
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_main_with_payload(
    payload: dict, monkeypatch, stdout_buf=None, stderr_buf=None
) -> tuple[int, str, str]:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = stdout_buf or io.StringIO()
    err = stderr_buf or io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = cm.main()
    return rc, out.getvalue(), err.getvalue()


def test_main_dry_run_emits_hook_output_but_no_side_effects(
    beacon_project, tmp_path, monkeypatch
):
    """In dry-run mode the hook still parses + emits hookSpecificOutput, but
    must NOT call ``beacon note`` and must NOT touch the state file. The
    test guarantees this by checking the state file stays absent."""
    monkeypatch.setenv("BEACON_CONTEXT_MONITOR_DRY_RUN", "1")
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "usage": {
                        "input_tokens": 40_000,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            }
        ],
    )

    rc, stdout, stderr = _run_main_with_payload(
        {"session_id": "S-dry", "transcript_path": str(transcript)},
        monkeypatch,
    )
    assert rc == 0

    # State file must NOT exist — dry-run.
    state_path = beacon_project / ".claude" / "context-usage-state.json"
    assert not state_path.exists(), (
        "dry-run must not write the state file; saw side effect"
    )

    # The hook JSON should be in stdout (with the Stop event name).
    payload = json.loads(stdout.strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "閾値 20%" in payload["hookSpecificOutput"]["additionalContext"]


def test_main_skips_when_no_beacon_project(tmp_path, monkeypatch):
    """No .beacon/project.json → silent exit (matches bash early exit)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_CONTEXT_MONITOR_DRY_RUN", "1")
    rc, stdout, stderr = _run_main_with_payload(
        {"session_id": "S", "transcript_path": "/dev/null"},
        monkeypatch,
    )
    assert rc == 0
    assert stdout == ""


def test_main_skips_when_payload_missing_fields(beacon_project, monkeypatch):
    monkeypatch.setenv("BEACON_CONTEXT_MONITOR_DRY_RUN", "1")
    rc, stdout, stderr = _run_main_with_payload({}, monkeypatch)
    assert rc == 0
    assert stdout == ""


def test_main_skips_when_percent_over_100(beacon_project, tmp_path, monkeypatch):
    """Post-compaction continuation produces percent > 100 — bash skips,
    Python must too."""
    monkeypatch.setenv("BEACON_CONTEXT_MONITOR_DRY_RUN", "1")
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 300_000},  # 150% of 200K
                }
            }
        ],
    )
    rc, stdout, stderr = _run_main_with_payload(
        {"session_id": "S", "transcript_path": str(transcript)},
        monkeypatch,
    )
    assert rc == 0
    assert stdout == ""  # no hook output


def test_main_emits_no_output_when_already_notified(
    beacon_project, tmp_path, monkeypatch
):
    """If the state file says the threshold was already notified, the
    hook must stay silent on a repeat fire — mirrors
    test_auto_note_fires_once_per_threshold from the bash suite."""
    monkeypatch.setenv("BEACON_CONTEXT_MONITOR_DRY_RUN", "1")
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)

    state_path = beacon_project / ".claude" / "context-usage-state.json"
    state_path.write_text(
        json.dumps({"session_id": "S-rep", "notified_thresholds": [20]})
    )

    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 40_000},  # 20%
                }
            }
        ],
    )
    rc, stdout, stderr = _run_main_with_payload(
        {"session_id": "S-rep", "transcript_path": str(transcript)},
        monkeypatch,
    )
    assert rc == 0
    assert stdout == ""
