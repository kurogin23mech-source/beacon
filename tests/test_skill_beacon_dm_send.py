"""Tests for the /beacon-dm-send Skill (ms-54 follow-up).

Two layers are exercised here:

  1. **SKILL.md structural validation** — the Skill markdown must have
     the right frontmatter shape (name / description / triggers) and
     mention the key composition steps. If a future edit accidentally
     drops the recipient-discovery step or the budget-gate handling
     section, this test fails.

  2. **Helpers unit tests** — the CLI composition logic that the Skill
     prose delegates to lives in
     ``beacon_cli/skills_helpers/dm_send.py``. We exercise it directly
     so today's dogfood failure modes (wrong recipient project, forgot
     ``--to``, payload JSON hand-rolled, ``--healthy`` filter regression)
     are locked down.

Why not run the Skill end-to-end via subprocess: the Skill body is
Claude-side prose that prompts a human for input. Calling it without an
interactive harness is meaningless. The helpers carry all the logic
that's *not* prose, so testing those gets us the regression coverage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from beacon_cli.skills_helpers import dm_send  # noqa: E402


# ---------------------------------------------------------------------------
# SKILL.md structural validation
# ---------------------------------------------------------------------------


SKILL_PATH = REPO / "skills" / "beacon-dm-send.md"


def test_skill_file_exists():
    assert SKILL_PATH.exists(), (
        "skills/beacon-dm-send.md must exist — the Skill is invoked via "
        "the markdown filename, not via a Python entry-point."
    )


def test_skill_has_well_formed_frontmatter():
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Frontmatter delimited by --- on its own lines.
    assert body.startswith("---\n"), "frontmatter must start at line 1"
    end = body.find("\n---\n", 4)
    assert end > 0, "frontmatter must close with --- on its own line"
    fm = body[4:end]
    assert "name: beacon-dm-send" in fm
    assert "description:" in fm
    # The trigger list MUST include the slash command itself, otherwise
    # `/beacon-dm-send` doesn't route to this Skill from Claude's harness.
    assert "/beacon-dm-send" in fm


def test_skill_documents_key_workflow_steps():
    """If a future edit drops a step, the dogfood failure mode comes back."""
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Step 1 — recipient discovery via directory + healthy filter
    assert "bus directory" in body
    assert "--healthy" in body, (
        "the Skill must mention the Option C healthy filter; falling "
        "back silently means the user can't tell when delivery is risky."
    )
    # Step 2 — member cross-reference
    assert "beacon member list" in body
    # Step 4 — cross-project handling
    assert "project_id" in body
    assert "cross-project" in body.lower() or "cross project" in body.lower()
    # Step 5/7 — payload is composed, not hand-rolled
    assert "JSON" in body or "json" in body
    # Step 6 — confirmation before send
    assert "yes" in body.lower() and "cancel" in body.lower()
    # Step 8 — verify is optional, not default
    assert "--verify" in body


# ---------------------------------------------------------------------------
# ms-92 e-2181: 確認 step を 1 回に集約
# ---------------------------------------------------------------------------


def test_skill_documents_unified_one_confirm_path():
    """ms-92 e-2181: 旧 Step 5 / 5b / 5c / Step 6 を 1 step に統合。

    本文入力直後に組み立てた draft + send/edit/cancel が **1 prompt** で完結
    する経路を Skill 本文に明示すること。 これが落ちると user が同じ内容を
    2 回見る冗長 UX が回帰する。
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    # 統合経路の自己言及 (= ms-92 e-2181 で集約された旨)
    assert "e-2181" in body, "consolidation must reference its driving task"
    assert "1 prompt" in body or "1 confirm" in body or "統合" in body, (
        "Skill must document that send-confirmation is one merged prompt"
    )
    # 旧 Step 6 が独立した「## Step 6: 送信確認」 として復活していないことを保証
    assert "## Step 6: 送信確認" not in body, (
        "old standalone Step 6 must not be re-introduced — that brings back "
        "the redundant two-prompt UX from before ms-92 e-2181"
    )


