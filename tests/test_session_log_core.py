"""Unit tests for lib/session_log.py — aggregation core (ms-57 / e-1037).

Locks down the contract shared by session-end (e-1038) and rescue
(e-1039):

* ``collect_project_entries`` filters commits/PRs by ``meta.session_id``
  and recurses into nested entry trees.
* ``collect_local_notes`` reads jsonl, filters by ``session_id``,
  tolerates malformed lines.
* ``mechanical_summary`` produces a deterministic fallback string.
* ``aggregate_session`` builds a payload with the correct schema,
  honors ``summary_override``, and applies the recovered-flag
  preservation semantics (set on first rescue write, never flipped).

We do NOT test the FastAPI plumbing here — that lives in the
broader integration suite and would require a Firestore stub.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session_log  # noqa: E402


# ---------------------------------------------------------------------------
# collect_project_entries
# ---------------------------------------------------------------------------

def _proj_with_entries(*entries) -> dict:
    return {"milestones": [{"id": "ms-57", "entries": list(entries)}]}


def test_collect_project_entries_filters_commits_by_session_id():
    data = _proj_with_entries(
        {"id": "e-1", "type": "commit", "meta": {"session_id": "S1", "message": "a"}},
        {"id": "e-2", "type": "commit", "meta": {"session_id": "S2", "message": "b"}},
        {"id": "e-3", "type": "commit", "meta": {"session_id": "S1", "message": "c"}},
    )
    out = session_log.collect_project_entries(data, "S1")
    assert out["commit_ids"] == ["e-1", "e-3"]
    assert out["commit_texts"] == ["a", "c"]
    assert out["pr_ids"] == []


def test_collect_project_entries_filters_prs_and_uses_description():
    data = _proj_with_entries(
        {"id": "e-1", "type": "pr", "description": "PR title 1",
         "meta": {"session_id": "S1", "url": "https://x/pull/1"}},
        {"id": "e-2", "type": "pr", "description": "PR title 2",
         "meta": {"session_id": "S2"}},
    )
    out = session_log.collect_project_entries(data, "S1")
    assert out["pr_ids"] == ["e-1"]
    assert out["pr_texts"] == ["PR title 1"]


def test_collect_project_entries_recurses_nested_entries():
    """PR entries can carry child commit entries via core.pr_add. The
    aggregation must see those nested commits too."""
    data = _proj_with_entries({
        "id": "e-1", "type": "pr", "meta": {"session_id": "S1"},
        "entries": [
            {"id": "e-2", "type": "commit", "meta": {"session_id": "S1", "message": "nested"}},
            {"id": "e-3", "type": "commit", "meta": {"session_id": "S2", "message": "other-sess"}},
        ],
    })
    out = session_log.collect_project_entries(data, "S1")
    assert "e-1" in out["pr_ids"]
    assert out["commit_ids"] == ["e-2"]


def test_collect_project_entries_ignores_entries_without_session_id():
    """Forward-only design: pre-ms-57 entries without meta.session_id
    are simply excluded — they don't show up in aggregation."""
    data = _proj_with_entries(
        {"id": "e-1", "type": "commit", "meta": {"message": "untagged"}},
        {"id": "e-2", "type": "commit", "meta": {"session_id": "S1", "message": "tagged"}},
    )
    out = session_log.collect_project_entries(data, "S1")
    assert out["commit_ids"] == ["e-2"]


def test_collect_project_entries_handles_empty_milestones():
    assert session_log.collect_project_entries({"milestones": []}, "S1") == {
        "commit_ids": [], "commit_texts": [], "pr_ids": [], "pr_texts": [],
    }


def test_collect_project_entries_handles_missing_milestones_key():
    assert session_log.collect_project_entries({}, "S1")["commit_ids"] == []


# ms-108 e-3269 — occupation-neutral Target walk (sales projects have no
# milestones; their Targets are opportunities).

def test_collect_project_entries_walks_opportunities_too():
    """A sales project keeps its Targets under ``opportunities`` (``milestones``
    is empty). Session-scoped entries nested there must still be collected."""
    data = {
        "milestones": [],
        "opportunities": [
            {"id": "opp-1", "entries": [
                {"id": "e-9", "type": "commit",
                 "meta": {"session_id": "S1", "message": "sales-side work"}},
            ]},
        ],
    }
    out = session_log.collect_project_entries(data, "S1")
    assert out["commit_ids"] == ["e-9"]
    assert out["commit_texts"] == ["sales-side work"]


