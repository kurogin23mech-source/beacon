"""Tests for lib/report_primitive.py — the report / request primitives
(ms-114 e-3741). Covers the constructors + required-field enforcement, schema
validation (tolerant of extra keys), idempotency-key derivation, and the
authoring-dispatch skeleton (no_rule / invalid / authored / duplicate), plus the
SPEC AC3 transparency property: an existing write verb can route a report
through the primitive without its canonical mutation changing, and a re-emitted
report is authored exactly once."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import report_primitive as rp  # noqa: E402


# --- constructors ----------------------------------------------------------

def test_make_report_defaults_and_mode():
    r = rp.make_report(kind="commit", actor="alice",
                       payload={"hash": "abc123", "message": "feat: x"})
    assert r["mode"] == rp.REPORT
    assert r["kind"] == "commit"
    assert r["actor"] == "alice"
    assert r["payload"] == {"hash": "abc123", "message": "feat: x"}
    # optional fields default to empty, not missing (stable shape for the server)
    for k in ("subject", "evidence", "occurred_at", "origin_verb", "session_id", "source"):
        assert k in r


def test_make_report_requires_kind_and_actor():
    with pytest.raises(ValueError):
        rp.make_report(kind="", actor="alice", payload={})
    with pytest.raises(ValueError):
        rp.make_report(kind="commit", actor="  ", payload={})


def test_make_report_copies_payload_and_evidence():
    payload = {"hash": "a"}
    evidence = ["a"]
    r = rp.make_report(kind="commit", actor="a", payload=payload, evidence=evidence)
    payload["hash"] = "MUTATED"
    evidence.append("b")
    # builder took a copy — later caller mutation must not leak into the report
    assert r["payload"] == {"hash": "a"}
    assert r["evidence"] == ["a"]


def test_make_request_requires_action_and_actor():
    q = rp.make_request(action="pr_merge", actor="alice", subject="e-1")
    assert q["mode"] == rp.REQUEST
    assert q["action"] == "pr_merge"
    with pytest.raises(ValueError):
        rp.make_request(action="", actor="alice")


# --- schema ----------------------------------------------------------------

def test_describe_schema_is_isolated_copy():
    s = rp.describe_report_schema()
    s["kind"]["required"] = False
    assert rp.REPORT_SCHEMA["kind"]["required"] is True  # module constant intact


def test_report_schema_declares_required_core_fields():
    s = rp.describe_report_schema()
    assert s["kind"]["required"] and s["actor"]["required"] and s["payload"]["required"]
    assert not s["subject"]["required"]  # subject may be resolved by the rule


# --- validation ------------------------------------------------------------

def test_validate_report_ok():
    r = rp.make_report(kind="commit", actor="a", payload={"hash": "x"})
    assert rp.validate_report(r) == []


def test_validate_report_missing_required():
    problems = rp.validate_report({"kind": "commit"})  # no actor / payload
    assert any("actor" in p for p in problems)
    assert any("payload" in p for p in problems)


def test_validate_report_type_mismatch():
    r = {"kind": "commit", "actor": "a", "payload": "not-a-dict"}
    problems = rp.validate_report(r)
    assert any("payload" in p and "object" in p for p in problems)


def test_validate_report_tolerates_extra_keys():
    r = rp.make_report(kind="commit", actor="a", payload={"hash": "x"})
    r["future_field"] = "ignored"  # forward-compat append-only
    assert rp.validate_report(r) == []


def test_validate_report_empty_payload_is_present():
    # an empty dict payload still counts as present (facts may live in subject)
    r = rp.make_report(kind="ping", actor="a", subject="ms-1", payload={})
    assert rp.validate_report(r) == []


# --- idempotency -----------------------------------------------------------

def test_report_key_explicit():
    r = rp.make_report(kind="commit", actor="a", payload={"hash": "x"},
                       idempotency_key="commit-abc")
    assert rp.report_key(r) == "commit-abc"


def test_report_key_derived_is_stable_and_order_independent():
    r1 = rp.make_report(kind="commit", actor="a", subject="ms-1",
                        payload={"hash": "x", "message": "m"})
    r2 = rp.make_report(kind="commit", actor="a", subject="ms-1",
                        payload={"message": "m", "hash": "x"})  # different insert order
    assert rp.report_key(r1) == rp.report_key(r2)


def test_report_key_differs_on_different_facts():
    r1 = rp.make_report(kind="commit", actor="a", payload={"hash": "x"})
    r2 = rp.make_report(kind="commit", actor="a", payload={"hash": "y"})
    assert rp.report_key(r1) != rp.report_key(r2)


# --- authoring dispatch skeleton -------------------------------------------

def test_apply_report_no_rule():
    rules = {}
    r = rp.make_report(kind="commit", actor="a", payload={"hash": "x"})
    out = rp.apply_report({}, r, rules=rules)
    assert out["status"] == "no_rule"


def test_apply_report_invalid():
    out = rp.apply_report({}, {"kind": "commit"}, rules={})
    assert out["status"] == "invalid"
    assert out["problems"]


def test_register_and_authoring_rule_for_isolated_registry():
    rules = {}
    rp.register_authoring_rule("commit", lambda d, r: {"ok": True}, rules=rules)
    assert rp.authoring_rule_for("commit", rules=rules) is not None
    # a different (default) registry is unaffected
    assert rp.authoring_rule_for("commit", rules={}) is None


def test_register_rule_validation():
    with pytest.raises(ValueError):
        rp.register_authoring_rule("", lambda d, r: {}, rules={})
    with pytest.raises(ValueError):
        rp.register_authoring_rule("commit", "not-callable", rules={})


def test_apply_report_authored_and_idempotent():
    """AC3 transparency: a report routed through the primitive produces the same
    canonical mutation an existing write verb would, and re-applying the same
    report authors it exactly once (mirrors `beacon log`'s 'Already logged')."""
    # a stand-in authoring rule that mimics `beacon log`: append a ledger entry.
    def author_commit(data, report):
        entry = {"hash": report["payload"]["hash"], "actor": report["actor"]}
        data.setdefault("log", []).append(entry)
        return entry

    rules = {}
    seen = {}
    rp.register_authoring_rule("commit", author_commit, rules=rules)
    data = {}
    r = rp.make_report(kind="commit", actor="alice", subject="ms-1",
                       payload={"hash": "abc"}, idempotency_key="commit-abc")

    first = rp.apply_report(data, r, rules=rules, seen=seen)
    assert first["status"] == "authored"
    assert data["log"] == [{"hash": "abc", "actor": "alice"}]

    # re-emit the identical report (network retry / re-run) → deduped, no double write
    second = rp.apply_report(data, r, rules=rules, seen=seen)
    assert second["status"] == "duplicate"
    assert data["log"] == [{"hash": "abc", "actor": "alice"}]  # unchanged
