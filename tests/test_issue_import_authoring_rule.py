"""Tests for the issue-import authoring rule (ms-114 e-3743) — the second concrete
server-side authoring rule re-homed behind the report/request seam
(lib/report_primitive.py), after the commit rule (e-3742).

What this slice must prove (leader-approved boundary):
  1. ``core.issue_import_authoring_rule`` is a standalone ``AuthoringRule`` — an
     ``issue_import`` *report* routed through ``apply_report`` authors a task
     entry (SPEC AC4: intake — client reports the raw Issue, server authors the
     record).
  2. The seam is transparent: ``core.issue_import`` (now a report-building
     adapter) returns the SAME entry id and produces the SAME mutation as the
     rule invoked directly — the expand/contract expand step, so the three CLI
     callers are unaffected.
  3. The interpretation (``"#N: title"`` description + the untriaged-priority
     sentinel) is authored SERVER-SIDE — the client that fetched the Issue no
     longer authors that record (SPEC AC5, intake dimension).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402
import report_primitive as rp  # noqa: E402


# --- fixtures --------------------------------------------------------------

def make_project(**kwargs):
    base = {"name": "test", "milestones": []}
    base.update(kwargs)
    return base


def make_ms(ms_id="ms-1", title="Test MS", status="in_progress", entries=None):
    return {
        "id": ms_id, "title": title, "status": status,
        "progress": 0, "target_date": "",
        "entries": entries or [], "commits": [],
    }


def _issue_report(**payload_overrides):
    payload = {
        "number": 42, "url": "https://github.com/o/r/issues/42",
        "title": "Widget crashes on empty input", "body": "steps...",
        "date": "", "ms_id": "ms-1", "created_by": "tester",
    }
    payload.update(payload_overrides)
    return rp.make_report(kind="issue_import", actor="tester", payload=payload,
                          subject=payload.get("ms_id", ""),
                          origin_verb="issue_import")


def _fresh_registry():
    rules = rp.new_authoring_registry()
    rp.register_authoring_rule(rules, "issue_import",
                               core.issue_import_authoring_rule)
    return rules


# --- 1. the rule authors a task via apply_report ---------------------------

def test_apply_report_authors_issue_task():
    """AC4: an issue_import report routed through apply_report authors a task
    entry on the target milestone (the server is the author)."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    outcome = rp.apply_report(data, _issue_report(), rules=_fresh_registry(),
                              seen={})
    assert outcome["status"] == "authored"
    assert outcome["result"]["status"] == "imported"
    assert len(ms["entries"]) == 1
    e = ms["entries"][0]
    assert e["type"] == "task"
    assert e["meta"]["issue_number"] == 42


def test_rule_matches_authoring_signature():
    """The rule is a plain ``(project_data, report) -> result`` and works when
    called directly — a genuinely re-homed pure author."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    result = core.issue_import_authoring_rule(data, _issue_report())
    assert result["status"] == "imported"
    assert result["entry_id"] == ms["entries"][0]["id"]


# --- 2. seam transparency: issue_import == rule-via-apply_report ------------

def test_issue_import_routes_through_seam_same_entry():
    """``issue_import`` (the adapter) and the rule dispatched via apply_report
    produce the same task entry (ignoring the auto-assigned id/timestamps) and
    the adapter returns the entry id as a str — proving the seam is transparent."""
    ms_a = make_ms()
    data_a = make_project(milestones=[ms_a])
    eid = core.issue_import(
        data_a, ms_id="ms-1", number=42,
        url="https://github.com/o/r/issues/42",
        title="Widget crashes on empty input", body="steps...")
    assert isinstance(eid, str)
    assert ms_a["entries"][0]["id"] == eid  # adapter returned the authored id

    ms_b = make_ms()
    data_b = make_project(milestones=[ms_b])
    rp.apply_report(data_b, _issue_report(), rules=_fresh_registry(), seen={})

    ea, eb = ms_a["entries"][0], ms_b["entries"][0]
    # same authored shape (description, meta) via both paths
    assert ea["description"] == eb["description"]
    assert ea["meta"]["issue_number"] == eb["meta"]["issue_number"]
    assert ea["meta"]["priority"] == eb["meta"]["priority"]


# --- 3. interpretation is authored server-side -----------------------------

def test_server_authors_description_interpretation():
    """AC5 (intake dimension): the ``"#N: title"`` description is built by the
    rule, not reported by the client — the client only sends number + title."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    core.issue_import_authoring_rule(
        data, _issue_report(number=7, title="Fix parser"))
    assert ms["entries"][0]["description"] == "#7: Fix parser"


def test_server_stamps_untriaged_priority():
    """The untriaged-priority sentinel is stamped by the server-side rule (a
    machine-path import has no human-judged priority yet, ms-126). The client
    does not author the priority."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    core.issue_import_authoring_rule(data, _issue_report())
    assert ms["entries"][0]["meta"]["priority"] == core.UNTRIAGED_PRIORITY


def test_titleless_issue_description_fallback():
    """No title reported → the rule authors the ``"Issue #N"`` fallback, still
    server-side."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    core.issue_import_authoring_rule(data, _issue_report(number=99, title=""))
    assert ms["entries"][0]["description"] == "Issue #99"


def test_body_becomes_detail_truncated():
    """A reported body is authored into ``detail`` (truncated to 500 chars) by
    the rule — behaviour preserved from the pre-seam implementation."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    core.issue_import_authoring_rule(data, _issue_report(body="x" * 600))
    assert ms["entries"][0]["detail"] == "x" * 500
