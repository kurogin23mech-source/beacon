"""ms-184 — 「期間内に進捗した対象」の導出と、期間フィルタの穴 (e-7015 / e-7016).

What these pin
==============
Two things that were measurably wrong on 2026-10-15 against this very project:

1. **The period filter folded "no date" into "matches"** (e-7016). Asking
   ``--from 2020-01-01 --to 2020-01-02`` and asking for yesterday both
   returned the SAME 25 milestones — the ones whose ``created_at`` is an empty
   string — while the milestone that actually took 8 commits yesterday was in
   neither answer. A date-only upper bound also dropped everything stamped
   with a time on that day.
2. **"Which Targets progressed" did not exist** (e-7015); the closest thing
   was "which Targets were CREATED in the period", a different question.

Every test here is written so that reverting the fix turns it RED, and that
claim is itself checked: ``test_inversion_harness_is_wired`` runs
``scripts/invert-ms184-guards.py``, which re-breaks each fix in a scratch copy
and fails if the matching test still passes. A guard nobody has watched fail
is not yet a guard (this project has shipped three false-green guards for
exactly that reason).
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys

import pytest

import date_window as dw
import progress_window as pw
import search


# ---------------------------------------------------------------------------
# Fixtures — hand-built projects, never the repo's own .beacon/project.json
# (a test that reads live data changes answer as work lands).
# ---------------------------------------------------------------------------

def _dev_project() -> dict:
    """A dev project with the three shapes that broke the old filter."""
    return {
        "project_id": "p-test",
        "name": "Test",
        "milestones": [
            {
                # The 2026-10-15 false positive: a long-finished Target whose
                # own created_at is an empty string.
                "id": "ms-1", "title": "古い完了済", "status": "done",
                "created_at": "", "entries": [
                    {"id": "e-1", "type": "commit", "description": "ancient",
                     "created_at": ""},
                ],
            },
            {
                # The 2026-10-15 false negative: created long ago, moved
                # yesterday. Must be RETURNED for yesterday.
                "id": "ms-2", "title": "昨日動いた", "status": "in_progress",
                "created_at": "2026-01-05T00:00:00Z",
                "entries": [
                    {"id": "e-2", "type": "commit", "description": "fix a",
                     "created_at": "2026-10-14"},
                    {"id": "e-3", "type": "task", "description": "work item",
                     "created_at": "2026-05-01T00:00:00Z",
                     "done_at": "2026-10-14T09:00:00Z"},
                    {"id": "e-4", "type": "pr", "description": "old pr",
                     "created_at": "2026-06-01"},
                ],
            },
            {
                # Created yesterday but with nothing attached. Per 受入条件 1
                # the Target's OWN creation date is not evidence, so this must
                # NOT be returned.
                "id": "ms-3", "title": "昨日作っただけ", "status": "todo",
                "created_at": "2026-10-14T10:00:00Z", "entries": [],
            },
        ],
        "operations": [
            {
                # Operation declares no arms (ms-142 T1) yet physically carries
                # entries — the fallback must still see this.
                "id": "op-1", "title": "運用", "status": "open",
                "entries": [
                    {"id": "e-9", "type": "run", "description": "daily run",
                     "created_at": "2026-10-14T22:00:00Z"},
                ],
            },
        ],
    }


def _sales_project() -> dict:
    """A sales project: evidence lives on activities / communications."""
    return {
        "project_id": "p-sales", "name": "Sales", "profession": "sales",
        "opportunities": [
            {
                "id": "opp-1", "label": "A社 商談", "phase": "proposal",
                "activities": [
                    {"id": "act-1", "description": "提案書送付",
                     "created_at": "2026-10-14T03:00:00Z"},
                ],
                "communications": [],
            },
            {
                "id": "opp-2", "label": "B社 商談", "phase": "lead",
                "activities": [
                    {"id": "act-9", "description": "初回打診",
                     "created_at": "2026-02-01T03:00:00Z"},
                ],
            },
        ],
        "accounts": [], "milestones": [], "operations": [],
    }


YESTERDAY = ("2026-10-14", "2026-10-14")
ANCIENT = ("2020-01-01", "2020-01-02")


# ---------------------------------------------------------------------------
# e-7016 — the period filter is three-valued
# ---------------------------------------------------------------------------

def test_absent_timestamp_is_undated_not_a_match():
    """The exact bug: an empty timestamp used to satisfy every window."""
    assert dw.classify("", *YESTERDAY) == dw.UNDATED
    assert dw.classify("", *ANCIENT) == dw.UNDATED
    # ... but with NO window asked for, an undated record is not excluded:
    # asking no period question cannot fail to be answered.
    assert dw.classify("", "", "") == dw.IN_WINDOW


def test_date_only_upper_bound_covers_the_whole_day():
    """``--to 2026-10-14`` must include 2026-10-14T10:00:00Z.

    Lexicographic comparison made the timestamped record GREATER than the bare
    bound, so a day's worth of work vanished from any query that ended on that
    day.
    """
    assert dw.classify("2026-10-14T10:00:00Z", "2026-10-14", "2026-10-14") \
        == dw.IN_WINDOW
    assert dw.classify("2026-10-14T23:59:59.999999Z",
                       "2026-10-14", "2026-10-14") == dw.IN_WINDOW
    # The boundary still EXCLUDES the neighbouring days.
    assert dw.classify("2026-10-15T00:00:00Z", "2026-10-14", "2026-10-14") \
        == dw.OUT_OF_WINDOW
    assert dw.classify("2026-10-13T23:59:59Z", "2026-10-14", "2026-10-14") \
        == dw.OUT_OF_WINDOW


def test_classify_any_needs_only_one_real_stamp():
    """A record stamped on one field is judgeable even if others are blank."""
    assert dw.classify_any(["", "2026-10-14T01:00:00Z"], *YESTERDAY) \
        == dw.IN_WINDOW
    assert dw.classify_any(["", ""], *YESTERDAY) == dw.UNDATED
    assert dw.classify_any(["2026-01-01", ""], *YESTERDAY) == dw.OUT_OF_WINDOW


def test_search_excludes_undated_and_reports_how_many():
    """``search`` must stop returning undated records for every period AND say
    how many it could not judge — dropping them silently is indistinguishable
    from "nothing happened" (方針 8)."""
    project = _dev_project()
    ancient = search.search_project(project, [], type=["milestone"],
                                    from_date=ANCIENT[0], to_date=ANCIENT[1])
    assert ancient["total"] == 0, \
        "an undated milestone must not match a 2020 window"
    assert ancient["undated"]["excluded"] == 1
    assert ancient["undated"]["by_type"] == {"milestone": 1}

    # The two windows used to return the IDENTICAL set. They must now differ.
    recent = search.search_project(project, [], type=["milestone"],
                                   from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    assert [r["id"] for r in ancient["results"]] != [r["id"] for r in recent["results"]] \
        or recent["total"] == 0 and ancient["total"] == 0

    # With no window, nothing is excluded and the key is still present, so a
    # consumer can read it unconditionally.
    unfiltered = search.search_project(project, [], type=["milestone"])
    assert unfiltered["undated"] == {"excluded": 0, "by_type": {}}
    assert unfiltered["total"] == 3


def test_search_window_covers_the_whole_end_day():
    """A commit dated with a time on the --to day must still be found."""
    project = {"project_id": "p", "milestones": [
        {"id": "ms-1", "title": "m", "status": "todo", "created_at": "2026-01-01",
         "entries": [{"id": "e-1", "type": "commit", "description": "x",
                      "created_at": "2026-10-14T18:30:00Z"}]}]}
    res = search.search_project(project, [], type=["commit"],
                               from_date="2026-10-14", to_date="2026-10-14")
    assert res["total"] == 1, "a timestamped commit fell outside its own day"


# ---------------------------------------------------------------------------
# e-7015 — progress is derived from evidence, not from the Target's own date
# ---------------------------------------------------------------------------

def test_derive_returns_the_target_that_moved_not_the_one_created():
    result = pw.derive(_dev_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    ids = [t["target_id"] for t in result["targets"]]
    assert "ms-2" in ids, "the Target that took commits yesterday must be returned"
    assert "ms-3" not in ids, (
        "a Target created in the window with nothing attached is not progress "
        "(受入条件 1: the Target's own creation date is not evidence)")
    assert "ms-1" not in ids, "a long-finished Target must not be returned"


def test_derive_counts_each_in_window_moment_of_a_record():
    """A work item raised before the window and finished inside it counts, and
    is dated by the completion — the moment that belongs to the window."""
    result = pw.derive(_dev_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    ms2 = next(t for t in result["targets"] if t["target_id"] == "ms-2")
    assert ms2["evidence_by_kind"] == {"commit": 1, "task": 1}, \
        "the out-of-window PR must not count; the done-yesterday task must"
    task = next(e for e in ms2["evidence"] if e["kind"] == "task")
    assert task["at"] == "2026-10-14T09:00:00Z"


def test_derive_returns_nothing_for_an_unrelated_period():
    """受入条件 3: an unrelated past window is 0, not 'everything undated'."""
    result = pw.derive(_dev_project(), from_date=ANCIENT[0], to_date=ANCIENT[1])
    assert result["targets"] == []
    assert result["moved"] is False
    assert result["target_count"] == 0


def test_derive_reports_records_it_could_not_judge():
    """受入条件 6: undated records are counted, not silently dropped."""
    result = pw.derive(_dev_project(), from_date=ANCIENT[0], to_date=ANCIENT[1])
    assert result["undated"]["count"] == 1
    assert result["undated"]["by_kind"] == {"commit": 1}


def test_derive_sees_an_operation_whose_arms_are_undeclared():
    """A dev Operation declares no arms (ms-142 T1) but carries run records.

    A strict read of the declaration would drop every Operation's movement, so
    a class with no declared arms falls back to the declaration-derived union
    of child-table names.
    """
    result = pw.derive(_dev_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    ids = [t["target_id"] for t in result["targets"]]
    assert "op-1" in ids, "the Operation's daily run is evidence it moved"


def test_derive_reaches_evidence_nested_two_levels_deep():
    """``_walk_arms`` recursion is load-bearing — pin it, do not just claim it.

    A dev Milestone's entry tree nests: a PR entry carries its own ``entries``
    of commits. The module's docstring says the recursion is what reaches
    them, but no fixture nested anything two levels deep, so DELETING the
    recursive call left all 35 tests green (an independent maintainability
    review removed it and measured that). A documented capability with no
    executable proof is undocumented from the next editor's point of view —
    and worse than undocumented, because the docstring invites trust.
    """
    project = {"project_id": "p", "milestones": [{
        "id": "ms-1", "title": "m", "status": "in_progress",
        "created_at": "2026-01-01T00:00:00Z",
        "entries": [{
            "id": "e-pr", "type": "pr", "description": "the PR",
            "created_at": "2026-06-01",              # OUT of window
            "entries": [{
                "id": "e-nested", "type": "commit",
                "description": "commit nested under the PR",
                "created_at": "2026-10-14",          # IN window
            }],
        }],
    }]}
    result = pw.derive(project, from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    assert result["target_count"] == 1, \
        "a commit nested under a PR is still the Target's movement"
    t = result["targets"][0]
    assert [e["id"] for e in t["evidence"]] == ["e-nested"], \
        "the nested commit must be reached; the out-of-window PR must not count"


def test_derive_reaches_evidence_three_levels_deep():
    """Depth is not special-cased at 2 — the walk recurses, it does not unroll."""
    project = {"project_id": "p", "milestones": [{
        "id": "ms-1", "title": "m", "status": "in_progress",
        "created_at": "2026-01-01T00:00:00Z",
        "entries": [{"id": "l1", "type": "pr", "description": "l1",
                     "created_at": "2026-06-01",
                     "entries": [{"id": "l2", "type": "save", "description": "l2",
                                  "created_at": "2026-06-02",
                                  "entries": [{"id": "l3", "type": "commit",
                                               "description": "deep",
                                               "created_at": "2026-10-14"}]}]}],
    }]}
    result = pw.derive(project, from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    assert [e["id"] for e in result["targets"][0]["evidence"]] == ["l3"]


def test_derive_is_occupation_generic_without_naming_an_arm():
    """A sales Opportunity's activity is evidence by the same rule.

    If this needed a profession branch or a hardcoded arm name, declaring a new
    occupation would silently produce an always-empty recap.
    """
    result = pw.derive(_sales_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    ids = [t["target_id"] for t in result["targets"]]
    assert ids == ["opp-1"], f"expected only the moved opportunity, got {ids}"


def test_progress_window_hardcodes_no_arm_or_collection_name():
    """The derivation must not carry a profession concrete in its CODE.

    Checked on the AST (not the text) so the module's prose may discuss
    ``entries`` while the code stays declaration-driven — the same rule
    scripts/check-capability-scope.py applies to the aggregator modules it
    scans. progress_window is NOT in that scanner's population (it does not
    call ``iter_target_records``), so without this test the coupling could
    land unseen.
    """
    import ast
    import capability_ledger as cl

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "lib", "progress_window.py")
    tree = ast.parse(open(src, encoding="utf-8").read())
    concretes = set(cl.PROFESSION_CONCRETE_ARMS) | set(cl.PROFESSION_CONCRETE_COLLECTIONS)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in concretes:
            hits.append(node.value)
    assert hits == [], (
        f"lib/progress_window.py names profession concretes in code: {hits}. "
        "Read arms through occupation.target_decomposition instead.")


def test_derive_attributes_a_decision_through_its_work_item():
    """A decision referencing a work item belongs to the Target owning it."""
    project = _dev_project()
    decisions = [
        {"decision_id": "dec-1", "what": "採用", "created_at": "2026-10-14T12:00:00Z",
         "related": {"task_id": "e-3"}},
        {"decision_id": "dec-2", "what": "却下", "created_at": "2026-10-14T12:00:00Z",
         "related": {"target_id": "ms-2"}},
        {"decision_id": "dec-3", "what": "無関係", "created_at": "2026-01-01T00:00:00Z",
         "related": {"target_id": "ms-2"}},
    ]
    result = pw.derive(project, from_date=YESTERDAY[0], to_date=YESTERDAY[1],
                       decisions=decisions)
    ms2 = next(t for t in result["targets"] if t["target_id"] == "ms-2")
    assert ms2["evidence_by_kind"].get("decision") == 2


def test_derive_says_which_sources_it_did_not_consult():
    """``decisions=None`` must read as "not looked at", never as "none exist"
    — a zero that came from not looking is the error this MS is about."""
    looked = pw.derive(_dev_project(), from_date=YESTERDAY[0],
                       to_date=YESTERDAY[1], documents=[], decisions=[])
    assert looked["sources_unavailable"] == []
    assert set(looked["sources_consulted"]) == {"entry", "document", "decision"}

    blind = pw.derive(_dev_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    assert blind["sources_unavailable"] == ["document", "decision"]


def test_derive_caps_evidence_rows_without_understating_the_count():
    project = {"project_id": "p", "milestones": [{
        "id": "ms-1", "title": "m", "status": "todo", "created_at": "2026-01-01",
        "entries": [{"id": f"e-{i}", "type": "commit", "description": f"c{i}",
                     "created_at": "2026-10-14"} for i in range(10)]}]}
    result = pw.derive(project, from_date=YESTERDAY[0], to_date=YESTERDAY[1],
                       evidence_limit=3)
    t = result["targets"][0]
    assert len(t["evidence"]) == 3
    assert t["evidence_count"] == 10, "a capped list must not lower the count"


# ---------------------------------------------------------------------------
# e-7017 — the cross-project fold
# ---------------------------------------------------------------------------

def test_aggregate_drops_non_movers_but_keeps_unreadable_projects():
    """受入条件 8: "did not move" and "could not tell" must stay apart."""
    moved = pw.derive(_dev_project(), from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    still = pw.derive(_dev_project(), from_date=ANCIENT[0], to_date=ANCIENT[1])
    out = pw.aggregate([
        {"project_id": "p-moved", "name": "Moved", "result": moved},
        {"project_id": "p-still", "name": "Still", "result": still},
        {"project_id": "p-broken", "name": "Broken", "error": "API error 403"},
    ], from_date=YESTERDAY[0], to_date=YESTERDAY[1])

    assert [p["project_id"] for p in out["projects"]] == ["p-moved"]
    assert out["project_count"] == 1
    assert [u["project_id"] for u in out["unavailable"]] == ["p-broken"]
    assert "403" in out["unavailable"][0]["error"]


def test_aggregate_zero_movers_is_not_an_error():
    out = pw.aggregate([], from_date=YESTERDAY[0], to_date=YESTERDAY[1])
    assert out["projects"] == []
    assert out["project_count"] == 0
    assert out["unavailable"] == []


def test_aggregate_orders_projects_by_most_recent_movement():
    def _res(at):
        return {"moved": True, "target_count": 1, "evidence_count": 1,
                "last_evidence_at": at, "targets": [
                    {"target_id": "ms-1", "target_kind": "milestone",
                     "label": "l", "status": "todo", "evidence_count": 1,
                     "last_evidence_at": at}]}
    out = pw.aggregate([
        {"project_id": "old", "name": "old", "result": _res("2026-10-14T01:00:00Z")},
        {"project_id": "new", "name": "new", "result": _res("2026-10-14T20:00:00Z")},
    ])
    assert [p["project_id"] for p in out["projects"]] == ["new", "old"]


def test_aggregate_carries_the_caveats_of_projects_it_dropped():
    """A project omitted as a non-mover may only LOOK still.

    If its decision stream was unreadable, "did not move" is a guess. The
    fold must keep that caveat even though it drops the project, or the
    breadth answer — the one the headline question hits — silently converts
    "could not tell" into "nothing happened" (受入条件 8).

    This is the second consumer of the signal ``derive`` emits. Wiring only
    the first one is the failure shape this milestone exists to remove, and
    it was the state of this code until self-review caught it.
    """
    # A project with NO in-window evidence, whose decision stream was unreadable.
    blind_still = pw.derive(_dev_project(), from_date=ANCIENT[0],
                            to_date=ANCIENT[1], documents=[], decisions=None)
    assert blind_still["moved"] is False
    assert blind_still["sources_unavailable"] == ["decision"]

    out = pw.aggregate([
        {"project_id": "p-still", "name": "Still", "result": blind_still},
    ], from_date=ANCIENT[0], to_date=ANCIENT[1])

    assert out["projects"] == [], "it still must not be listed as a mover"
    assert out["sources_unavailable"] == ["decision"], (
        "the dropped project's unread evidence source vanished from the "
        "cross-project answer")
    assert out["undated"]["count"] == blind_still["undated"]["count"] > 0, \
        "the dropped project's unjudgeable records vanished from the total"


def test_aggregate_reports_no_caveats_when_everything_was_readable():
    """The caveat lines must be ABSENT in the clean case.

    A warning that is always present is a warning nobody reads — and this
    project has the receipts (a notification queue that reached 102 entries,
    70 of them dead).
    """
    clean = pw.derive(_dev_project(), from_date="2026-10-14", to_date="2026-10-14",
                      documents=[], decisions=[])
    project = {"project_id": "p", "milestones": [{
        "id": "ms-1", "title": "m", "status": "todo",
        "created_at": "2026-01-01T00:00:00Z",
        "entries": [{"id": "e-1", "type": "commit", "description": "x",
                     "created_at": "2026-10-14"}]}]}
    spotless = pw.derive(project, from_date="2026-10-14", to_date="2026-10-14",
                         documents=[], decisions=[])
    out = pw.aggregate([
        {"project_id": "p", "name": "P", "result": spotless},
    ], from_date="2026-10-14", to_date="2026-10-14")
    assert out["sources_unavailable"] == []
    assert out["undated"]["count"] == 0
    assert clean["sources_unavailable"] == []


# ---------------------------------------------------------------------------
# 受入条件 7 — the period spec never makes a person compute a date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("today", ("2026-10-15", "2026-10-15")),
    ("yesterday", ("2026-10-14", "2026-10-14")),
    ("昨日", ("2026-10-14", "2026-10-14")),
    ("3d", ("2026-10-13", "2026-10-15")),
    ("this-week", ("2026-10-12", "2026-10-15")),
    ("last-week", ("2026-10-05", "2026-10-11")),
    ("this-month", ("2026-10-01", "2026-10-15")),
    ("2026-09-30", ("2026-09-30", "2026-09-30")),
    ("2026-09-01..2026-09-30", ("2026-09-01", "2026-09-30")),
])
def test_parse_since_accepts_natural_periods(spec, expected):
    assert dw.parse_since(spec, today=datetime.date(2026, 10, 15)) == expected


@pytest.mark.parametrize("bad", ["", "lastweek", "3x", "0d",
                                  "2026-13-99..2026-01-01",
                                  "2026-10-10..2026-10-01"])
def test_parse_since_refuses_rather_than_substituting_a_window(bad):
    """A silently-substituted period returns a confident wrong answer, which
    the caller has no way to notice from the output."""
    with pytest.raises(ValueError):
        dw.parse_since(bad, today=datetime.date(2026, 10, 15))


# ---------------------------------------------------------------------------
# The guards are watched failing (ms-184: 偽の緑を自分で作らない)
# ---------------------------------------------------------------------------

def test_inversion_harness_is_wired():
    """Re-break each fix in a scratch copy; fail if its test still passes.

    Skipped nowhere: a SKIP here reads green and would hide exactly the thing
    the harness exists to prove.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "scripts", "invert-ms184-guards.py")
    assert os.path.isfile(script), f"missing inversion harness: {script}"
    proc = subprocess.run([sys.executable, script],
                          cwd=root, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (
        "inversion harness reports a fix that can be reverted without any test "
        f"going red:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}")
