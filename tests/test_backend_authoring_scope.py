"""Tests for the backend work-unit scope gate on the report seam (ms-114 e-3745)
— connecting server-side authoring (report_primitive) to the ms-113 principal
scope model (principal.backend_work_unit_effective_scope).

What this slice must prove:
  1. ``apply_report``'s scope gate: with ``authorizing_scope`` given, a report
     targeting a project outside it is refused (``out_of_scope``), the rule never
     runs, and no dedup slot is consumed. In-scope authors normally.
  2. ``authorizing_scope=None`` (the local/CLI default) skips the gate — local
     mode is unaffected (AC6).
  3. ``backend_authoring.apply_report_as_backend`` composes the principal scope
     computation with the gate: a backend authors only within min(service grant,
     declared work-unit, originating scope) — a wide work-unit declaration is
     clamped by a narrow ceiling / originating scope (confused-deputy defense).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import backend_authoring  # noqa: E402
import principal  # noqa: E402
import report_primitive as rp  # noqa: E402


# --- helpers ---------------------------------------------------------------

def _project():
    return {"name": "t", "milestones": [
        {"id": "ms-1", "title": "T", "status": "in_progress",
         "progress": 0, "entries": [], "commits": []}]}


def _rule(project_data, report):
    """A trivial authoring rule that records it ran (so 'never ran' is observable)."""
    project_data.setdefault("_authored", []).append(report.get("kind"))
    return {"status": "authored-by-rule"}


def _registry():
    rules = rp.new_authoring_registry()
    rp.register_authoring_rule(rules, "commit", _rule)
    return rules


def _report():
    return rp.make_report(kind="commit", actor="a", payload={"hash": "h1"})


# --- 1. scope gate in apply_report -----------------------------------------

def test_in_scope_authors():
    data = _project()
    out = rp.apply_report(data, _report(), rules=_registry(), seen={},
                          authorizing_scope={"proj-A", "proj-B"},
                          target_project_id="proj-A")
    assert out["status"] == "authored"
    assert data.get("_authored") == ["commit"]  # rule ran


def test_out_of_scope_refused_and_rule_never_runs():
    data = _project()
    seen = {}
    out = rp.apply_report(data, _report(), rules=_registry(), seen=seen,
                          authorizing_scope={"proj-A"},
                          target_project_id="proj-EVIL")
    assert out["status"] == "out_of_scope"
    assert out["target_project_id"] == "proj-EVIL"
    assert out["authorizing_scope"] == ["proj-A"]
    assert "_authored" not in data          # rule never ran, no mutation
    assert seen == {}                       # no dedup slot consumed (retry can author)


def test_none_scope_skips_gate_local_mode_unaffected():
    """authorizing_scope=None (local/CLI default) → gate skipped, authors as before
    (AC6: local mode unaffected)."""
    data = _project()
    out = rp.apply_report(data, _report(), rules=_registry(), seen={})
    assert out["status"] == "authored"
    assert data.get("_authored") == ["commit"]


def test_empty_scope_refuses_everything():
    """An empty authorizing_scope (a backend granted nothing) authors into no
    project — the safest default, not an open door."""
    data = _project()
    out = rp.apply_report(data, _report(), rules=_registry(), seen={},
                          authorizing_scope=set(), target_project_id="proj-A")
    assert out["status"] == "out_of_scope"
    assert "_authored" not in data


# --- 2. backend composition seam -------------------------------------------

def _svc(grant):
    return principal.make_service_identity("svc-1", grant, org_id="org-1")


def test_backend_authors_within_effective_scope():
    data = _project()
    out = backend_authoring.apply_report_as_backend(
        data, _report(), rules=_registry(), seen={},
        service_identity=_svc(["proj-A", "proj-B"]),
        work_unit_scope=["proj-A"], originating_scope=["proj-A", "proj-B"],
        target_project_id="proj-A")
    assert out["status"] == "authored"
    assert data.get("_authored") == ["commit"]


def test_backend_confused_deputy_clamped_by_ceiling():
    """A wide work-unit declaration is clamped by a narrow service grant: the
    backend declares proj-A+proj-B but the service is only granted proj-A, so
    authoring into proj-B is refused (scope can't be escalated)."""
    data = _project()
    out = backend_authoring.apply_report_as_backend(
        data, _report(), rules=_registry(), seen={},
        service_identity=_svc(["proj-A"]),                 # ceiling = {A}
        work_unit_scope=["proj-A", "proj-B"],              # declared wide
        originating_scope=["proj-A", "proj-B"],
        target_project_id="proj-B")                        # outside min → refused
    assert out["status"] == "out_of_scope"
    assert "_authored" not in data


def test_backend_clamped_by_originating_scope():
    """Even with a broad grant + work-unit, a narrow originating principal scope
    clamps the effective set — a strong backend can't be used as a stepping stone
    beyond what the caller could reach (confused-deputy defense)."""
    data = _project()
    out = backend_authoring.apply_report_as_backend(
        data, _report(), rules=_registry(), seen={},
        service_identity=_svc(["proj-A", "proj-B", "proj-C"]),
        work_unit_scope=["proj-A", "proj-B", "proj-C"],
        originating_scope=["proj-A"],                      # caller only reaches A
        target_project_id="proj-B")
    assert out["status"] == "out_of_scope"


def test_backend_malformed_service_identity_raises():
    """A service_identity not built via make_service_identity is rejected at the
    principal layer's entry (no silent zero-grant no-op)."""
    import pytest
    data = _project()
    with pytest.raises(ValueError):
        backend_authoring.apply_report_as_backend(
            data, _report(), rules=_registry(), seen={},
            service_identity={"bogus": True}, work_unit_scope=["proj-A"],
            originating_scope=["proj-A"], target_project_id="proj-A")