def test_skill_documents_skip_dialog_when_launch_message_is_complete():
    """AC #2: 起動メッセージで recipient + content が揃っていれば追加対話を skip。"""
    body = SKILL_PATH.read_text(encoding="utf-8")
    # 入力経路判定の存在 (Step 5a)
    assert "Step 5a" in body, "Step 5a (= 入力経路判定) must be present"
    assert "recipient" in body and "content" in body, (
        "Skill must explain that recipient + content are the gating fields"
    )
    # skip 経路の語彙が明示されていること
    assert "skip" in body.lower(), (
        "Skill must mention the skip path for fully-specified launch messages"
    )


def test_skill_documents_inline_warnings_in_single_prompt():
    """AC #3: live check / cross-project / budget / template は draft と同じ 1 prompt 内 inline。"""
    body = SKILL_PATH.read_text(encoding="utf-8")
    # draft セクション内で 4 つの inline 表示要素が言及されていること
    assert "live check" in body, "live check warning must be inline in draft"
    assert "cross-project" in body, "cross-project warning must be inline"
    assert "budget" in body, "budget status must be inline"
    assert "template" in body or "テンプレート" in body, (
        "template selection must be inline"
    )
    # inline 化の語彙
    assert "inline" in body, (
        "Skill must explicitly state warnings/status are rendered inline "
        "in the single draft prompt"
    )


def test_skill_documents_force_confirm_for_dangerous_paths():
    """AC #4: dangerous 経路 (= cross-project / 外部 user 初回 / sensitivity high) は明示確認 force 維持。"""
    body = SKILL_PATH.read_text(encoding="utf-8")
    # force-confirm セクション
    assert "Step 5d" in body, "Step 5d (= force-confirm for dangerous paths) must exist"
    # 3 つの危険条件
    assert "cross-project" in body
    assert "外部 user 初回" in body or "外部 user" in body
    assert "sensitivity high" in body or "sensitivity" in body
    # 明示文言入力強制 (= 単純 yes ではない)
    assert "risk understood" in body, (
        "dangerous paths must require an explicit force-yes phrase to "
        "prevent silent dispatch under cross-project / first-time external / "
        "sensitivity-high conditions"
    )


def test_skill_preserves_receipt_verify_step():
    """AC #5: 既存 receipt verify (= sent / delivered / opened の 3 段 stamp) はそのまま残す。"""
    body = SKILL_PATH.read_text(encoding="utf-8")
    # 3 段 receipt 確認の語彙
    assert "sent" in body.lower()
    assert "delivered" in body.lower()
    assert "opened" in body.lower()
    # bus status による receipt 取得
    assert "bus status" in body, (
        "receipt verification via `beacon bus status` must remain part of "
        "the post-send flow (Step 8) — e-2181 consolidates Step 5/6 only, "
        "not the receipt path"
    )


# ---------------------------------------------------------------------------
# Helpers: build_candidates / member cross-ref
# ---------------------------------------------------------------------------


def _session(sid: str, *, email="", machine="", agent="", age=None,
              healthy=None, project_id=""):
    s = {
        "session_id": sid,
        "actor": {"email": email, "machine": machine, "agent": agent,
                  "project_id": project_id},
        "last_active": "2026-06-09T00:00:01.000000Z",
    }
    if age is not None or healthy is not None:
        s["poll_health"] = {}
        if age is not None:
            s["poll_health"]["age_seconds"] = age
        if healthy is not None:
            s["poll_health"]["healthy"] = healthy
    return s


def test_build_candidates_minimal_no_member_info():
    sessions = [_session("aa60cc21-foo", machine="DESKTOP", agent="DESKTOP")]
    rows = dm_send.build_candidates(sessions, members=[])
    assert len(rows) == 1
    assert rows[0]["index"] == 1
    assert rows[0]["session_id"] == "aa60cc21-foo"
    assert rows[0]["machine"] == "DESKTOP"
    # No member cross-ref → email blank, role blank
    assert rows[0]["email"] == ""
    assert rows[0]["member_role"] == ""


