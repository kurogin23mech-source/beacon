"""--reason gating on done / observe transition verbs (e-976).

Background:
  Before this change `beacon task done` / `beacon milestone done` already
  refused to run with an empty reason, but they couldn't tell the
  difference between "operator forgot the flag" and "operator deliberately
  recorded no reason." The new contract (e-976):

    1. ``--reason`` omitted entirely (env var not set) → refuse, exit 1.
    2. ``--reason ""`` passed (env var set to empty string) → accept,
       record an empty reason. Discouraged but explicit waiver.
    3. ``--reason "<text>"`` → accept, record the text.

  These tests exercise the python entrypoints directly (commands.py),
  manipulating BEACON_REASON the same way bin/beacon does, so the gate
  itself is unit-tested without spawning bash.

Scope note:
  ``beacon milestone restore`` / ``beacon task restore`` aren't implemented
  yet — they're listed in the e-976 acceptance criteria but no handler
  exists. They're tracked as follow-up; no test here so we don't lock in
  a non-existent API.

  ``cmd_milestone_delete`` already had a required-reason gate before
  this change. The "delete unchanged" regression test below pins that
  to make sure refactoring the new helper didn't leak into the old path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def project_dir(monkeypatch):
    """Per-test temp project with BEACON_PROJECT_FILE pointed at it."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "test", "milestones": []}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        # ms-95 e-2438: monkeypatch.delitem auto-restores so downstream tests'
        # firestore_client mocks survive. Raw pop leaks the deletion across the
        # test boundary → CI DefaultCredentialsError for invitation/trash/etc.
        if "firestore_client" in sys.modules:
            monkeypatch.delitem(sys.modules, "firestore_client")
        yield Path(tmp)


def _write_project(project_dir: Path, data: dict) -> None:
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _read_project(project_dir: Path) -> dict:
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8")
    )


def _ms_with_task(ms_id: str, task_id: str, status: str = "in_progress") -> dict:
    """Build a minimal milestone dict with one open task, ready for task done."""
    return {
        "id": ms_id,
        "title": f"MS {ms_id}",
        "status": status,
        "target_date": "",
        "commits": [],
        "entries": [
            {
                "id": task_id,
                "type": "task",
                "description": "do the thing",
                "status": "todo",
                "created_at": "2026-06-01",
            }
        ],
    }


# ---------------------------------------------------------------------------
# task done
# ---------------------------------------------------------------------------


class TestTaskDoneReason:
    def test_unset_env_refuses(self, project_dir, monkeypatch, capsys):
        """BEACON_REASON not in environ → refuse, exit 1 (operator forgot flag)."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [_ms_with_task("ms-1", "e-1")],
        })
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.setenv("BEACON_PROGRESS", "")
        monkeypatch.delenv("BEACON_REASON", raising=False)  # the critical bit

        import commands  # type: ignore
        with pytest.raises(SystemExit) as ei:
            commands.cmd_task_done()
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "--reason" in err
        assert "task done" in err
        # The task must remain todo — refusal cannot mutate state.
        data = _read_project(project_dir)
        assert data["milestones"][0]["entries"][0]["status"] == "todo"

    def test_explicit_empty_rejected(self, project_dir, monkeypatch, capsys):
        """BEACON_REASON='' → refuse (ms-120 e-3906 option B: empty is ambiguous —
        deliberate waiver vs. padding to pass the gate — so it is no longer a
        silent waiver; use --acknowledge to waive on purpose)."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [_ms_with_task("ms-1", "e-2")],
        })
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-2")
        monkeypatch.setenv("BEACON_PROGRESS", "")
        monkeypatch.setenv("BEACON_REASON", "")
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit):
            commands.cmd_task_done()

        data = _read_project(project_dir)
        assert data["milestones"][0]["entries"][0]["status"] != "done"

    def test_acknowledge_waives_and_is_recorded(self, project_dir, monkeypatch):
        """BEACON_ACKNOWLEDGE=1 → accept, and record the explicit-waiver sentinel
        so the trail shows a *deliberate* no-reason (better than the old empty)."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [_ms_with_task("ms-1", "e-2")],
        })
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-2")
        monkeypatch.setenv("BEACON_PROGRESS", "")
        monkeypatch.delenv("BEACON_REASON", raising=False)
        monkeypatch.setenv("BEACON_ACKNOWLEDGE", "1")

        import commands  # type: ignore
        commands.cmd_task_done()  # must not raise

        data = _read_project(project_dir)
        entry = data["milestones"][0]["entries"][0]
        assert entry["status"] == "done"
        assert "acknowledged" in entry["meta"].get("done_reason", "").lower()

    def test_non_empty_records_reason(self, project_dir, monkeypatch, capsys):
        """BEACON_REASON='actual text' → accept and persist the text."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [_ms_with_task("ms-1", "e-3")],
        })
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-3")
        monkeypatch.setenv("BEACON_PROGRESS", "")
        monkeypatch.setenv("BEACON_REASON", "AC1/2 satisfied, see PR #100")

        import commands  # type: ignore
        commands.cmd_task_done()

        data = _read_project(project_dir)
        entry = data["milestones"][0]["entries"][0]
        assert entry["status"] == "done"
        assert entry["meta"]["done_reason"] == "AC1/2 satisfied, see PR #100"