def test_collect_project_entries_sales_without_entries_is_empty_not_crash():
    """A realistic sales project (opportunities carry activities/gates, not
    ``entries``, and no ``milestones``) aggregates to empty without crashing."""
    data = {"opportunities": [{"id": "opp-1", "activities": [], "gates": []}]}
    assert session_log.collect_project_entries(data, "S1") == {
        "commit_ids": [], "commit_texts": [], "pr_ids": [], "pr_texts": [],
    }


# ---------------------------------------------------------------------------
# collect_local_notes
# ---------------------------------------------------------------------------

@pytest.fixture
def beacon_dir():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "session_notes.jsonl").write_text("", encoding="utf-8")
        yield d


def _write_notes(beacon_dir: Path, notes: list[dict]) -> None:
    path = beacon_dir / "session_notes.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for n in notes:
            f.write(json.dumps(n) + "\n")


def test_collect_local_notes_filters_by_session_id(beacon_dir):
    _write_notes(beacon_dir, [
        {"ts": "2026-06-06T10:00:00Z", "text": "n1", "session_id": "S1"},
        {"ts": "2026-06-06T10:01:00Z", "text": "n2", "session_id": "S2"},
        {"ts": "2026-06-06T10:02:00Z", "text": "n3", "session_id": "S1"},
    ])
    out = session_log.collect_local_notes(beacon_dir, "S1")
    assert out["note_ids"] == ["2026-06-06T10:00:00Z", "2026-06-06T10:02:00Z"]
    assert out["note_texts"] == ["n1", "n3"]


def test_collect_local_notes_tolerates_malformed_lines(beacon_dir):
    path = beacon_dir / "session_notes.jsonl"
    path.write_text(
        '{"ts":"2026-06-06T10:00:00Z","text":"good","session_id":"S1"}\n'
        "this is not json\n"
        '{"ts":"2026-06-06T10:01:00Z","text":"also good","session_id":"S1"}\n',
        encoding="utf-8",
    )
    out = session_log.collect_local_notes(beacon_dir, "S1")
    assert out["note_texts"] == ["good", "also good"]


def test_collect_local_notes_handles_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        out = session_log.collect_local_notes(Path(tmp), "S1")
    assert out == {"note_ids": [], "note_texts": []}


def test_collect_local_notes_skips_notes_without_session_id(beacon_dir):
    """Pre-ms-57 notes (no session_id) are not in any session and must
    not contaminate aggregation results."""
    _write_notes(beacon_dir, [
        {"ts": "2026-06-06T10:00:00Z", "text": "untagged"},
        {"ts": "2026-06-06T10:01:00Z", "text": "tagged", "session_id": "S1"},
    ])
    out = session_log.collect_local_notes(beacon_dir, "S1")
    assert out["note_texts"] == ["tagged"]


# ---------------------------------------------------------------------------
# collect_cloud_notes
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self, notes):
        self._notes = notes
        self.calls = 0
    def list_notes(self, project_id):
        self.calls += 1
        return self._notes


def test_collect_cloud_notes_filters_by_session_id():
    client = _StubClient([
        {"ts": "t1", "text": "a", "session_id": "S1"},
        {"ts": "t2", "text": "b", "session_id": "S2"},
    ])
    out = session_log.collect_cloud_notes(client, "proj-1", "S1")
    assert out["note_texts"] == ["a"]
    assert client.calls == 1


def test_collect_cloud_notes_swallows_client_errors():
    class _Broken:
        def list_notes(self, project_id):
            raise RuntimeError("boom")
    out = session_log.collect_cloud_notes(_Broken(), "p", "S1")
    assert out == {"note_ids": [], "note_texts": []}


# ---------------------------------------------------------------------------
# mechanical_summary
# ---------------------------------------------------------------------------

def test_mechanical_summary_counts_and_joins():
    s = session_log.mechanical_summary(
        note_texts=["note text"],
        commit_texts=["commit msg"],
        pr_texts=["pr title"],
    )
    assert "1 notes" in s
    assert "1 commits" in s
    assert "1 PRs" in s
    assert "note text" in s
    assert "commit msg" in s
    assert "pr title" in s


def test_mechanical_summary_no_entries():
    s = session_log.mechanical_summary(note_texts=[], commit_texts=[], pr_texts=[])
    assert s == "no entries"


def test_mechanical_summary_truncates_at_max_chars():
    s = session_log.mechanical_summary(
        note_texts=["x" * 1000],
        commit_texts=[], pr_texts=[],
        max_chars=100,
    )
    assert "…" in s
    # header "1 notes: " is 9 chars, body trimmed to 99 chars + ellipsis
    assert len(s) <= 110