def test_build_candidates_cross_references_member_by_email():
    sessions = [_session("aa60cc21", email="dolphin.orca@gmail.com", machine="MAC")]
    members = [
        {"id": "dolphin", "email": "dolphin.orca@gmail.com", "role": "editor",
         "name": "Dolphin"},
    ]
    rows = dm_send.build_candidates(sessions, members=members)
    assert rows[0]["email"] == "dolphin.orca@gmail.com"
    assert rows[0]["member_role"] == "editor"


def test_build_candidates_member_match_by_machine_id_when_email_missing():
    """e.g. solo user adds themselves as member id=DESKTOP-CHG6PAT."""
    sessions = [_session("aa60", machine="DESKTOP-CHG6PAT")]
    members = [{"id": "DESKTOP-CHG6PAT", "email": "", "role": "owner"}]
    rows = dm_send.build_candidates(sessions, members=members)
    assert rows[0]["member_id"] == "DESKTOP-CHG6PAT"
    assert rows[0]["member_role"] == "owner"


def test_build_candidates_option_c_poll_health():
    """When `poll_health` is present (Option C), healthy + age are surfaced."""
    sessions = [_session("aa60", machine="MAC", age=3, healthy=True)]
    rows = dm_send.build_candidates(sessions)
    assert rows[0]["healthy"] is True
    assert rows[0]["age_seconds"] == 3
    assert rows[0]["age_str"] == "age 3s"


def test_build_candidates_pre_option_c_defaults_to_healthy():
    """Pre-Option-C server: no poll_health field. We trust last_active."""
    sessions = [_session("aa60", machine="MAC")]  # no poll_health
    rows = dm_send.build_candidates(sessions)
    assert rows[0]["healthy"] is True
    assert rows[0]["age_seconds"] is None
    assert rows[0]["age_str"] == "age=?"


def test_build_candidates_stale_session():
    sessions = [_session("aa60", machine="MAC", age=900, healthy=False)]
    rows = dm_send.build_candidates(sessions)
    assert rows[0]["healthy"] is False
    assert rows[0]["age_str"] == "age 15m"


# ---------------------------------------------------------------------------
# Helpers: render_candidate_line
# ---------------------------------------------------------------------------


def test_render_candidate_line_includes_session_machine_health():
    sessions = [_session("aa60cc21-abc", machine="DESKTOP", age=3, healthy=True)]
    row = dm_send.build_candidates(sessions)[0]
    line = dm_send.render_candidate_line(row)
    assert "1." in line
    assert "machine=DESKTOP" in line
    assert "aa60cc21" in line
    assert "healthy" in line
    assert "age 3s" in line


def test_render_candidate_line_marks_stale_explicitly():
    sessions = [_session("aa60", machine="MAC", age=900, healthy=False)]
    row = dm_send.build_candidates(sessions)[0]
    line = dm_send.render_candidate_line(row)
    assert "stale" in line
    assert "healthy" not in line  # must not falsely advertise health


def test_render_candidate_line_appends_email_when_known():
    sessions = [_session("aa60", machine="MAC", email="user@x.com")]
    members = [{"id": "user", "email": "user@x.com", "role": "editor"}]
    row = dm_send.build_candidates(sessions, members=members)[0]
    line = dm_send.render_candidate_line(row)
    assert "user@x.com" in line
    assert "editor" in line


# ---------------------------------------------------------------------------
# Helpers: build_directory_cmd
# ---------------------------------------------------------------------------


def test_build_directory_cmd_includes_healthy_by_default():
    argv = dm_send.build_directory_cmd()
    assert argv[:3] == ["beacon", "bus", "directory"]
    assert "--live" in argv
    assert "--healthy" in argv
    assert "--json" in argv
    assert "--since-min" in argv


def test_build_directory_cmd_fallback_omits_healthy():
    """When --healthy is unsupported, the Skill calls with healthy=False."""
    argv = dm_send.build_directory_cmd(healthy=False)
    assert "--healthy" not in argv
    assert "--live" in argv  # live is still passed


