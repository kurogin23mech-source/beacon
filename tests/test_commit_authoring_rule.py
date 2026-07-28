"""Tests for the commit authoring rule (ms-114 e-3742) — the first concrete
server-side authoring rule re-homed from ``beacon log``'s internal logic behind
the report/request seam (lib/report_primitive.py).

What this slice must prove (leader-approved scope boundary):
  1. ``core.commit_authoring_rule`` is a standalone ``AuthoringRule`` — a commit
     *report* routed through ``report_primitive.apply_report`` authors the
     canonical ledger mutations (SPEC AC3: "authoring 段が 1 つ以上動く").
  2. The seam is transparent: ``core.log_commit`` (which now builds a report and
     dispatches through the rule) produces the SAME mutation and result as the
     rule invoked directly — the expand step of the expand/contract migration
     (SPEC 方針7), so stdout/behaviour for the three callers is unchanged.
  3. Task-resolution is authored SERVER-SIDE from the raw commit text — the
     executor no longer authors which task its own commit closes (SPEC AC5,
     task-resolve dimension). Explicit ``resolves`` (a human decision) is honoured
     as a reported field.
  4. Two-layer dedup: the authoritative in-ledger hash scan lives in the rule and
     is independent of the seam's ``report_key``/``seen`` replay ledger
     (e-3742 承認点3).

Out of scope for this slice (asserted so the boundary is explicit): ``progress``
stays a *client-reported* value — its server-side derivation is a later slice.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402
import report_primitive as rp  # noqa: E402


# --- fixtures (mirrors tests/test_core.py) ---------------------------------

def make_project(**kwargs):
    base = {"name": "test", "milestones": []}
    base.update(kwargs)
    return base


def make_ms(ms_id="ms-1", title="Test MS", status="in_progress", progress=0,
            entries=None):
    return {
        "id": ms_id, "title": title, "status": status,
        "progress": progress, "target_date": "",
        "entries": entries or [], "commits": [],
    }


def make_task(eid="e-1", desc="task1"):
    return {
        "id": eid, "type": "task", "description": desc,
        "status": "todo", "date": "2026-05-11",
        "created_at": "2026-05-11", "done_at": None, "meta": {},
    }


def _commit_report(**payload_overrides):
    """Build a well-formed kind="commit" report with the given payload facts."""
    payload = {
        "hash": "abc1234", "message": "Fix bug", "date": "2026-05-11",
        "summary": "", "progress": "", "behavior": "",
        "resolves": "", "resolves_explicit": False,
        "ms_id": "ms-1", "actor": {}, "author": {},
    }
    payload.update(payload_overrides)
    return rp.make_report(kind="commit", actor="tester", payload=payload,
                          subject=payload.get("ms_id", ""), origin_verb="log")


def _fresh_registry():
    rules = rp.new_authoring_registry()
    rp.register_authoring_rule(rules, "commit", core.commit_authoring_rule)
    return rules


# --- 1. the rule authors a commit via apply_report -------------------------

def test_apply_report_authors_commit_entry():
    """AC3: a commit report routed through apply_report authors a commit entry
    on the target milestone (the server is the author)."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    outcome = rp.apply_report(data, _commit_report(), rules=_fresh_registry(),
                              seen={})
    assert outcome["status"] == "authored"
    assert outcome["result"]["status"] == "logged"
    assert len(ms["entries"]) == 1
    assert ms["entries"][0]["type"] == "commit"
    assert ms["entries"][0]["meta"]["hash"] == "abc1234"


