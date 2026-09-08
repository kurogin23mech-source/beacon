"""ms-159 / e-6290 — pin the derived-fallback contract for a session's working
target + activity (lib/working_target).

Mirrors tests/test_derive_state_e6243.py: the point of the pure function is that
the declaration-authoritative / derived-fallback asymmetry can be exhaustively
pinned without a bus or filesystem. We cover both branches (declared wins /
derived fills in), the priority order of the derived sources, and the edge cases
(garbage declaration, posix/windows paths, case-insensitive ms-id).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import working_target as wt  # noqa: E402


# ---------------------------------------------------------------------------
# derive_working_target — declaration is authoritative
# ---------------------------------------------------------------------------

class TestDeclarationWins:
    def test_valid_declaration_used_verbatim(self):
        declared = {
            "root": {"kind": "project", "id": "beacon-b95643", "label": "Beacon"},
            "target": {"kind": "task", "id": "e-6290", "title": "derive fallback"},
        }
        out = wt.derive_working_target(
            declared, branch="ms-159-fork-361e58", cwd="/x/ms-159",
            fork_json={"target_ms_id": "ms-159"})
        assert out["source"] == wt.SOURCE_DECLARED
        assert out["target"]["id"] == "e-6290"
        assert out["target"]["kind"] == "task"
        assert out["target"]["source"] == wt.SOURCE_DECLARED
        # declared root preserved verbatim
        assert out["root"]["id"] == "beacon-b95643"

    def test_declaration_missing_kind_defaults_milestone(self):
        declared = {"target": {"id": "ms-42"}}
        out = wt.derive_working_target(declared)
        assert out["source"] == wt.SOURCE_DECLARED
        assert out["target"]["kind"] == "milestone"
        assert out["target"]["id"] == "ms-42"

    def test_garbage_declaration_falls_through_to_derived(self):
        # A dict with no usable target id is not trusted — fall to fork_json.
        for junk in ({}, {"target": {}}, {"target": {"id": ""}}, {"target": "nope"}):
            out = wt.derive_working_target(
                junk, fork_json={"target_ms_id": "ms-159"})
            assert out["source"] == wt.SOURCE_FORK_JSON, junk
            assert out["target"]["id"] == "ms-159"

    def test_non_dict_declaration_ignored(self):
        for junk in (None, "", "ms-1", 42, []):
            out = wt.derive_working_target(junk, branch="ms-7-fork-a")
            assert out["source"] == wt.SOURCE_BRANCH
            assert out["target"]["id"] == "ms-7"


# ---------------------------------------------------------------------------
# derive_working_target — derived fallback priority order
# ---------------------------------------------------------------------------

class TestDerivedPriority:
    def test_fork_json_beats_branch_and_focus(self):
        out = wt.derive_working_target(
            None, branch="ms-999-fork-x", cwd="/w/ms-888",
            fork_json={"target_ms_id": "ms-159", "target_ms_title": "統合オペUI"},
            focus_milestone={"id": "ms-1", "title": "old"})
        assert out["source"] == wt.SOURCE_FORK_JSON
        assert out["target"]["id"] == "ms-159"
        assert out["target"]["title"] == "統合オペUI"

    def test_branch_beats_cwd_and_focus(self):
        out = wt.derive_working_target(
            None, branch="ms-159-fork-361e58", cwd="/w/ms-888",
            focus_milestone={"id": "ms-1"})
        assert out["source"] == wt.SOURCE_BRANCH
        assert out["target"]["id"] == "ms-159"

    def test_cwd_used_when_branch_has_no_ms_id(self):
        out = wt.derive_working_target(
            None, branch="main", cwd="/repo/.worktrees/ms-77-backoffice",
            focus_milestone={"id": "ms-1"})
        assert out["source"] == wt.SOURCE_CWD
        assert out["target"]["id"] == "ms-77"

    def test_focus_is_weakest_fallback(self):
        out = wt.derive_working_target(
            None, branch="main", cwd="/repo",
            focus_milestone={"id": "ms-133", "title": "職種汎用化"})
        assert out["source"] == wt.SOURCE_FOCUS
        assert out["target"]["id"] == "ms-133"
        assert out["target"]["title"] == "職種汎用化"

    def test_nothing_knowable_returns_none_target(self):
        out = wt.derive_working_target(None, branch="main", cwd="/repo")
        assert out["source"] == wt.SOURCE_NONE
        assert out["target"] is None
        # root still derived from cwd basename so the row shape is stable
        assert out["root"] == {"kind": "project", "id": "", "label": "repo"}

    def test_case_insensitive_ms_id_normalized_lower(self):
        out = wt.derive_working_target(None, branch="MS-159-Fork-Z")
        assert out["target"]["id"] == "ms-159"


# ---------------------------------------------------------------------------
# root derivation
# ---------------------------------------------------------------------------

class TestRootDerivation:
    def test_project_hint_wins(self):
        out = wt.derive_working_target(
            None, branch="ms-5-fork-a", cwd="/w/x",
            fork_json={"parent_repo_path": "/Users/me/tools/beacon"},
            project={"kind": "project", "id": "beacon-b95643", "label": "Beacon"})
        assert out["root"]["id"] == "beacon-b95643"
        assert out["root"]["label"] == "Beacon"

    def test_fork_parent_repo_basename_when_no_hint(self):
        out = wt.derive_working_target(
            None, branch="ms-5-fork-a", cwd="/w/x",
            fork_json={"parent_repo_path": "/Users/me/tools/beacon"})
        assert out["root"] == {"kind": "project", "id": "", "label": "beacon"}

    def test_cwd_basename_last_resort(self):
        out = wt.derive_working_target(None, branch="ms-5", cwd="/a/b/myproj/")
        assert out["root"]["label"] == "myproj"

    def test_windows_path_basename(self):
        out = wt.derive_working_target(
            None, branch="ms-5", cwd="D:\\Projects\\beacon\\wt")
        assert out["root"]["label"] == "wt"

    def test_root_none_when_nothing(self):
        out = wt.derive_working_target(None, branch="ms-5", cwd="")
        assert out["root"] is None
        assert out["target"]["id"] == "ms-5"


# ---------------------------------------------------------------------------
# derive_activity
# ---------------------------------------------------------------------------

class TestDeriveActivity:
    def test_declared_wins(self):
        assert wt.derive_activity(
            "レビュー findings 反映中", head_subject="fix: whatever") == \
            "レビュー findings 反映中"

    def test_head_subject_fallback(self):
        assert wt.derive_activity(
            None, head_subject="feat(ms-159): derive_state 純粋関数") == \
            "feat(ms-159): derive_state 純粋関数"

    def test_empty_when_neither(self):
        assert wt.derive_activity(None) == ""
        assert wt.derive_activity("", head_subject="") == ""

    def test_whitespace_declared_falls_back(self):
        assert wt.derive_activity("   ", head_subject="real") == "real"

    def test_strips_surrounding_whitespace(self):
        assert wt.derive_activity("  doing X  ") == "doing X"
