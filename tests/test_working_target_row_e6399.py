"""ms-159 / e-6399 — pin the row-enrichment contract (lib/working_target
``enrich_row`` / ``working_target_for_row`` / ``activity_for_row``).

The pure derive functions (pinned in test_working_target_e6290) took already-
extracted signals. e-6399 wires them into the CLI fetch path: a whole server
directory row → the derive inputs (``git.branch`` / ``cwd`` / ``focus.milestone``
/ ``git.head_subject``) → the attached ``working_target`` / ``activity``. These
tests pin that mapping so the roster stops showing an empty ``(no target)/—``
shell, without needing a live bus. They also lock the priority ladder end-to-end
from a realistic row and the non-mutation guarantee.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import working_target as wt  # noqa: E402


def _row(**over):
    """A realistic server directory row (shape from `bus directory --json`)."""
    row = {
        "session_id": "sv-abc-123",
        "project_id": "beacon-b95643",
        "project_name": "Beacon",
        "cwd": "/Users/x/tools/beacon",
        "git": {"branch": "", "head_short": "", "head_subject": ""},
        "focus": {"milestone": {"id": "ms-133", "title": "職種インスタンス"}},
    }
    row.update(over)
    return row


class TestWorkingTargetForRow:
    def test_branch_ms_id_beats_focus(self):
        # A fork worktree row: branch encodes the real per-session target, which
        # must win over the project's active MS (focus, not session-specific).
        row = _row(git={"branch": "ms-159-fork-ff7b86",
                         "head_short": "", "head_subject": ""})
        out = wt.working_target_for_row(row)
        assert out["target"]["id"] == "ms-159"
        assert out["source"] == wt.SOURCE_BRANCH

    def test_focus_milestone_fallback_when_no_branch(self):
        # No branch/cwd ms-id → fall to the project's active MS (weakest).
        out = wt.working_target_for_row(_row(cwd="/Users/x/tools/beacon"))
        assert out["target"]["id"] == "ms-133"
        assert out["source"] == wt.SOURCE_FOCUS

    def test_root_is_the_rows_own_project(self):
        out = wt.working_target_for_row(_row())
        assert out["root"]["id"] == "beacon-b95643"
        assert out["root"]["label"] == "Beacon"

    def test_existing_working_target_kept_verbatim(self):
        # A row that already carries a working_target (server stamp / declaration)
        # is authoritative and beats every derived guess — returned untouched.
        existing = {"root": {"id": "beacon-b95643", "label": "Beacon"},
                    "target": {"kind": "task", "id": "e-6399",
                               "title": "produce D data"},
                    "source": wt.SOURCE_DECLARED}
        row = _row(working_target=existing,
                   git={"branch": "ms-159", "head_short": "", "head_subject": ""})
        out = wt.working_target_for_row(row)
        assert out is existing  # verbatim, not clobbered by the branch guess

    def test_declared_root_without_target_survives(self):
        # The e6293 regression: a declared root with no specific target must NOT
        # be dropped just because there is no target id (an idle session that
        # declared its project but no work item).
        existing = {"root": {"id": "p2", "label": "Sales"}, "target": None}
        out = wt.working_target_for_row(_row(working_target=existing))
        assert out["root"]["label"] == "Sales"

    def test_empty_shell_working_target_is_derived(self):
        # An empty {} working_target is not "content" — fall to the derive ladder.
        row = _row(working_target={},
                   git={"branch": "ms-159-fork-ff7b86", "head_subject": ""})
        out = wt.working_target_for_row(row)
        assert out["target"]["id"] == "ms-159"
        assert out["source"] == wt.SOURCE_BRANCH

    def test_cwd_ms_id_when_branch_blank(self):
        row = _row(cwd="/Users/x/.worktrees/ms-171-fork-abc",
                   focus={"milestone": None})
        out = wt.working_target_for_row(row)
        assert out["target"]["id"] == "ms-171"
        assert out["source"] == wt.SOURCE_CWD

    def test_no_target_when_nothing_knowable(self):
        row = _row(cwd="/tmp/plain", git={"branch": ""}, focus={"milestone": None})
        out = wt.working_target_for_row(row)
        assert out["target"] is None
        assert out["source"] == wt.SOURCE_NONE

    def test_garbage_row_does_not_raise(self):
        for junk in (None, {}, {"git": "nope"}, {"focus": 5}):
            out = wt.working_target_for_row(junk)
            assert set(out) == {"root", "target", "source"}


class TestActivityForRow:
    def test_head_subject_fallback(self):
        row = _row(git={"branch": "", "head_short": "abc",
                        "head_subject": "feat(ms-159): produce D data"})
        assert wt.activity_for_row(row) == "feat(ms-159): produce D data"

    def test_declared_activity_wins(self):
        row = _row(activity="レビュー対応中",
                   git={"branch": "", "head_subject": "fix: x"})
        assert wt.activity_for_row(row) == "レビュー対応中"

    def test_empty_when_no_signal(self):
        assert wt.activity_for_row(_row()) == ""

    def test_garbage_row_does_not_raise(self):
        for junk in (None, {}, {"git": None}):
            assert wt.activity_for_row(junk) == ""


class TestEnrichRow:
    def test_attaches_both_keys(self):
        row = _row(git={"branch": "ms-159-fork-ff7b86", "head_short": "",
                        "head_subject": "feat: produce D data"})
        out = wt.enrich_row(row)
        assert out["working_target"]["target"]["id"] == "ms-159"
        assert out["activity"] == "feat: produce D data"
        # original keys preserved
        assert out["session_id"] == "sv-abc-123"

    def test_non_mutating(self):
        row = _row()
        assert "working_target" not in row
        wt.enrich_row(row)
        # enrichment must not leak back into the caller's row
        assert "working_target" not in row
        assert "activity" not in row

    def test_non_dict_passes_through(self):
        assert wt.enrich_row(None) is None
        assert wt.enrich_row("x") == "x"

    def test_enrich_rows_over_list(self):
        rows = [_row(session_id="a"), _row(session_id="b")]
        out = wt.enrich_rows(rows)
        assert len(out) == 2
        assert all("working_target" in r and "activity" in r for r in out)

    def test_enrich_rows_none(self):
        assert wt.enrich_rows(None) == []
