"""Win parity coverage for the ms-55 coordination CLIs (e-1735).

Mirrors tests/test_powershell_dispatch.py's strategy: patch
``subprocess.call`` inside beacon_cli.dispatch so we capture the
(cmd, env) that *would have* been forwarded to ``commands.py``. We
then assert each argv path produces the env contract that bin/beacon
already uses on macOS/Linux. The lib/* modules being pure Python is
what makes this enough — they run identically once the env reaches
commands.py.

Verbs covered (= the 6 the dispatch parity warning called out):
  * beacon stop scoped|global|status
  * beacon resume scoped|global
  * beacon rollback
  * beacon claim request|handoff|post|respond|release|list
  * beacon stuck check
  * beacon morning
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402


@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def test_stop_scoped_translates_target(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "stop", "scoped",
        "--target", "ms:ms-55",
        "--reason", "build fail",
        "--reason-kind", "build_fail",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STOP_TARGET_KIND"] == "ms"
    assert env["BEACON_STOP_TARGET_ID"] == "ms-55"
    assert env["BEACON_STOP_REASON"] == "build fail"
    assert env["BEACON_STOP_REASON_KIND"] == "build_fail"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "stop_scoped"


def test_stop_scoped_bare_target_rejected(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["stop", "scoped", "--target", "ms-55"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "kind" in err.lower()
    assert captured_call["cmd"] is None


def test_stop_global_no_target_keys(project_dir, no_bash, captured_call):
    rc = main_mod.main(["stop", "global", "--reason", "hostile"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STOP_REASON"] == "hostile"
    # global path doesn't carry target kind/id at all
    assert "BEACON_STOP_TARGET_KIND" not in env
    assert captured_call["cmd"][-1] == "stop_global"


def test_stop_status_minimal_env(project_dir, no_bash, captured_call):
    rc = main_mod.main(["stop", "status", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "stop_status"


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def test_resume_scoped(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "resume", "scoped",
        "--target", "session:sv-A",
        "--reason", "operator unstuck",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STOP_TARGET_KIND"] == "session"
    assert env["BEACON_STOP_TARGET_ID"] == "sv-A"
    assert env["BEACON_STOP_REASON"] == "operator unstuck"
    assert captured_call["cmd"][-1] == "resume_scoped"


def test_resume_global(project_dir, no_bash, captured_call):
    rc = main_mod.main(["resume", "global", "--reason", "all clear"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STOP_REASON"] == "all clear"
    assert captured_call["cmd"][-1] == "resume_global"


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def test_rollback_defaults(project_dir, no_bash, captured_call):
    rc = main_mod.main(["rollback"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ROLLBACK_COMMITS"] == "0"
    assert env["BEACON_ROLLBACK_DRY_RUN"] == ""
    assert env["BEACON_ROLLBACK_NO_RECORD"] == ""
    assert captured_call["cmd"][-1] == "rollback"


def test_rollback_full_flags(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "rollback",
        "--commits", "3",
        "--reason", "stop runaway",
        "--dry-run",
        "--no-record",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ROLLBACK_COMMITS"] == "3"
    assert env["BEACON_ROLLBACK_REASON"] == "stop runaway"
    assert env["BEACON_ROLLBACK_DRY_RUN"] == "1"
    assert env["BEACON_ROLLBACK_NO_RECORD"] == "1"
    assert env["BEACON_JSON"] == "1"


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub", ["request", "handoff", "post"])
def test_claim_issuance_translates(sub, project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "claim", sub,
        "--target", "task:e-1648",
        "--intent", "I'll take this",
        "--to", "sv-B",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_CLAIM_TARGET_KIND"] == "task"
    assert env["BEACON_CLAIM_TARGET_ID"] == "e-1648"
    assert env["BEACON_CLAIM_INTENT"] == "I'll take this"
    assert env["BEACON_CLAIM_TO"] == "sv-B"
    assert captured_call["cmd"][-1] == f"claim_{sub}"


def test_claim_respond_with_accept(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "claim", "respond", "cl-1", "--accept", "--reason", "got it",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_CLAIM_ID"] == "cl-1"
    assert env["BEACON_CLAIM_DECISION"] == "accept"
    assert env["BEACON_CLAIM_REASON"] == "got it"
    assert captured_call["cmd"][-1] == "claim_respond"


def test_claim_respond_with_decline(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "claim", "respond", "cl-2", "--decline", "--reason", "busy",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_CLAIM_DECISION"] == "decline"


def test_claim_respond_without_id_fails(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["claim", "respond", "--accept"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "claim_id" in err
    assert captured_call["cmd"] is None


def test_claim_release_with_outcome(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "claim", "release", "cl-3",
        "--outcome", "completed",
        "--reason", "merged PR",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_CLAIM_ID"] == "cl-3"
    assert env["BEACON_CLAIM_OUTCOME"] == "completed"
    assert captured_call["cmd"][-1] == "claim_release"


def test_claim_list_mine(project_dir, no_bash, captured_call):
    rc = main_mod.main(["claim", "list", "--mine", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_CLAIM_MINE"] == "1"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "claim_list"


def test_claim_list_alias_ls(project_dir, no_bash, captured_call):
    """`claim ls` is an alias for `claim list` — same env contract."""
    rc = main_mod.main(["claim", "ls", "--json"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "claim_list"


# ---------------------------------------------------------------------------
# stuck
# ---------------------------------------------------------------------------

def test_stuck_check_with_file(tmp_path, project_dir, no_bash, captured_call):
    telemetry = tmp_path / "tele.json"
    telemetry.write_text("[]")
    rc = main_mod.main([
        "stuck", "check",
        "--telemetry-file", str(telemetry),
        "--timeout-minutes", "15",
        "--dry-run",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STUCK_TELEMETRY_FILE"] == str(telemetry)
    assert env["BEACON_STUCK_TIMEOUT_MINUTES"] == "15"
    assert env["BEACON_STUCK_DRY_RUN"] == "1"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "stuck_check"


def test_stuck_check_default_timeout(project_dir, no_bash, captured_call):
    """No --timeout-minutes → default 30 (matches bash)."""
    rc = main_mod.main([
        "stuck", "check", "--telemetry-inline", "[]",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_STUCK_TIMEOUT_MINUTES"] == "30"


# ---------------------------------------------------------------------------
# morning
# ---------------------------------------------------------------------------

def test_morning_defaults(project_dir, no_bash, captured_call):
    rc = main_mod.main(["morning"])
    assert rc == 0
    env = captured_call["env"]
    # bash default is 12; dispatch follows the same fallback.
    assert env["BEACON_MORNING_SINCE_HOURS"] == "12"
    assert env["BEACON_MORNING_NO_DOC"] == ""
    assert captured_call["cmd"][-1] == "morning"


def test_morning_no_doc_and_events_file(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "morning",
        "--since-hours", "6",
        "--events-file", "/tmp/ev.json",
        "--no-doc",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MORNING_SINCE_HOURS"] == "6"
    assert env["BEACON_MORNING_EVENTS_FILE"] == "/tmp/ev.json"
    assert env["BEACON_MORNING_NO_DOC"] == "1"
    assert env["BEACON_JSON"] == "1"


# ---------------------------------------------------------------------------
# Handler registration — pin the dict so a future rename doesn't silently
# break Windows parity.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "verb", ["stop", "resume", "rollback", "claim", "stuck", "morning"],
)
def test_handler_registered(verb: str) -> None:
    assert verb in dispatch._HANDLERS, (
        f"beacon {verb!r} missing from _HANDLERS — "
        "Windows pipx users will see `argparse invalid choice`"
    )
