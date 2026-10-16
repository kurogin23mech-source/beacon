"""Tests for the Python tool-record hook (``beacon_cli.hooks.save_hook``).

This is the ``beacon-hook-save`` console script — the leg
``commands._resolve_hook_command`` prefers FIRST when it writes
``settings.json``, so on pipx / wheel / Windows installs it is the hook that
actually runs.

WHAT CHANGED, and why this file was rewritten (ms-180)
------------------------------------------------------
This module used to be an independent translation of the old bash hook, with its
own ``_MONITORED_SOURCES`` table naming two Google-Drive servers and a
``_SKIP_OPERATION_PREFIXES`` read/write filter. The previous version of this file
pinned exactly those internals (``_source_for``, ``_is_write_operation``, "skips
unmonitored tool", "skips read operations").

ms-180 removed that hand-written table: which tools get recorded, and where, is
now a DECLARATION in the tool ledger (``.beacon/tools.json``), consulted once in
``lib/tool_record_hook.handle``. So those old tests were pinning the behaviour
the milestone exists to delete — they are not regressions to preserve, and the
previous file is kept at ``.trash/test_save_hook_py.py.pre-ms180`` for reference.

ms-180 ALSO only rewrote the bash leg at first, leaving this one on the old
table — so on the preferred install path the ledger did nothing and nothing said
so. An independent philosophy review of PR #802 found that. The tests below are
therefore mostly about PARITY and DELEGATION: this leg must reach the same
decision as the bash leg, because there is one definition and two wrappers.
Per-leg behavioural duplicates are in
``tests/test_tool_record_hook_parity_ms180.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "lib"))

from beacon_cli.hooks import save_hook  # noqa: E402
import tool_ledger as tl                # noqa: E402

BASH_LEG = _REPO_ROOT / "bin" / "beacon-save-hook.sh"


@pytest.fixture
def beacon_project(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "entries": []}], "documents": []},
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _declare(root: Path, *rows: dict) -> None:
    led = tl.empty_ledger()
    for row in rows:
        tl.upsert(led, row)
    tl.save_ledger(tl.ledger_path(root), led)


def _payload(root: Path, tool_name: str, tool_input=None,
             event: str = "PostToolUse") -> str:
    return json.dumps({"hook_event_name": event, "session_id": "s",
                       "cwd": str(root), "tool_name": tool_name,
                       "tool_input": tool_input or {}})


def _run_python_leg(root: Path, body: str, cwd: Path = None) -> int:
    import os
    env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
    return subprocess.run([sys.executable, "-m", "beacon_cli.hooks.save_hook"],
                          input=body, capture_output=True, text=True,
                          timeout=60, cwd=str(cwd or root), env=env).returncode


def _run_bash_leg(root: Path, body: str) -> int:
    return subprocess.run([str(BASH_LEG)], input=body, capture_output=True,
                          text=True, timeout=60, cwd=str(root)).returncode


def _entries(root: Path) -> list:
    return json.loads((root / ".beacon" / "project.json").read_text(
        encoding="utf-8"))["milestones"][0]["entries"]


# ---------------------------------------------------------------------------
# The hand-written table is gone (= what the old tests pinned).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("removed", ["_MONITORED_SOURCES", "_source_for",
                                      "_is_write_operation",
                                      "_SKIP_OPERATION_PREFIXES",
                                      "_active_milestone_id"])
def test_the_hand_written_filter_is_gone(removed):
    """These were the old per-tool policy. Keeping any of them would mean the
    policy still lives in code rather than in the ledger (SPEC 方針3)."""
    assert not hasattr(save_hook, removed), (
        f"{removed} is back; the record decision belongs to the ledger")


def test_the_module_delegates_to_the_shared_decision():
    assert hasattr(save_hook, "_import_shared")
    source = (_REPO_ROOT / "beacon_cli" / "hooks" / "save_hook.py").read_text(
        encoding="utf-8")
    assert "tool_record_hook" in source


# ---------------------------------------------------------------------------
# Parity with the bash leg — the property the philosophy review found broken.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leg", ["python", "bash"])
def test_a_declared_record_sink_records_on_either_leg(beacon_project, leg):
    _declare(beacon_project, tl.build_record(
        "mcp__gmail__send_email", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    run = _run_python_leg if leg == "python" else _run_bash_leg
    assert run(beacon_project,
               _payload(beacon_project, "mcp__gmail__send_email",
                        {"subject": "ご提案"})) == 0
    assert len(_entries(beacon_project)) == 1


@pytest.mark.parametrize("leg", ["python", "bash"])
def test_an_undeclared_tool_records_nothing_on_either_leg(beacon_project, leg):
    _declare(beacon_project)
    run = _run_python_leg if leg == "python" else _run_bash_leg
    assert run(beacon_project,
               _payload(beacon_project, "mcp__github__create_issue")) == 0
    assert _entries(beacon_project) == []


def test_a_non_mcp_tool_records_when_the_ledger_says_so(beacon_project):
    """The old filter dropped everything that was not an MCP call. The ledger
    accepts any 道具, so a declared non-MCP tool must record — this is also why
    the install matcher is "*" rather than "mcp__"."""
    _declare(beacon_project, tl.build_record(
        "Bash", purpose="デプロイを打つ", effect=tl.EFFECT_STATE_CHANGE,
        recorder=tl.RECORDER_SAVE))
    assert _run_python_leg(beacon_project,
                           _payload(beacon_project, "Bash",
                                    {"command": "make deploy"})) == 0
    assert len(_entries(beacon_project)) == 1


def test_a_read_operation_records_when_the_ledger_says_so(beacon_project):
    """The old filter hardcoded read/get/list/search as never-record. That is now
    a per-tool DECLARATION: reading a customer's mail is evidence, grepping code
    is not (SPEC 方針3)."""
    _declare(beacon_project, tl.build_record(
        "mcp__gmail__search_emails", purpose="顧客のメールを確認する",
        effect=tl.EFFECT_READ_ONLY, recorder=tl.RECORDER_EVIDENCE))
    assert _run_python_leg(beacon_project,
                           _payload(beacon_project, "mcp__gmail__search_emails",
                                    {"query": "A社"})) == 0
    assert len(_entries(beacon_project)) == 1


# ---------------------------------------------------------------------------
# Fail-safe contract (unchanged from the old file's intent).
# ---------------------------------------------------------------------------

def test_main_returns_zero_outside_a_beacon_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin",
                        __import__("io").StringIO(json.dumps(
                            {"hook_event_name": "PostToolUse",
                             "cwd": str(tmp_path),
                             "tool_name": "mcp__x__y"})))
    assert save_hook.main() == 0


@pytest.mark.parametrize("body", ["", "   ", "not json", "[]", "null",
                                   '{"hook_event_name":"PostToolUse"}'])
def test_the_python_leg_exits_cleanly_on_malformed_input(tmp_path, body):
    assert _run_python_leg(tmp_path, body) == 0


def test_the_python_leg_is_silent_on_stdout(beacon_project):
    """A recording hook must inject nothing into the model's context."""
    import os
    _declare(beacon_project, tl.build_record(
        "mcp__gmail__send_email", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
    proc = subprocess.run([sys.executable, "-m", "beacon_cli.hooks.save_hook"],
                          input=_payload(beacon_project,
                                         "mcp__gmail__send_email",
                                         {"subject": "x"}),
                          capture_output=True, text=True, timeout=60,
                          cwd=str(beacon_project), env=env)
    assert proc.stdout == ""


def test_a_corrupt_ledger_records_nothing_and_does_not_fail(beacon_project):
    Path(tl.ledger_path(beacon_project)).write_text("{not json",
                                                    encoding="utf-8")
    assert _run_python_leg(beacon_project,
                           _payload(beacon_project,
                                    "mcp__gmail__send_email")) == 0
    assert _entries(beacon_project) == []