def test_build_directory_cmd_cross_project():
    argv = dm_send.build_directory_cmd(project_id="other-project-id-123")
    assert "--project" in argv
    assert "other-project-id-123" in argv


# ---------------------------------------------------------------------------
# Helpers: build_send_cmd
# ---------------------------------------------------------------------------


def test_build_send_cmd_minimal_dm():
    """The 4 dogfood failure modes are eliminated here:
    --to is required (not optional), payload is JSON-encoded from text,
    channel defaults to dm, no in-reply-to means no budget gate fires.
    """
    argv = dm_send.build_send_cmd(
        recipient_session_id="aa60cc21-target",
        text="hello",
    )
    assert "--channel" in argv and "dm" in argv
    # --to MUST appear with the recipient (forgot-to flag is fixed)
    to_idx = argv.index("--to")
    assert argv[to_idx + 1] == "aa60cc21-target"
    # --payload MUST be valid JSON with text key (no hand-rolled quotes)
    payload_idx = argv.index("--payload")
    payload = json.loads(argv[payload_idx + 1])
    assert payload == {"text": "hello"}
    # default: no --in-reply-to → no budget gate
    assert "--in-reply-to" not in argv
    # default: --no-envelope NOT set → envelope auto-issue at T1 (e-1290)
    assert "--no-envelope" not in argv


def test_build_send_cmd_rejects_missing_recipient():
    """Forgot-to-flag failure mode #2 from dogfood — surfaces as ValueError."""
    with pytest.raises(ValueError, match="recipient_session_id"):
        dm_send.build_send_cmd(recipient_session_id="", text="hello")


def test_build_send_cmd_rejects_non_string_text():
    with pytest.raises(TypeError):
        dm_send.build_send_cmd(
            recipient_session_id="x", text={"bad": "shape"},  # type: ignore[arg-type]
        )


def test_build_send_cmd_cross_project_adds_project_flag():
    """Failure mode #1 from dogfood: wrong project. Helper forces --project."""
    argv = dm_send.build_send_cmd(
        recipient_session_id="aa60",
        text="hi",
        project_id="other-project-id",
    )
    proj_idx = argv.index("--project")
    assert argv[proj_idx + 1] == "other-project-id"


def test_build_send_cmd_payload_handles_unicode_and_quotes():
    """Failure mode #4 from dogfood: hand-rolled JSON breaks on quotes/newlines."""
    text = 'こんにちは "world"\nline2'
    argv = dm_send.build_send_cmd(recipient_session_id="x", text=text)
    payload_idx = argv.index("--payload")
    payload = json.loads(argv[payload_idx + 1])
    assert payload["text"] == text  # round-trips exactly


def test_build_send_cmd_with_action():
    argv = dm_send.build_send_cmd(
        recipient_session_id="x",
        text="hi",
        actions=["foo.update", "bar.read"],
    )
    # Both actions must be present, repeated
    action_positions = [i for i, a in enumerate(argv) if a == "--action"]
    assert len(action_positions) == 2
    assert argv[action_positions[0] + 1] == "foo.update"
    assert argv[action_positions[1] + 1] == "bar.read"


def test_build_send_cmd_with_in_reply_to_flag():
    argv = dm_send.build_send_cmd(
        recipient_session_id="x",
        text="hi",
        in_reply_to="parent-event-id",
    )
    irt_idx = argv.index("--in-reply-to")
    assert argv[irt_idx + 1] == "parent-event-id"


def test_build_send_cmd_no_envelope_optional():
    argv = dm_send.build_send_cmd(
        recipient_session_id="x", text="hi", no_envelope=True,
    )
    assert "--no-envelope" in argv


def test_build_send_cmd_json_output_default_true():
    argv = dm_send.build_send_cmd(recipient_session_id="x", text="hi")
    assert "--json" in argv


# ---------------------------------------------------------------------------
# Helpers: cross-project detection
# ---------------------------------------------------------------------------


