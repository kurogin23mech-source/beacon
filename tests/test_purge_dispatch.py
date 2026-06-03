"""PowerShell dispatch parity for the *purge recovery commands (e-893).

milestone / entry / operation purge are duplicate-ID recovery paths. They had
no native PowerShell dispatch — `bin/beacon` (bash) was the only entry, so a
Windows user with a corrupt project had no way out. These tests verify that
``beacon_cli/dispatch.py`` now translates each purge to the right commands.py
subcommand + BEACON_* env, and — crucially — that purge bypasses the
``_ensure_project`` guard so it runs even when no project is loadable.

We patch ``dispatch.subprocess.call`` and assert what dispatch *would have*
invoked, mirroring tests/test_powershell_dispatch.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch  # noqa: E402  (sys.path mutated)


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
def no_project_cwd(tmp_path, monkeypatch):
    """chdir into an empty dir with no .beacon anywhere up the tree."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path — correct subcommand + env, and bypasses ensure_project
# ---------------------------------------------------------------------------


def test_milestone_purge_dispatch(no_project_cwd, captured_call):
    rc = dispatch.dispatch(
        ROOT, ["milestone", "purge", "ms-13", "--reason", "dup", "--index", "2", "--json"]
    )
    assert rc == 0  # ran even with NO project loadable (bypasses ensure_project)
    assert captured_call["cmd"] is not None
    assert captured_call["cmd"][-1] == "milestone_purge"
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-13"
    assert env["BEACON_REASON"] == "dup"
    assert env["BEACON_INDEX"] == "2"
    assert env["BEACON_JSON"] == "1"


def test_entry_purge_dispatch(no_project_cwd, captured_call):
    rc = dispatch.dispatch(
        ROOT, ["entry", "purge", "e-5", "--reason", "dup recovery", "--index", "1"]
    )
    assert rc == 0
    assert captured_call["cmd"][-1] == "entry_purge"
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-5"
    assert env["BEACON_REASON"] == "dup recovery"
    assert env["BEACON_INDEX"] == "1"
    assert env["BEACON_JSON"] == ""


def test_operation_purge_dispatch(no_project_cwd, captured_call):
    rc = dispatch.dispatch(
        ROOT, ["operation", "purge", "op-2", "--reason", "dup"]
    )
    assert rc == 0
    assert captured_call["cmd"][-1] == "operation_purge"
    env = captured_call["env"]
    assert env["BEACON_OP_ID"] == "op-2"
    assert env["BEACON_REASON"] == "dup"
    assert env["BEACON_INDEX"] == ""


# ---------------------------------------------------------------------------
# Guards — reason and id required, no commands.py call when missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["milestone", "purge", "ms-13"],
    ["entry", "purge", "e-5"],
    ["operation", "purge", "op-2"],
])
def test_purge_requires_reason(no_project_cwd, captured_call, argv):
    rc = dispatch.dispatch(ROOT, argv)
    assert rc == 1
    assert captured_call["cmd"] is None


@pytest.mark.parametrize("argv", [
    ["milestone", "purge", "--reason", "x"],
    ["entry", "purge", "--reason", "x"],
    ["operation", "purge", "--reason", "x"],
])
def test_purge_requires_id(no_project_cwd, captured_call, argv):
    rc = dispatch.dispatch(ROOT, argv)
    assert rc == 1
    assert captured_call["cmd"] is None


# ---------------------------------------------------------------------------
# operation handler usage (no subcommand)
# ---------------------------------------------------------------------------


def test_operation_no_subcommand_shows_usage(no_project_cwd, captured_call, capsys):
    rc = dispatch.dispatch(ROOT, ["operation"])
    assert rc == 2
    assert "operation purge" in capsys.readouterr().out
    assert captured_call["cmd"] is None