# ---------------------------------------------------------------------------
# milestone done
# ---------------------------------------------------------------------------


class TestMilestoneDoneReason:
    def test_unset_env_refuses(self, project_dir, monkeypatch, capsys):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-2", "title": "x", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-2")
        monkeypatch.delenv("BEACON_REASON", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit) as ei:
            commands.cmd_milestone_done()
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "--reason" in err
        assert "milestone done" in err
        # Status must remain unchanged.
        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_explicit_empty_rejected(self, project_dir, monkeypatch):
        """ms-120 e-3906 option B: empty --reason is refused, not a silent waiver."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-3", "title": "y", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-3")
        monkeypatch.setenv("BEACON_REASON", "")
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit):
            commands.cmd_milestone_done()

        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_acknowledge_waives(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-3", "title": "y", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-3")
        monkeypatch.delenv("BEACON_REASON", raising=False)
        monkeypatch.setenv("BEACON_ACKNOWLEDGE", "1")

        import commands  # type: ignore
        commands.cmd_milestone_done()  # must not raise

        ms = _read_project(project_dir)["milestones"][0]
        assert ms["status"] == "done"
        assert "acknowledged" in ms["meta"].get("done_reason", "").lower()

    def test_non_empty_records_reason(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-4", "title": "z", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-4")
        monkeypatch.setenv("BEACON_REASON", "shipped via PR #42")

        import commands  # type: ignore
        commands.cmd_milestone_done()

        ms = _read_project(project_dir)["milestones"][0]
        assert ms["status"] == "done"
        assert ms["meta"]["done_reason"] == "shipped via PR #42"


# ---------------------------------------------------------------------------
# milestone observe (new dedicated handler, e-976)
# ---------------------------------------------------------------------------


class TestMilestoneObserveReason:
    def test_unset_env_refuses(self, project_dir, monkeypatch, capsys):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-5", "title": "p", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-5")
        monkeypatch.delenv("BEACON_REASON", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit) as ei:
            commands.cmd_milestone_observe()
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "--reason" in err
        assert "milestone observe" in err
        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_explicit_empty_rejected(self, project_dir, monkeypatch):
        """ms-120 e-3906 option B: empty --reason is refused, not a silent waiver."""
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-6", "title": "q", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-6")
        monkeypatch.setenv("BEACON_REASON", "")
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit):
            commands.cmd_milestone_observe()

        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_acknowledge_waives(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-6", "title": "q", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-6")
        monkeypatch.delenv("BEACON_REASON", raising=False)
        monkeypatch.setenv("BEACON_ACKNOWLEDGE", "1")

        import commands  # type: ignore
        commands.cmd_milestone_observe()

        ms = _read_project(project_dir)["milestones"][0]
        assert ms["status"] == "observing"

    def test_non_empty_records_reason(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-7", "title": "r", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-7")
        monkeypatch.setenv("BEACON_REASON", "paused for upstream API freeze")

        import commands  # type: ignore
        commands.cmd_milestone_observe()

        ms = _read_project(project_dir)["milestones"][0]
        assert ms["status"] == "observing"
        assert ms["meta"]["observing_reason"] == "paused for upstream API freeze"


# ---------------------------------------------------------------------------
# ms-119: observing is a completion claim → same e-4008 gate parity as done.
# An AI session's bare `milestone observe` must be refused (抜け穴を塞ぐ);
# --review routes to the human approval gate instead of landing directly.
# ---------------------------------------------------------------------------


class TestMilestoneObserveCompletionGate:
    def test_ai_session_bare_observe_refused(self, project_dir, monkeypatch, capsys):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-5", "title": "p", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-5")
        monkeypatch.setenv("BEACON_REASON", "運用に回す")
        # AI session (not human) and no explicit override → ban must fire.
        monkeypatch.setenv("BEACON_SESSION_KIND", "claude")
        monkeypatch.delenv("BEACON_TARGET_COMPLETE_USER_OVERRIDE", raising=False)
        monkeypatch.delenv("BEACON_REVIEW", raising=False)

        import commands  # type: ignore
        with pytest.raises(SystemExit) as ei:
            commands.cmd_milestone_observe()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "e-4008" in err
        # status must NOT have changed (bare observe blocked).
        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_user_override_allows_direct_observe(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-5", "title": "p", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-5")
        monkeypatch.setenv("BEACON_REASON", "運用に回す")
        monkeypatch.setenv("BEACON_SESSION_KIND", "claude")
        monkeypatch.setenv("BEACON_TARGET_COMPLETE_USER_OVERRIDE", "1")

        import commands  # type: ignore
        commands.cmd_milestone_observe()
        assert _read_project(project_dir)["milestones"][0]["status"] == "observing"

    def test_review_routes_to_approval_gate_not_direct(self, project_dir, monkeypatch, capsys):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-5", "title": "p", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-5")
        monkeypatch.setenv("BEACON_REASON", "運用に回す")
        monkeypatch.setenv("BEACON_SESSION_KIND", "claude")
        monkeypatch.setenv("BEACON_REVIEW", "1")

        import commands  # type: ignore
        commands.cmd_milestone_observe()
        # --review creates a pending approval; it does NOT land observing directly.
        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"
        out = capsys.readouterr().out
        assert "承認待ち" in out


# ---------------------------------------------------------------------------
# regression: milestone delete still gates (pre-existing behavior)
# ---------------------------------------------------------------------------


class TestMilestoneDeleteUnchanged:
    """``milestone delete`` already required a reason before e-976 and uses
    its own (older) gate, not the new helper. Pin its behavior so the
    helper extraction didn't drift it.
    """

    def test_empty_reason_still_refuses(self, project_dir, monkeypatch, capsys):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-8", "title": "s", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-8")
        monkeypatch.setenv("BEACON_REASON", "")

        import commands  # type: ignore
        with pytest.raises(SystemExit) as ei:
            commands.cmd_milestone_delete()
        assert ei.value.code == 1
        out = capsys.readouterr().out + capsys.readouterr().err
        # Delete uses a slightly different (older) wording; key thing is
        # the gate still fires and status doesn't change.
        assert _read_project(project_dir)["milestones"][0]["status"] == "in_progress"

    def test_non_empty_proceeds(self, project_dir, monkeypatch):
        _write_project(project_dir, {
            "name": "t",
            "milestones": [{
                "id": "ms-9", "title": "t", "status": "in_progress",
                "target_date": "", "commits": [], "entries": [],
            }],
        })
        monkeypatch.setenv("BEACON_MS_ID", "ms-9")
        monkeypatch.setenv("BEACON_REASON", "scope-out: merged into ms-10")

        import commands  # type: ignore
        commands.cmd_milestone_delete()

        ms = _read_project(project_dir)["milestones"][0]
        assert ms["status"] == "cancelled"