# ---------------------------------------------------------------------------
# aggregate_session
# ---------------------------------------------------------------------------

def test_aggregate_session_builds_full_payload(beacon_dir):
    _write_notes(beacon_dir, [
        {"ts": "t1", "text": "n1", "session_id": "S1"},
    ])
    data = _proj_with_entries(
        {"id": "e-100", "type": "commit", "meta": {"session_id": "S1", "message": "c1"}},
        {"id": "e-101", "type": "pr", "description": "p1", "meta": {"session_id": "S1"}},
    )
    payload = session_log.aggregate_session(
        project_data=data,
        beacon_dir=beacon_dir,
        session_id="S1",
    )
    assert payload["session_id"] == "S1"
    assert payload["note_ids"] == ["t1"]
    assert payload["commit_ids"] == ["e-100"]
    assert payload["pr_ids"] == ["e-101"]
    assert payload["summary"]
    assert payload["last_aggregated_at"]
    assert payload["created_at"]
    assert "recovered" not in payload  # default rescue=False


def test_aggregate_session_uses_summary_override(beacon_dir):
    payload = session_log.aggregate_session(
        project_data={"milestones": []},
        beacon_dir=beacon_dir,
        session_id="S1",
        summary_override="AI-generated narrative",
    )
    assert payload["summary"] == "AI-generated narrative"


def test_aggregate_session_recovered_true_on_first_rescue(beacon_dir):
    payload = session_log.aggregate_session(
        project_data={"milestones": []},
        beacon_dir=beacon_dir,
        session_id="S1",
        recovered=True,
        existing_log=None,
    )
    assert payload["recovered"] is True


def test_aggregate_session_recovered_not_set_when_existing_session_end(beacon_dir):
    """If session-end already produced the entry (no recovered flag),
    a subsequent rescue must NOT add the flag — that would mislabel
    a clean session-end aggregation."""
    payload = session_log.aggregate_session(
        project_data={"milestones": []},
        beacon_dir=beacon_dir,
        session_id="S1",
        recovered=True,  # caller says "I'm rescue"
        existing_log={"session_id": "S1", "summary": "clean", "created_at": "earlier"},
    )
    assert "recovered" not in payload


def test_aggregate_session_recovered_preserved_when_existing_was_recovered(beacon_dir):
    """If a prior rescue created the entry with recovered=True, later
    re-aggregation (rescue OR session-end) must keep the True flag —
    the forensic 'this entry was first born as a rescue' fact doesn't
    become false retroactively."""
    payload = session_log.aggregate_session(
        project_data={"milestones": []},
        beacon_dir=beacon_dir,
        session_id="S1",
        recovered=False,  # caller is session-end this time
        existing_log={"session_id": "S1", "summary": "rescue first", "recovered": True},
    )
    assert payload["recovered"] is True


def test_aggregate_session_idempotent_shape(beacon_dir):
    """Two aggregations of the same session_id with the same inputs
    produce identical id lists. (Timestamps differ, that's expected.)"""
    _write_notes(beacon_dir, [
        {"ts": "t1", "text": "n1", "session_id": "S1"},
    ])
    data = _proj_with_entries(
        {"id": "e-100", "type": "commit", "meta": {"session_id": "S1", "message": "c1"}},
    )
    p1 = session_log.aggregate_session(project_data=data, beacon_dir=beacon_dir, session_id="S1")
    p2 = session_log.aggregate_session(project_data=data, beacon_dir=beacon_dir, session_id="S1")
    assert p1["note_ids"] == p2["note_ids"]
    assert p1["commit_ids"] == p2["commit_ids"]
    assert p1["pr_ids"] == p2["pr_ids"]
    assert p1["summary"] == p2["summary"]


def test_aggregate_session_uses_cloud_client_when_provided(beacon_dir):
    """When cloud_client is given, local jsonl is ignored — cloud is
    authoritative in cloud mode."""
    _write_notes(beacon_dir, [
        {"ts": "local-1", "text": "local note", "session_id": "S1"},
    ])
    client = _StubClient([
        {"ts": "cloud-1", "text": "cloud note", "session_id": "S1"},
    ])
    payload = session_log.aggregate_session(
        project_data={"milestones": []},
        beacon_dir=beacon_dir,
        session_id="S1",
        cloud_client=client,
        cloud_project_id="proj-1",
    )
    assert payload["note_ids"] == ["cloud-1"]
