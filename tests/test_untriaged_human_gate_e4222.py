"""ms-126 / e-4222: close the human bypass of the mandatory-priority gate.

Background (ms-126): priority is mandatory on the human entry path
(`beacon task add` / `beacon milestone add`) — an empty priority is rejected so
a person actually picks one of the 5 severities. Machine call sites that have
genuinely not judged a priority (issue import / review-derived / roadmap bulk /
dispatch-derived) opt into the ``untriaged`` sentinel instead, so the missing
judgement becomes *visible debt* rather than a silent empty field.

The hole this task closes: ``--untriaged`` (→ ``BEACON_ALLOW_UNTRIAGED=1``) was
reachable on the HUMAN CLI path too, so a person could type
``beacon task add "…" --untriaged`` (or export ``BEACON_ALLOW_UNTRIAGED=1``) and
skip the forcing function — a hole straight through the human=mandatory line.

Design (方針 A, structural gate): reuse the established ``BEACON_SESSION_KIND``
actor convention.
  * A HUMAN session (``BEACON_SESSION_KIND=human``) that passes ``--untriaged``
    is REFUSED — the human owns the priority judgement and must pick one of 5.
  * A machine / AI session (default, unset / "ai") keeps the untriaged
    capability, so the four named machine paths (and the in-process core call
    sites) are NOT regressed.

These tests pin the gate at the ``cmd_*`` (env-var reading) layer, where the
human ``--untriaged`` flag lands, and confirm the ``core`` machine capability is
untouched.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import core  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_project(path):
    (path / ".beacon").mkdir(parents=True, exist_ok=True)
    data = {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1",
                "title": "Sample",
                "status": "in_progress",
                "progress": 0,
                "target_date": "",
                "entries": [],
                "commits": [],
            }
        ],
        "operations": [],
    }
    (path / ".beacon" / "project.json").write_text(json.dumps(data))


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Clean slate: neither the untriaged opt-in nor a session-kind is set unless
    # a test sets it. Point BEACON_HOME at a clean tmp so author resolution does
    # not read the developer's real creds.
    for var in (
        "BEACON_ALLOW_UNTRIAGED",
        "BEACON_SESSION_KIND",
        "BEACON_USER_ID",
        "BEACON_USER_EMAIL",
        "BEACON_DISPLAY_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BEACON_HOME", str(tmp_path / ".beacon_home"))
    return tmp_path


def _load(pd):
    return json.loads((pd / ".beacon" / "project.json").read_text())


# ---------------------------------------------------------------------------
# Unit: the actor-based gate helper
# ---------------------------------------------------------------------------


class TestHumanUntriagedBypassRefusedHelper:
    def test_human_plus_untriaged_is_refused(self, monkeypatch):
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        assert commands._human_untriaged_bypass_refused() is True

    def test_machine_session_untriaged_is_allowed(self, monkeypatch):
        # Default (AI / unset session) — untriaged capability preserved.
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        assert commands._human_untriaged_bypass_refused() is False

    def test_explicit_ai_session_untriaged_is_allowed(self, monkeypatch):
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_SESSION_KIND", "ai")
        assert commands._human_untriaged_bypass_refused() is False

    def test_human_without_untriaged_is_not_refused(self, monkeypatch):
        # A human NOT asking for untriaged is fine here — they'll be forced to
        # pick a priority by the existing core forcing function, not by this gate.
        monkeypatch.delenv("BEACON_ALLOW_UNTRIAGED", raising=False)
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        assert commands._human_untriaged_bypass_refused() is False

    def test_human_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_SESSION_KIND", "Human")
        assert commands._human_untriaged_bypass_refused() is True

    # ms-126 philosophy fix (2026-07-29): undeclared sessions are inferred from
    # the terminal — an interactive stdin is a human typing (refuse the bypass),
    # a non-interactive stdin is a machine/pipe/CI (allow).
    def _set_isatty(self, monkeypatch, value):
        import types
        monkeypatch.setattr(commands.sys, "stdin",
                            types.SimpleNamespace(isatty=lambda: value))

    def test_undeclared_interactive_terminal_is_refused(self, monkeypatch):
        # The exact hole the fix closes: a real human at a bare terminal who has
        # NOT exported BEACON_SESSION_KIND=human. Interactive stdin → human → refuse.
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        self._set_isatty(monkeypatch, True)
        assert commands._human_untriaged_bypass_refused() is True

    def test_undeclared_non_interactive_is_allowed(self, monkeypatch):
        # AI tool / pipe / CI: no tty on stdin → machine → allowed (unchanged).
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        self._set_isatty(monkeypatch, False)
        assert commands._human_untriaged_bypass_refused() is False

    def test_explicit_ai_overrides_interactive_tty(self, monkeypatch):
        # An AI that happens to run in a PTY can still declare itself and keep
        # the untriaged capability — explicit kind wins over the tty inference.
        monkeypatch.setenv("BEACON_SESSION_KIND", "ai")
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        self._set_isatty(monkeypatch, True)
        assert commands._human_untriaged_bypass_refused() is False

    def test_no_bypass_attempt_is_never_refused_even_on_tty(self, monkeypatch):
        # Without --untriaged there is nothing to refuse; the tty inference must
        # not fire on a plain interactive command (guards against over-blocking).
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        monkeypatch.delenv("BEACON_ALLOW_UNTRIAGED", raising=False)
        self._set_isatty(monkeypatch, True)
        assert commands._human_untriaged_bypass_refused() is False


# ---------------------------------------------------------------------------
# cmd_task_add — human --untriaged rejected, machine --untriaged continues
# ---------------------------------------------------------------------------


class TestCmdTaskAddHumanGate:
    def test_human_untriaged_rejected(self, project_dir, monkeypatch, capsys):
        # AC: a human session cannot bypass the priority forcing function via
        # --untriaged. It exits non-zero, prints the machine-only error, and
        # writes NO task.
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_DESCRIPTION", "sneaky task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        with pytest.raises(SystemExit) as exc:
            commands.cmd_task_add()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "machine-only" in err
        assert "Priority is required" in err
        # No task appended.
        assert _load(project_dir)["milestones"][0]["entries"] == []

    def test_human_export_of_env_also_rejected(
        self, project_dir, monkeypatch, capsys
    ):
        # The gate must fire whether untriaged came from the --untriaged flag or
        # from a human manually exporting BEACON_ALLOW_UNTRIAGED=1 — both land as
        # the same env var, both are refused on a human session.
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_DESCRIPTION", "env-export task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        with pytest.raises(SystemExit) as exc:
            commands.cmd_task_add()
        assert exc.value.code == 1
        assert _load(project_dir)["milestones"][0]["entries"] == []

    def test_machine_untriaged_still_creates_task(
        self, project_dir, monkeypatch
    ):
        # Regression guard: the machine / AI path (session-kind unset) keeps the
        # untriaged capability — the task is created with the untriaged sentinel.
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_DESCRIPTION", "machine task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        commands.cmd_task_add()
        entries = _load(project_dir)["milestones"][0]["entries"]
        assert len(entries) == 1
        assert entries[0]["meta"]["priority"] == core.UNTRIAGED_PRIORITY

    def test_human_with_real_priority_still_works(
        self, project_dir, monkeypatch
    ):
        # The gate must not touch the normal human path: a human picking a real
        # priority (no untriaged) creates the task as before.
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.setenv("BEACON_DESCRIPTION", "real task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        monkeypatch.setenv("BEACON_PRIORITY", "high")
        commands.cmd_task_add()
        entries = _load(project_dir)["milestones"][0]["entries"]
        assert len(entries) == 1
        assert entries[0]["meta"]["priority"] == "high"


# ---------------------------------------------------------------------------
# cmd_milestone_add — same gate on the milestone entry path
# ---------------------------------------------------------------------------


class TestCmdMilestoneAddHumanGate:
    def test_human_untriaged_rejected(self, project_dir, monkeypatch, capsys):
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_TITLE", "sneaky MS")
        before = len(_load(project_dir)["milestones"])
        with pytest.raises(SystemExit) as exc:
            commands.cmd_milestone_add()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "machine-only" in err
        assert "Priority is required" in err
        # No milestone appended.
        assert len(_load(project_dir)["milestones"]) == before

    def test_machine_untriaged_still_creates_milestone(
        self, project_dir, monkeypatch
    ):
        monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
        monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
        monkeypatch.setenv("BEACON_TITLE", "machine MS")
        commands.cmd_milestone_add()
        ms = next(
            m for m in _load(project_dir)["milestones"] if m["title"] == "machine MS"
        )
        assert ms["priority"] == core.UNTRIAGED_PRIORITY

    def test_human_with_real_priority_still_works(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.setenv("BEACON_TITLE", "real MS")
        monkeypatch.setenv("BEACON_PRIORITY", "medium")
        commands.cmd_milestone_add()
        ms = next(
            m for m in _load(project_dir)["milestones"] if m["title"] == "real MS"
        )
        assert ms["priority"] == "medium"


# ---------------------------------------------------------------------------
# core layer: the machine capability itself is untouched (in-process call sites)
# ---------------------------------------------------------------------------


class TestCoreMachineCapabilityUntouched:
    def test_core_task_add_untriaged_unaffected_by_session_kind(
        self, monkeypatch
    ):
        # Even under BEACON_SESSION_KIND=human, the CORE call site (used by the
        # server / issue_import etc.) still honours allow_untriaged=True — the
        # gate lives at the cmd_* boundary, never inside core, so in-process
        # machine call sites are never regressed.
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        data = {
            "name": "t",
            "milestones": [
                {"id": "ms-1", "status": "in_progress", "entries": []}
            ],
        }
        core.task_add(data, "ms-1", "T", allow_untriaged=True)
        meta = data["milestones"][0]["entries"][0]["meta"]
        assert meta["priority"] == core.UNTRIAGED_PRIORITY

    def test_core_issue_import_untriaged_unaffected(self, monkeypatch):
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        data = {
            "name": "t",
            "milestones": [
                {"id": "ms-1", "status": "in_progress", "entries": []}
            ],
        }
        core.issue_import(
            data, ms_id="ms-1", number=7, url="http://x",
            title="Fix", body="b",
        )
        entry = data["milestones"][0]["entries"][0]
        assert entry["meta"]["priority"] == core.UNTRIAGED_PRIORITY
