"""Doc root rollup — a spec/memo/report linked to a child Target must be
reachable from the first-class root target (ms-160 e-5817).

Before this, a doc's Target link was flat/single: a doc attached to ``ms-104``
could be listed with ``--ms ms-104`` but was invisible from the root, and
project-level docs sat in the pre-root "target 空" state with no way to reach
them from the root either (ユーザー指摘 C1). ``doc list --target root`` now rolls
up: it surfaces project-level docs (root sentinel / not yet linked) PLUS every
doc attached to any descendant Target.
"""

from __future__ import annotations

import os
import sys

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import root_target  # noqa: E402


def _data():
    """A dev project with two live child milestones and one cancelled."""
    return {
        "name": "demo",
        "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "one", "status": "in_progress",
             "progress": 0, "entries": []},
            {"id": "ms-2", "title": "two", "status": "todo",
             "progress": 0, "entries": []},
            {"id": "ms-9", "title": "gone", "status": "cancelled",
             "progress": 0, "entries": []},
        ],
        "operations": [],
    }


# --- helper: child_target_ids ----------------------------------------------

def test_child_target_ids_excludes_cancelled():
    ids = root_target.child_target_ids(_data())
    assert "ms-1" in ids and "ms-2" in ids
    # cancelled Targets are dropped by project_targets, so their docs must not
    # resurface under root.
    assert "ms-9" not in ids


# --- helper: doc_rolls_up_to_root ------------------------------------------

@pytest.mark.parametrize("meta,expected", [
    ({"target": "ms-1"}, True),          # child Target -> rolled up
    ({"target": "ms-2"}, True),          # other live child -> rolled up
    ({"target": "root"}, True),          # explicit project-level (root sentinel)
    ({"target": ""}, True),              # pre-root "target 空" project doc
    ({}, True),                          # no link at all -> project-level
    ({"milestone": "ms-1"}, True),       # legacy link key, tolerant read
    ({"target": "ms-9"}, False),         # cancelled child -> NOT reachable
    ({"target": "ms-404"}, False),       # unknown / detached Target
    ({"target": "tk-3"}, False),         # trek (separate axis) -> NOT under root
])
def test_doc_rolls_up_to_root(meta, expected):
    child_ids = root_target.child_target_ids(_data())
    assert root_target.doc_rolls_up_to_root(meta, child_ids) is expected


def test_doc_rolls_up_to_root_derives_children_when_not_passed():
    # child_ids omitted -> derived from data once, same verdict.
    assert root_target.doc_rolls_up_to_root({"target": "ms-1"}, data=_data()) is True
    assert root_target.doc_rolls_up_to_root({"target": "ms-9"}, data=_data()) is False


# --- integration: cmd_doc_list --target root filters correctly -------------

def test_cmd_doc_list_target_root_rollup(monkeypatch, capsys):
    import cmd_doc

    docs = [
        {"doc_id": "d1", "scope": "spec", "title": "child", "target": "ms-1"},
        {"doc_id": "d2", "scope": "memo", "title": "project", "target": "root"},
        {"doc_id": "d3", "scope": "spec", "title": "bare", "target": ""},
        {"doc_id": "d4", "scope": "spec", "title": "gone", "target": "ms-9"},
        {"doc_id": "d5", "scope": "spec", "title": "trek", "target": "tk-3"},
    ]

    class _Store:
        def list_documents(self):
            return list(docs)

    monkeypatch.setattr(cmd_doc, "get_store", lambda: _Store())
    monkeypatch.setattr(cmd_doc, "load_project", lambda: _data())
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_TARGET", "root")

    cmd_doc.cmd_doc_list()
    import json
    out = json.loads(capsys.readouterr().out)
    got = {d["doc_id"] for d in out}
    # d1 (child), d2 (root sentinel), d3 (bare/project-level) reachable;
    # d4 (cancelled child) and d5 (trek) NOT.
    assert got == {"d1", "d2", "d3"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
