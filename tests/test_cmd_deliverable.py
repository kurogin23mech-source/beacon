"""CLI-surface tests for `beacon deliverable list` (ms-155 e-5666, PR #679 AX).

The AX review of the resolver PR flagged that a resolve FAILURE must be visible at
the process boundary — not buried in a stdout line. These tests pin that contract:

  * ``--json`` carries a top-level ``{mode, items}`` discriminator (pointer vs
    resolved) so a consumer never mistakes pointers for produced value.
  * ``--resolve --json`` adds ``all_resolved`` + ``unresolved`` so partial failure
    is detectable without iterating every entry.
  * ``--resolve`` exits non-zero when a pointer cannot be resolved (2 = partial,
    1 = total), 0 when everything resolves.
"""
from __future__ import annotations

import sys
import json as _json
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import cmd_deliverable as cd  # noqa: E402


def _run(monkeypatch, capsys, *, resolve, rows, json_mode=True):
    monkeypatch.setenv("BEACON_RESOLVE", "1" if resolve else "")
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")
    with mock.patch.object(cd, "load_project", return_value={}):
        if resolve:
            patcher = mock.patch.object(
                cd._dr, "resolve_project_deliverables", return_value=rows)
        else:
            patcher = mock.patch.object(
                cd.occupation, "project_deliverables", return_value=rows)
        with patcher:
            code = 0
            try:
                cd.cmd_deliverable_list()
            except SystemExit as e:
                code = e.code
    out = capsys.readouterr()
    return code, out


def test_pointer_json_has_mode_discriminator(monkeypatch, capsys):
    rows = [{"target_class": "milestone", "kind": "feature-map",
             "projector": "doc", "ref": "application-map"}]
    code, out = _run(monkeypatch, capsys, resolve=False, rows=rows)
    payload = _json.loads(out.out)
    assert code == 0
    assert payload["mode"] == "pointer"
    assert payload["items"] == rows
    assert "all_resolved" not in payload   # only meaningful for resolved mode


def test_resolved_json_signals_full_success(monkeypatch, capsys):
    rows = [{"target_class": "milestone", "projector": "doc", "ref": "map",
             "resolved": {"found": True, "content": "body"}}]
    code, out = _run(monkeypatch, capsys, resolve=True, rows=rows)
    payload = _json.loads(out.out)
    assert code == 0
    assert payload["mode"] == "resolved"
    assert payload["all_resolved"] is True
    assert payload["unresolved"] == []


def test_resolve_partial_failure_exits_2_and_flags_unresolved(monkeypatch, capsys):
    rows = [
        {"target_class": "milestone", "projector": "doc", "ref": "map",
         "resolved": {"found": True, "content": "body"}},
        {"target_class": "opportunity", "projector": "doc", "ref": "gone",
         "resolved": {"found": False, "error": "document 'gone' not found"}},
    ]
    code, out = _run(monkeypatch, capsys, resolve=True, rows=rows)
    payload = _json.loads(out.out)
    assert code == 2                       # partial
    assert payload["all_resolved"] is False
    assert payload["unresolved"] == ["gone"]


def test_resolve_total_failure_exits_1(monkeypatch, capsys):
    rows = [{"target_class": "milestone", "projector": "doc", "ref": "gone",
             "resolved": {"found": False, "error": "document 'gone' not found"}}]
    code, _out = _run(monkeypatch, capsys, resolve=True, rows=rows)
    assert code == 1                       # total


def test_resolve_warnings_go_to_stderr_not_stdout(monkeypatch, capsys):
    rows = [{"target_class": "milestone", "projector": "doc", "ref": "gone",
             "label": "機能",
             "resolved": {"found": False, "error": "document 'gone' not found"}}]
    with pytest.raises(SystemExit):
        monkeypatch.setenv("BEACON_RESOLVE", "1")
        monkeypatch.setenv("BEACON_JSON", "")
        with mock.patch.object(cd, "load_project", return_value={}), \
             mock.patch.object(cd._dr, "resolve_project_deliverables",
                               return_value=rows):
            cd.cmd_deliverable_list()
    out = capsys.readouterr()
    assert "未解決" in out.err            # warning on stderr
    assert "未解決" not in out.out        # not on stdout
