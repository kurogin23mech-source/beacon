"""Unit coverage for the DM/trek detector consolidation (ms-85 e-3180).

e-3180 merged four session-start detectors into two single-pass scripts:

  * Step 1n + 1n-2 -> scripts/session-start-dm-inbox.py
    (one auth resolution, both DM sections rendered)
  * Step 1o + 1o-2 -> scripts/session-start-trek-status.py + lib/trek_status
    (one trek list fetch; armed CLIs only when an active trek is joined)

The refactor's contract is **display unchanged + local-mode silent skip**.
These tests pin the pure trek_status logic, the DM section ordering, and the
local-mode / no-joined-trek early returns.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))


def _load_script(name: str):
    path = REPO / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# lib/trek_status: format_joined_treks / has_active_trek / is_armed
# ---------------------------------------------------------------------------

import trek_status as ts  # noqa: E402


def test_trek_empty_collapses():
    assert ts.format_joined_treks([]) == ""
    assert ts.format_joined_treks(None) == ""
    assert ts.has_active_trek([]) is False


def test_trek_format_all_fields():
    treks = [{
        "trek_id": "trek-1", "title": "Build X", "status": "active",
        "halt": None, "goal_state": "ship v1",
        "members": [{"user_id": "me"}, {"user_id": "a"}, {"user_id": "b"}],
    }]
    out = ts.format_joined_treks(treks)
    assert out.splitlines() == [
        "[trek-1] Build X",
        "  status: active",
        "  目標: ship v1",
        "  他 2 名",
    ]


def test_trek_format_halted_and_no_goal():
    treks = [{
        "trek_id": "trek-2", "title": "Halted", "status": "active",
        "halt": {"reason": "blocked on API"}, "goal_state": "",
        "members": [{"user_id": "me"}],
    }]
    out = ts.format_joined_treks(treks)
    assert "  ⚠ HALTED: blocked on API" in out
    assert "目標:" not in out          # empty goal omitted
    assert "  他 0 名" in out          # sole member -> 0 others


def test_has_active_trek():
    assert ts.has_active_trek([{"status": "planning"}]) is False
    assert ts.has_active_trek([{"status": "planning"}, {"status": "active"}]) is True


def test_is_armed_true_when_trek_channel_and_budget():
    assert ts.is_armed({"channels": ["trek-trigger"]}, {"total": 5, "used": 1}) is True


def test_is_armed_false_without_trek_channel():
    assert ts.is_armed({"channels": ["dm"]}, {"total": 5, "used": 1}) is False


def test_is_armed_false_when_budget_exhausted():
    assert ts.is_armed({"channels": ["trek-task-review"]}, {"total": 5, "used": 5}) is False


def test_is_armed_false_on_missing_payloads():
    assert ts.is_armed(None, None) is False
    assert ts.is_armed({"channels": ["trek-trigger"]}, None) is False


def test_is_armed_accepts_bare_channel_list():
    assert ts.is_armed(["trek-progress-check"], {"total": 3, "used": 0}) is True


# ---------------------------------------------------------------------------
# scripts/session-start-trek-status.py: gating + section assembly
# ---------------------------------------------------------------------------

TREK_SCRIPT = _load_script("session-start-trek-status.py")


def test_trek_script_no_joined_treks_silent(monkeypatch, capsys):
    monkeypatch.setattr(TREK_SCRIPT, "_cli_json", lambda args: [])
    assert TREK_SCRIPT.main() == 0
    assert capsys.readouterr().out == ""


def test_trek_script_active_armed_shows_list_and_reminder_only(monkeypatch, capsys):
    calls = {"count": 0}

    def fake_cli(args):
        calls["count"] += 1
        if args[:3] == ["beacon", "trek", "list"]:
            return [{"trek_id": "t1", "title": "T", "status": "active",
                     "halt": None, "goal_state": "", "members": [{"user_id": "me"}]}]
        if args[:3] == ["beacon", "bus", "auto-execute"]:
            return {"channels": ["trek-trigger"]}
        if args[:3] == ["beacon", "bus", "budget"]:
            return {"total": 5, "used": 0}
        return None

    monkeypatch.setattr(TREK_SCRIPT, "_cli_json", fake_cli)
    assert TREK_SCRIPT.main() == 0
    out = capsys.readouterr().out
    assert "[t1] T" in out
    assert ts.BLANKET_APPROVAL_REMINDER in out
    assert ts.NOT_ARMED_WARNING not in out  # armed -> no warning


def test_trek_script_active_not_armed_adds_warning(monkeypatch, capsys):
    def fake_cli(args):
        if args[:3] == ["beacon", "trek", "list"]:
            return [{"trek_id": "t1", "title": "T", "status": "active",
                     "halt": None, "goal_state": "", "members": [{"user_id": "me"}]}]
        if args[:3] == ["beacon", "bus", "auto-execute"]:
            return {"channels": []}  # not armed
        if args[:3] == ["beacon", "bus", "budget"]:
            return {"total": 0, "used": 0}
        return None

    monkeypatch.setattr(TREK_SCRIPT, "_cli_json", fake_cli)
    assert TREK_SCRIPT.main() == 0
    out = capsys.readouterr().out
    assert ts.NOT_ARMED_WARNING in out


def test_trek_script_planning_only_skips_armed_check(monkeypatch):
    """Planning-only join -> no blanket reminder, no armed CLIs invoked."""
    invoked = []

    def fake_cli(args):
        invoked.append(args[:3])
        if args[:3] == ["beacon", "trek", "list"]:
            return [{"trek_id": "t1", "title": "P", "status": "planning",
                     "halt": None, "goal_state": "", "members": [{"user_id": "me"}]}]
        return None

    monkeypatch.setattr(TREK_SCRIPT, "_cli_json", fake_cli)
    TREK_SCRIPT.main()
    # Only the trek list is fetched; armed CLIs are gated behind active trek.
    assert ["beacon", "bus", "auto-execute"] not in invoked
    assert ["beacon", "bus", "budget"] not in invoked


# ---------------------------------------------------------------------------
# scripts/session-start-dm-inbox.py: local-mode skip + section ordering
# ---------------------------------------------------------------------------

DM_SCRIPT = _load_script("session-start-dm-inbox.py")


def test_dm_inbox_local_mode_silent(monkeypatch, capsys):
    monkeypatch.setattr(DM_SCRIPT, "_resolve_context", lambda: ("", "", "", "base"))
    assert DM_SCRIPT.main() == 0
    assert capsys.readouterr().out == ""


def test_dm_inbox_orders_pending_then_catchup(monkeypatch, capsys):
    monkeypatch.setattr(
        DM_SCRIPT, "_resolve_context", lambda: ("proj", "u1", "tok", "base")
    )
    monkeypatch.setattr(DM_SCRIPT, "_pending_action_section",
                        lambda *a: "保留中 DM action: 1 件")
    monkeypatch.setattr(DM_SCRIPT, "_catchup_section",
                        lambda *a: "留守中に届いた DM (user-scoped catch-up):")
    assert DM_SCRIPT.main() == 0
    out = capsys.readouterr().out
    assert out.index("保留中 DM action") < out.index("留守中に届いた DM")


def test_dm_inbox_omits_empty_sections(monkeypatch, capsys):
    monkeypatch.setattr(
        DM_SCRIPT, "_resolve_context", lambda: ("proj", "u1", "tok", "base")
    )
    monkeypatch.setattr(DM_SCRIPT, "_pending_action_section", lambda *a: "")
    monkeypatch.setattr(DM_SCRIPT, "_catchup_section", lambda *a: "only catchup")
    assert DM_SCRIPT.main() == 0
    assert capsys.readouterr().out.strip() == "only catchup"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
