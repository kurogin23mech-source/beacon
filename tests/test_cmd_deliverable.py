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


# ---------------------------------------------------------------------------
# Curation surface — add / retire / supersede / map (ms-161 e-5902/e-5903/e-5851).
#
# These drive the CLI cmd functions over an in-memory ``data`` dict: ``load_project``
# returns it, ``save_project`` records that it was called (so we assert the mutation
# persisted through the same seam every write verb uses). No cloud / disk touched.
# ---------------------------------------------------------------------------

_VERB_FN = {
    "add": lambda: cd.cmd_deliverable_add(),
    "retire": lambda: cd.cmd_deliverable_retire(),
    "supersede": lambda: cd.cmd_deliverable_supersede(),
    "map": lambda: cd.cmd_deliverable_map(),
}


def _drive(monkeypatch, verb, argv, data):
    """Run deliverable ``verb`` with ``argv`` (the flags after the verb) against a
    shared ``data`` dict. ``load_project`` returns it; ``save_project`` asserts it
    persisted the SAME dict. Returns (exit_code, saved_flag)."""
    monkeypatch.setattr(sys, "argv", ["commands.py", f"deliverable_{verb}", *argv])
    saved = {"called": False}

    def _save(d, op=None):
        assert d is data
        saved["called"] = True

    code = 0
    with mock.patch.object(cd, "load_project", return_value=data), \
         mock.patch.object(cd, "save_project", side_effect=_save):
        try:
            _VERB_FN[verb]()
        except SystemExit as e:
            code = e.code or 0
    return code, saved["called"]


def _add(monkeypatch, capsys, data, *flags):
    code, saved = _drive(monkeypatch, "add", list(flags), data)
    return code, saved, capsys.readouterr()


def test_add_appends_surface_grained_entry_with_wedges(monkeypatch, capsys):
    data = {"name": "P", "profession": "dev"}
    code, saved, out = _add(
        monkeypatch, capsys, data,
        "--title", "status", "--summary", "1画面で把握できる",
        "--category", "状態を一望する", "--area", "見失わない",
        "--tag", "cli:beacon status", "--tag", "api:GET /api/projects", "--json")
    assert code == 0 and saved
    entry = _json.loads(out.out)
    assert entry["id"] == "dlv-1"
    assert entry["source"] == {"target_id": "root", "kind": "root"}
    assert entry["category"] == "状態を一望する"
    # --area became an area: tag, ordered before the wedges
    assert entry["tags"][0] == "area:見失わない"
    assert "cli:beacon status" in entry["tags"]
    # persisted into the root changelog
    assert data[cd._dc.CHANGELOG_KEY][0]["id"] == "dlv-1"


def test_add_allows_multiple_entries_for_one_target(monkeypatch, capsys):
    """e-5902 の核: 1 target が複数 surface を記帳できる（lib append に
    (target,category) dedup が無いので CLI 経路で 2 本目が通る）。"""
    data = {"name": "P", "profession": "dev"}
    _add(monkeypatch, capsys, data,
         "--title", "a", "--summary", "sa", "--category", "cat",
         "--source-target", "ms-9", "--source-kind", "milestone")
    _add(monkeypatch, capsys, data,
         "--title", "b", "--summary", "sb", "--category", "cat",
         "--source-target", "ms-9", "--source-kind", "milestone")
    log = data[cd._dc.CHANGELOG_KEY]
    assert [e["id"] for e in log] == ["dlv-1", "dlv-2"]
    assert all(e["source"]["target_id"] == "ms-9" for e in log)


def test_add_bad_input_exits_1(monkeypatch, capsys):
    data = {"name": "P"}
    # empty title is rejected by normalize_deliverable_entry → exit 1, no save
    code, saved = _drive(
        monkeypatch, "add",
        ["--title", "", "--summary", "s", "--category", "c"], data)
    assert code == 1 and not saved
    assert "Error" in capsys.readouterr().err


def test_retire_flips_status_and_drops_from_map(monkeypatch, capsys):
    data = {"name": "P", "profession": "dev"}
    cd._dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "cat", "title": "t", "summary": "s"})
    code, saved = _drive(monkeypatch, "retire", ["dlv-1", "--reason", "廃止"], data)
    assert code == 0 and saved
    assert data[cd._dc.CHANGELOG_KEY][0]["status"] == "retired"
    # gone from the derived current-state map
    assert cd._dm.summarize_map(data)["total"] == 0


def test_retire_unknown_id_exits_1(monkeypatch, capsys):
    data = {"name": "P"}
    code, saved = _drive(monkeypatch, "retire", ["dlv-999"], data)
    assert code == 1 and not saved


def test_supersede_replaces_predecessor(monkeypatch, capsys):
    data = {"name": "P", "profession": "dev"}
    cd._dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "cat", "title": "old", "summary": "old"})
    code, saved = _drive(
        monkeypatch, "supersede",
        ["dlv-1", "--title", "new", "--summary", "new", "--category", "cat"],
        data)
    assert code == 0 and saved
    log = data[cd._dc.CHANGELOG_KEY]
    assert log[0]["status"] == "superseded"
    assert log[1]["supersedes"] == "dlv-1"
    # only the successor remains current
    active = cd._dm.summarize_map(data)
    assert active["total"] == 1
    assert active["categories"][0]["entries"][0]["title"] == "new"


def test_map_renders_derived_current_state(monkeypatch, capsys):
    data = {"name": "P", "profession": "dev"}
    cd._dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "状態を一望する", "title": "status",
        "summary": "1画面で把握できる",
        "tags": ["area:見失わない", "cli:beacon status"]})
    code, _saved = _drive(monkeypatch, "map", [], data)
    assert code == 0
    out = capsys.readouterr().out
    assert "アプリケーション全貌マップ" in out
    assert "`cli:beacon status`" in out       # wedge survives to the rendered map