def test_rule_matches_authoring_signature():
    """The rule is a plain ``(project_data, report) -> result`` and can be called
    directly (not only through apply_report), so it is genuinely a re-homed pure
    author, not entangled with the dispatcher."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    result = core.commit_authoring_rule(data, _commit_report())
    assert result["status"] == "logged"
    assert result["milestone"] == "ms-1"


# --- 2. seam transparency: log_commit == rule-via-apply_report -------------

def test_log_commit_routes_through_seam_same_result():
    """``log_commit`` (the adapter) and the rule dispatched via apply_report must
    produce identical mutations + results for the same facts — proving the seam
    is transparent (expand/contract expand step)."""
    ms_a = make_ms(entries=[make_task(desc="Fix authentication bug")])
    data_a = make_project(milestones=[ms_a])
    result_a = core.log_commit(
        data_a, ms_id="ms-1", commit_hash="abc1234",
        message="Fix authentication", date="2026-05-11",
    )

    ms_b = make_ms(entries=[make_task(desc="Fix authentication bug")])
    data_b = make_project(milestones=[ms_b])
    outcome_b = rp.apply_report(
        data_b,
        _commit_report(hash="abc1234", message="Fix authentication"),
        rules=_fresh_registry(), seen={},
    )
    result_b = outcome_b["result"]

    # same result dict (ignoring nothing — both took the fuzzy-match path)
    assert result_a == result_b
    # same tree shape: commit nested under the matched task in both
    assert ms_a["entries"][0].get("entries", [])[0]["meta"]["hash"] == "abc1234"
    assert ms_b["entries"][0].get("entries", [])[0]["meta"]["hash"] == "abc1234"


# --- 3. task-resolution is server-authored ---------------------------------

def test_server_authors_task_resolution_from_raw_text():
    """AC5 (task-resolve dimension): the rule resolves which task the commit
    closes from the RAW commit text — the client reported only the fact, the
    server authored the linkage."""
    ms = make_ms(entries=[make_task(eid="e-7", desc="Fix authentication bug")])
    data = make_project(milestones=[ms])
    result = core.commit_authoring_rule(
        data, _commit_report(hash="def5678", message="Fix authentication"))
    assert result["matched_task"] == "e-7"
    # commit nested under the server-resolved task
    assert len(ms["entries"][0].get("entries", [])) == 1


def test_explicit_resolves_is_honoured_as_reported_decision():
    """A human's explicit ``resolves`` (a decision, carried in the report) wins
    over the fuzzy author — the reported decision is authoritative."""
    t1 = make_task(eid="e-1", desc="Fix authentication login flow")
    t2 = make_task(eid="e-2", desc="Refactor database layer")
    ms = make_ms(entries=[t1, t2])
    data = make_project(milestones=[ms])
    result = core.commit_authoring_rule(
        data,
        _commit_report(hash="abc1234", message="Fix authentication",
                       resolves="e-2", resolves_explicit=True),
    )
    assert result["matched_task"] == "e-2"          # reported decision wins
    assert len(t2.get("entries", [])) == 1
    assert len(t1.get("entries", [])) == 0          # NOT the fuzzy pick


# --- 4. two-layer dedup ----------------------------------------------------

def test_in_ledger_hash_dedup_is_authoritative():
    """The authoritative dedup is the in-ledger hash scan inside the rule: a
    second report with a hash already persisted returns ``duplicate`` even with a
    FRESH ``seen`` (so the seam's report_key is not what caught it)."""
    existing = {
        "id": "e-9", "type": "commit", "description": "prior",
        "status": "done", "meta": {"hash": "abc1234", "message": "prior"},
    }
    ms = make_ms(entries=[existing])
    data = make_project(milestones=[ms])
    outcome = rp.apply_report(data, _commit_report(hash="abc1234"),
                              rules=_fresh_registry(), seen={})  # fresh seen
    assert outcome["result"]["status"] == "duplicate"
    assert len(ms["entries"]) == 1  # no new entry authored


def test_report_key_dedup_prevents_double_author_on_replay():
    """The seam's replay layer: re-applying the SAME report object with a shared
    ``seen`` ledger authors exactly once (idempotent replay), independent of the
    in-ledger hash check."""
    ms = make_ms()
    data = make_project(milestones=[ms])
    rules, seen = _fresh_registry(), {}
    report = _commit_report(hash="feed777")
    first = rp.apply_report(data, report, rules=rules, seen=seen)
    second = rp.apply_report(data, report, rules=rules, seen=seen)
    assert first["status"] == "authored"
    assert second["status"] == "duplicate"   # caught by report_key/seen
    assert len(ms["entries"]) == 1


# --- boundary: progress stays a client-reported value (out of scope) -------

def test_progress_is_client_reported_not_derived():
    """Scope boundary marker (leader-approved): this slice does NOT derive
    progress server-side. The rule applies whatever progress the client reported
    verbatim — server-side derivation is a later slice. If a future change starts
    deriving progress, this test should be updated deliberately, not silently."""
    ms = make_ms(progress=10)
    data = make_project(milestones=[ms])
    core.commit_authoring_rule(data, _commit_report(hash="a1", progress="30"))
    assert ms["progress"] == 30          # took the reported value as-is

    ms2 = make_ms(progress=42)
    data2 = make_project(milestones=[ms2])
    core.commit_authoring_rule(data2, _commit_report(hash="a2", progress=""))
    assert ms2["progress"] == 42         # no report → no derivation, unchanged
