"""Win parity coverage for the ms-73 session CLI mirror (e-1762).

Mirrors tests/test_ms55_dispatch.py's strategy: patch
``subprocess.call`` inside beacon_cli.dispatch so we capture the
(cmd, env) that *would have* been forwarded to ``commands.py``. We
then assert each argv path produces the env contract that bin/beacon
already uses on macOS/Linux. lib/* (session, session_log) is pure
Python, so once the env reaches commands.py the underlying logic
runs identically on Windows pipx.

Verbs covered:
  * beacon session id           (already in ms-44 e-1171)
  * beacon session focus        (already in ms-54 e-1369)
  * beacon session attention    (already in ms-54 e-1369)
  * beacon session end          (NEW in e-1762)
  * beacon session rescue       (NEW in e-1762)
  * beacon session fork         (NEW in e-1762)
  * beacon session fork list    (NEW in e-1762)
  * beacon session log list     (NEW in e-1762)
  * beacon session log show     (NEW in e-1762)

The first three are smoke-tested for non-regression; the new ones get
full argv → env contract assertions.
"""

from __future__ import annotations

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
# Non-regression: pre-existing session verbs still work after argparse
# tree was extended with end/rescue/fork/log subparsers (e-1762).
# ---------------------------------------------------------------------------


def test_session_id_still_dispatches(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "id"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "session_id"


def test_session_focus_still_dispatches(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "focus", "implementing ms-73", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SESSION_FOCUS_TEXT"] == "implementing ms-73"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "session_focus"


def test_session_attention_still_dispatches(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "attention", "--set", "true"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SESSION_ATTENTION_SET"] == "true"
    assert captured_call["cmd"][-1] == "session_attention"


# ---------------------------------------------------------------------------
# session end — graceful close path (e-1762)
# ---------------------------------------------------------------------------


def test_session_end_passes_summary_and_session_id(
    project_dir, no_bash, captured_call
):
    rc = main_mod.main([
        "session", "end",
        "--summary", "shipped ms-73 mirror",
        "--session-id", "sv-test-1234",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SUMMARY"] == "shipped ms-73 mirror"
    assert env["BEACON_SESSION_ID"] == "sv-test-1234"
    assert env["BEACON_JSON"] == "1"
    assert env["BEACON_BUS_ORIGIN"] == ""
    assert captured_call["cmd"][-1] == "session_end"


def test_session_end_bus_origin_flag(project_dir, no_bash, captured_call):
    """`--bus-origin` becomes BEACON_BUS_ORIGIN=1, the poisoning-defense
    knob the bash side wires to commands.py (ms-54 e-1293).
    """
    rc = main_mod.main(["session", "end", "--bus-origin"])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_ORIGIN"] == "1"


def test_session_end_no_args_defaults(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "end"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SUMMARY"] == ""
    assert env["BEACON_SESSION_ID"] == ""
    assert env["BEACON_JSON"] == ""


# ---------------------------------------------------------------------------
# session rescue — recover dead siblings (e-1762)
# ---------------------------------------------------------------------------


def test_session_rescue_dispatches(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "rescue", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "session_rescue"


def test_session_rescue_no_json(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "rescue"])
    assert rc == 0
    assert captured_call["env"]["BEACON_JSON"] == ""


# ---------------------------------------------------------------------------
# session fork — spawn sibling worktree (e-1762, ms-67)
# ---------------------------------------------------------------------------


def test_session_fork_target_ms(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "fork", "ms-73", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SESSION_FORK_MS_ID"] == "ms-73"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "session_fork"


def test_session_fork_list_enumerates(project_dir, no_bash, captured_call):
    """`session fork list` must hit session_fork_list, not session_fork,
    so /beacon-session-merge-back's picker gets the same data on Windows.
    """
    rc = main_mod.main(["session", "fork", "list", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "session_fork_list"


def test_session_fork_missing_arg_rejected(
    project_dir, no_bash, captured_call, capsys
):
    rc = main_mod.main(["session", "fork"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "session fork" in err
    assert captured_call["cmd"] is None


# ---------------------------------------------------------------------------
# session log list / show — used by /beacon-session-start (e-1762)
# ---------------------------------------------------------------------------


def test_session_log_list_default_limit(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "log", "list"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_LIMIT"] == "0"
    assert env["BEACON_JSON"] == ""
    assert captured_call["cmd"][-1] == "session_log_list"


def test_session_log_list_with_limit_and_json(
    project_dir, no_bash, captured_call
):
    rc = main_mod.main(["session", "log", "list", "--limit", "5", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_LIMIT"] == "5"
    assert env["BEACON_JSON"] == "1"


def test_session_log_ls_alias_works(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "log", "ls", "--json"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "session_log_list"


def test_session_log_show_requires_id(
    project_dir, no_bash, captured_call, capsys
):
    rc = main_mod.main(["session", "log", "show"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "session-id" in err.lower() or "session id" in err.lower()
    assert captured_call["cmd"] is None


def test_session_log_show_dispatches(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "log", "show", "sv-abc-1234", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SESSION_ID"] == "sv-abc-1234"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "session_log_show"


def test_session_log_get_alias_works(project_dir, no_bash, captured_call):
    rc = main_mod.main(["session", "log", "get", "sv-xyz"])
    assert rc == 0
    assert captured_call["env"]["BEACON_SESSION_ID"] == "sv-xyz"
    assert captured_call["cmd"][-1] == "session_log_show"


# ---------------------------------------------------------------------------
# Help / unknown-subcommand paths
# ---------------------------------------------------------------------------


def test_session_help_lists_new_verbs(project_dir, no_bash, capsys):
    rc = main_mod.main(["session", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    # Must surface the new verbs so users discover them via --help.
    for verb in ("end", "rescue", "fork", "log list", "log show"):
        assert verb in out, f"Help missing entry for: {verb!r}"


def test_session_no_subcommand_prints_usage(project_dir, no_bash, capsys):
    rc = main_mod.main(["session"])
    assert rc == 2
    # No subcommand → usage hint, exit code 2.
