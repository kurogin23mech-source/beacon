"""Unit tests for ``lib/phantom_done_evidence.py`` (ms-95 / e-2726).

Covers the keyword extraction + commit collection + evidence evaluation
helpers. The endpoint-side wiring (= ``server/app.py`` ``done_entry``)
is exercised separately in ``test_phantom_done_warning_endpoint.py``.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import phantom_done_evidence as pde  # type: ignore


# ---------------------------------------------------------------------------
# extract_task_keywords
# ---------------------------------------------------------------------------


class TestExtractTaskKeywords:
    def test_pulls_from_description_only(self):
        entry = {"id": "e-1", "description": "Add phantom done evidence gate"}
        kws = pde.extract_task_keywords(entry)
        assert "phantom" in kws
        assert "evidence" in kws
        # "Add" is in the stop-token list — should be excluded.
        assert "add" not in kws

    def test_merges_all_keyword_fields(self):
        entry = {
            "id": "e-2",
            "description": "Wire phantom warning",
            "acceptance_criteria": "emit cloudlogging json line",
            "motivation": "observability for done audits",
            "behavior": "flag not filter",
            "detail": "use overlap threshold",
        }
        kws = pde.extract_task_keywords(entry)
        # one keyword from each field, picking words that survive the
        # stop-token filter
        assert "phantom" in kws  # description
        assert "cloudlogging" in kws  # acceptance_criteria
        assert "observability" in kws  # motivation
        assert "filter" in kws  # behavior
        assert "overlap" in kws  # detail

    def test_japanese_keywords_survive(self):
        entry = {
            "id": "e-3",
            "description": "phantom done を検出する仕組み",
            "acceptance_criteria": "warning を Cloud Logging に出力",
        }
        kws = pde.extract_task_keywords(entry)
        assert "phantom" in kws
        assert "done" in kws or "warning" in kws  # english tokens
        # JA noun (= "検出する仕組み" — particles split it, so we get
        # multi-char chunks)
        assert any(len(t) >= 2 and any("぀" <= ch <= "鿿" for ch in t)
                   for t in kws)

    def test_empty_returns_empty(self):
        assert pde.extract_task_keywords({}) == set()
        assert pde.extract_task_keywords({"id": "e-x"}) == set()
        assert pde.extract_task_keywords(None) == set()  # type: ignore[arg-type]
        assert pde.extract_task_keywords({"description": "   "}) == set()

    def test_stop_tokens_excluded(self):
        entry = {"description": "fix the bug and add the test"}
        kws = pde.extract_task_keywords(entry)
        # "fix", "add", "the", "and" — all stop tokens
        assert "fix" not in kws
        assert "add" not in kws
        assert "the" not in kws
        assert "and" not in kws
        # "bug" and "test" — stop "tests" is the stop, "test" is also
        # listed; only "bug" should survive.
        assert "bug" in kws


# ---------------------------------------------------------------------------
# collect_recent_commits
# ---------------------------------------------------------------------------


class TestCollectRecentCommits:
    def _build(self, *commits):
        return {
            "milestones": [
                {
                    "id": "ms-1",
                    "entries": list(commits),
                }
            ]
        }

    def test_collects_in_descending_created_at_order(self):
        data = self._build(
            {"id": "e-100", "type": "commit", "description": "first",
             "created_at": "2026-06-01T10:00:00Z",
             "meta": {"hash": "aaa", "message": "first commit"}},
            {"id": "e-101", "type": "commit", "description": "third",
             "created_at": "2026-06-03T10:00:00Z",
             "meta": {"hash": "ccc", "message": "third commit"}},
            {"id": "e-102", "type": "commit", "description": "second",
             "created_at": "2026-06-02T10:00:00Z",
             "meta": {"hash": "bbb", "message": "second commit"}},
        )
        rows = pde.collect_recent_commits(data, limit=10)
        assert [r["id"] for r in rows] == ["e-101", "e-102", "e-100"]
        # text concatenates description + meta.message
        assert "third" in rows[0]["text"] and "third commit" in rows[0]["text"]

    def test_respects_limit(self):
        commits = [
            {"id": f"e-{i}", "type": "commit", "description": f"c{i}",
             "created_at": f"2026-06-{i:02d}T00:00:00Z",
             "meta": {"message": f"m{i}"}}
            for i in range(1, 16)
        ]
        data = self._build(*commits)
        rows = pde.collect_recent_commits(data, limit=5)
        assert len(rows) == 5
        # descending — newest 5 = e-15..e-11
        assert [r["id"] for r in rows] == ["e-15", "e-14", "e-13", "e-12", "e-11"]

    def test_walks_nested_entries(self):
        # commits can live nested under tasks (= the _find_matching_task
        # pattern in core.log_commit). collect_recent_commits must walk in.
        data = {
            "milestones": [
                {
                    "id": "ms-1",
                    "entries": [
                        {
                            "id": "e-task",
                            "type": "task",
                            "description": "parent task",
                            "entries": [
                                {
                                    "id": "e-nested",
                                    "type": "commit",
                                    "description": "nested commit",
                                    "created_at": "2026-06-05T00:00:00Z",
                                    "meta": {"message": "nested"},
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        rows = pde.collect_recent_commits(data)
        assert len(rows) == 1
        assert rows[0]["id"] == "e-nested"

    def test_non_commit_entries_skipped(self):
        data = self._build(
            {"id": "e-1", "type": "task", "description": "not a commit"},
            {"id": "e-2", "type": "commit", "description": "ok",
             "created_at": "2026-06-01T00:00:00Z",
             "meta": {"message": "ok"}},
            {"id": "e-3", "type": "pr", "description": "pr"},
        )
        rows = pde.collect_recent_commits(data)
        assert [r["id"] for r in rows] == ["e-2"]

    def test_falls_back_to_date_when_created_at_missing(self):
        data = self._build(
            {"id": "e-1", "type": "commit", "description": "x",
             "date": "2026-06-01T00:00:00Z",
             "meta": {"message": "x"}},
        )
        rows = pde.collect_recent_commits(data)
        assert rows and rows[0]["created_at"] == "2026-06-01T00:00:00Z"

    def test_empty_input(self):
        assert pde.collect_recent_commits({}) == []
        assert pde.collect_recent_commits({"milestones": []}) == []
        assert pde.collect_recent_commits(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_done_evidence
# ---------------------------------------------------------------------------


class TestEvaluateDoneEvidence:
    def test_id_reference_is_strong_match(self):
        entry = {
            "id": "e-2726",
            "description": "Add phantom done evidence gate",
        }
        commits = [
            {"id": "e-c1", "text": "feat(ms-95): e-2726 — wire gate",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is True
        assert a["match_type"] == "id_reference"
        assert a["matched_commits"][0]["by"] == "id_reference"
        assert a["matched_commits"][0]["score"] == 1.0

    def test_keyword_overlap_above_threshold(self):
        entry = {
            "id": "e-9999",
            "description": "phantom done evidence gate with cloudlogging",
        }
        commits = [
            {"id": "e-c1", "text": "Wire phantom evidence gate; cloudlogging line",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is True
        assert a["match_type"] == "keyword_overlap"
        assert a["matched_commits"][0]["overlap"] >= 2

    def test_no_match_flagged(self):
        # e-710 reproduction: PE-side task with no PE commit.
        entry = {
            "id": "e-710",
            "description": "LPS dev impl 完了",
            "acceptance_criteria": "implementation complete",
        }
        commits = [
            {"id": "e-c1", "text": "chore: bump version",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
            {"id": "e-c2", "text": "docs: update README",
             "created_at": "2026-06-30T00:00:00Z", "hash": "def"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is False
        assert a["match_type"] == "none"
        assert a["matched_commits"] == []

    def test_e2567_drift_reproduction(self):
        # e-2567 reproduction: AC vs implementation drift. The task's AC
        # text was about a broad sweep but the commit only touched
        # ``scope[0]``. Even with some keyword overlap, the score should
        # stay below threshold OR we need a stricter signal — for our
        # purposes the key thing is the *no commit references e-2567*
        # case fires the warning. Use a commit that mentions an entirely
        # different scope.
        entry = {
            "id": "e-2567",
            "description": "implement broad sweep across all scope entries",
            "acceptance_criteria": "iterate every scope item",
        }
        commits = [
            {"id": "e-c1", "text": "fix typo in README",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is False
        assert a["match_type"] == "none"

    def test_empty_keywords_skips_warning(self):
        # Legacy task with no description / AC / motivation — we cannot
        # judge, so we treat as "no warning" (= match_type
        # ``skip_no_keywords``, ``has_evidence`` true).
        entry = {"id": "e-x"}
        commits = [
            {"id": "e-c1", "text": "something",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is True
        assert a["match_type"] == "skip_no_keywords"

    def test_empty_commit_window_flags(self):
        entry = {"id": "e-1", "description": "do the rewrite"}
        a = pde.evaluate_done_evidence(entry, [])
        # task has keywords but no commits → no evidence
        assert a["has_evidence"] is False
        assert a["matched_commits"] == []
        assert a["window"] == 0

    def test_id_partial_does_not_falsely_match(self):
        # Word-boundary anchor — ``e-27`` in commit must NOT match ``e-2726``.
        entry = {"id": "e-2726", "description": "the gate"}
        commits = [
            {"id": "e-c1", "text": "fix e-27 unrelated",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        # No id match (anchored), no keyword overlap → flagged.
        assert a["has_evidence"] is False

    def test_threshold_boundary(self):
        # 1 overlap out of 2 task keywords vs 4 commit keywords = 0.5 → above 0.3.
        entry = {
            "id": "e-1",
            "description": "phantom evidence",
        }
        commits = [
            {"id": "e-c1",
             "text": "rewrite the phantom checker with sleek logic only",
             "created_at": "2026-07-01T00:00:00Z", "hash": "abc"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        # ``phantom`` overlaps; both sides have small sets → score = 1/2 = 0.5
        assert a["has_evidence"] is True
        assert a["matched_commits"][0]["by"] == "keyword_overlap"

    def test_ranks_id_match_above_keyword(self):
        entry = {"id": "e-42", "description": "phantom evidence keyword gate"}
        commits = [
            {"id": "e-c1", "text": "phantom evidence keyword gate happened",
             "created_at": "2026-07-02T00:00:00Z", "hash": "aaa"},
            {"id": "e-c2", "text": "feat: e-42 — initial implementation",
             "created_at": "2026-07-01T00:00:00Z", "hash": "bbb"},
        ]
        a = pde.evaluate_done_evidence(entry, commits)
        assert a["has_evidence"] is True
        assert a["matched_commits"][0]["by"] == "id_reference"
        assert a["matched_commits"][0]["hash"] == "bbb"