def test_cross_project_prompt_fires_when_ids_differ():
    assert dm_send.needs_cross_project_prompt(
        selected_session_project_id="other",
        cwd_project_id="mine",
    )


def test_cross_project_prompt_silent_when_ids_match():
    assert not dm_send.needs_cross_project_prompt(
        selected_session_project_id="mine",
        cwd_project_id="mine",
    )


def test_cross_project_prompt_silent_when_unknown():
    """If the directory entry didn't include the target's project, don't pester."""
    assert not dm_send.needs_cross_project_prompt(
        selected_session_project_id="",
        cwd_project_id="mine",
    )
    assert not dm_send.needs_cross_project_prompt(
        selected_session_project_id="other",
        cwd_project_id="",
    )


# ---------------------------------------------------------------------------
# Helpers: healthy-filter fallback
# ---------------------------------------------------------------------------


def test_fallback_directory_cmd_when_healthy_returns_empty():
    """0 candidates with --healthy → re-query without it (warn user separately)."""
    fb = dm_send.fallback_directory_cmd_if_no_healthy(
        sessions_with_healthy_filter=[],
        since_min=5,
    )
    assert fb is not None
    assert "--healthy" not in fb
    assert "--live" in fb


def test_no_fallback_when_healthy_has_results():
    fb = dm_send.fallback_directory_cmd_if_no_healthy(
        sessions_with_healthy_filter=[{"session_id": "x"}],
        since_min=5,
    )
    assert fb is None


def test_fallback_preserves_cross_project_id():
    fb = dm_send.fallback_directory_cmd_if_no_healthy(
        sessions_with_healthy_filter=[],
        since_min=5,
        project_id="other-proj",
    )
    assert fb is not None
    assert "--project" in fb
    assert "other-proj" in fb


# ---------------------------------------------------------------------------
# Helpers: argv quoting (round-trip for user confirmation prompts)
# ---------------------------------------------------------------------------


def test_quote_argv_handles_spaces_and_quotes():
    argv = ["beacon", "bus", "send", "--payload", '{"text":"a b c"}']
    quoted = dm_send.quote_argv(argv)
    # Shell-safe: contains single quotes around the JSON or is otherwise escaped
    assert "beacon bus send --payload" in quoted
    # The JSON payload must survive intact when re-parsed by a shell
    import shlex
    re_split = shlex.split(quoted)
    assert re_split == argv


# ---------------------------------------------------------------------------
# Local-mode pre-flight check (ms-54 e-1330)
# ---------------------------------------------------------------------------


def test_check_local_mode_error_returns_none_when_cloud_mode():
    """cloud-mode cwd (cloud.json present) → no error, DM proceeds."""
    assert dm_send.check_local_mode_error(cwd_has_cloud_json=True) is None


def test_check_local_mode_error_returns_friendly_text_when_local():
    """local-mode cwd → multi-line friendly explanation + how to switch."""
    msg = dm_send.check_local_mode_error(cwd_has_cloud_json=False)
    assert msg is not None
    # Mentions the prerequisite + the two recovery commands
    assert "cloud" in msg.lower() or "クラウド" in msg
    assert "beacon auth login" in msg
    assert "beacon cloud setup" in msg
    # Doesn't recommend things that would mislead a user
    assert "rm" not in msg


# ---------------------------------------------------------------------------
# Cross-project discovery (ms-54 e-1330 / dm_discover)
# ---------------------------------------------------------------------------


def test_dm_discover_module_importable():
    """The discover helper must import cleanly under the same package."""
    from beacon_cli.skills_helpers import dm_discover
    assert hasattr(dm_discover, "discover_and_aggregate")
    assert hasattr(dm_discover, "discover_local_project_ids")
    assert hasattr(dm_discover, "list_bus_bridge_processes")


def test_dm_discover_aggregate_signature():
    """discover_and_aggregate must accept healthy + since_min kwargs."""
    from beacon_cli.skills_helpers import dm_discover
    import inspect

    sig = inspect.signature(dm_discover.discover_and_aggregate)
    params = sig.parameters
    assert "healthy" in params
    assert "since_min" in params
