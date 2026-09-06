"""Decision-capture coverage checker (ms-166 e-5974).

An axis ORTHOGONAL to the ms-163 completion-seam checks: those police whether every
terminable target-CLASS reaches the 完遂 decision producer; THIS one polices whether every
judgment-SEAM decision KIND (task-done / review-adjudication / completion-verdict / halt /
dm-send / pr-intent 導出 …) actually has a producer that is WIRED. A kind declared in the
SSOT vocabulary but produced by nothing = the "配線はあるが silent に produce しない"
non-function ms-166 targets (the旗艦 decision arm's read-window mismatch e-5970 was the same
family; this is the mechanical detector that stops it regressing).

Load-bearing (AC7 — the checker DETECTS an unwired kind before a fix and is clean after):
  - ``test_decision_capture_clean_on_real_code`` proves every judgment-seam kind's producer
    is wired today (green = "clean", not "checker asleep").
  - ``test_decision_capture_detects_unwired_kind_synthetically`` proves a kind whose producer
    token is invoked/registered NOWHERE is surfaced as a ``new_violation`` (deterministic
    synthetic token, so the detection proof does not depend on real code being broken).
  - ``test_decision_capture_covers_known_kinds`` forces every ``KNOWN_DECISION_KIND`` to be
    either produced (in DECISION_CAPTURE_PRODUCERS) or explicitly seam-less (in
    DECISION_CAPTURE_BOUNDARY), so a new kind cannot enter the vocabulary silently.
  - ``test_no_stale_decision_capture_gap`` forces an allowlist row's deletion once wired (the
    ratchet cannot rot into a lie — same discipline as KNOWN_COMPLETION_SEAM_GAP).
"""
from __future__ import annotations

import ast
import glob
import importlib.util
import os

# sys.path (lib / scripts / tests) is centralized in tests/conftest.py (ms-142 e-5144).
import capability_ledger as cl  # noqa: E402

# decision_event lives under server/ — add it so we can pin the SSOT vocabulary agreement.
import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import decision_event as de  # noqa: E402

_REPO = os.path.join(os.path.dirname(__file__), "..")
_CHK_PATH = os.path.join(_REPO, "scripts", "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def _all_defined_functions_broad() -> set:
    """Every function name defined across lib/*.py + server/*.py (broader than the
    completion-scan population, so a producer defined in a leaf module like
    decision_derive.py is still found)."""
    names: set = set()
    for pat in ("lib/*.py", "server/*.py"):
        for path in glob.glob(os.path.join(_REPO, pat)):
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
    return names


# --- registry honesty (a rename cannot rot the coverage table into a false pass) ---

def test_decision_capture_producers_resolve_to_real_functions():
    defined = _all_defined_functions_broad()
    declared = set()
    for producers in cl.DECISION_CAPTURE_PRODUCERS.values():
        declared |= set(producers)
    missing = sorted(p for p in declared if p not in defined)
    assert not missing, (
        "DECISION_CAPTURE_PRODUCERS names a function that does not exist "
        f"(rename → registry rot): {missing}")


def test_decision_capture_covers_known_kinds():
    # SSOT agreement (like test_ratchet_family_tables_agree): every KNOWN_DECISION_KIND is
    # either produced (has a DECISION_CAPTURE_PRODUCERS row) or explicitly declared seam-less
    # (DECISION_CAPTURE_BOUNDARY). So a kind added to the vocabulary without wiring a producer
    # — or declaring it conversational-only — fails HERE with a clear message.
    covered = set(cl.DECISION_CAPTURE_PRODUCERS) | set(cl.DECISION_CAPTURE_BOUNDARY)
    uncovered = sorted(k for k in de.KNOWN_DECISION_KINDS if k not in covered)
    assert not uncovered, (
        "KNOWN_DECISION_KIND(s) with neither a wired producer nor a boundary declaration "
        f"(wire a producer in DECISION_CAPTURE_PRODUCERS, or declare seam-less in "
        f"DECISION_CAPTURE_BOUNDARY): {uncovered}")


def test_decision_capture_producers_and_boundary_disjoint():
    # A kind is either seam-full (has a producer) or seam-less (boundary) — never both.
    both = sorted(set(cl.DECISION_CAPTURE_PRODUCERS) & set(cl.DECISION_CAPTURE_BOUNDARY))
    assert not both, f"kind declared BOTH produced and seam-less: {both}"


# --- clean today (green = clean, not asleep) ---

def test_decision_capture_clean_on_real_code():
    # every judgment-seam kind's producer is wired (calls for builders, dispatch-registration
    # for the CLI verb handler) → ZERO gaps on real code.
    assert chk.find_decision_capture_gaps() == []
    result = chk.run()
    assert result["new_decision_capture"] == []
    assert result["pending_decision_capture"] == []


# --- detection proof (synthetic, so it does not depend on real code being broken) ---

def test_decision_capture_detects_unwired_kind_synthetically(monkeypatch):
    # Add a phantom kind whose producer token is invoked/registered NOWHERE. The checker MUST
    # surface exactly it as a new_violation (and keep the real kinds clean). Deterministic —
    # a green real run therefore means "wired", not "checker asleep".
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "phantom-seam": frozenset({"produce_phantom_decision_nonexistent"})})
    gaps = chk.find_decision_capture_gaps()
    assert [g["kind"] for g in gaps] == ["phantom-seam"]
    assert gaps[0]["status"] == "new_violation"
    # run() must fail overall on a fresh unwired kind (it gates ok=).
    assert chk.run()["ok"] is False


def test_fresh_decision_capture_gap_is_new_not_pending(monkeypatch):
    # A gap NOT in the ratchet allowlist is a new_violation (fails CI), not silently accepted.
    monkeypatch.setattr(cl, "KNOWN_DECISION_CAPTURE_GAP", frozenset())
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "unlisted-seam": frozenset({"produce_unlisted_nonexistent"})})
    gaps = {g["kind"]: g["status"] for g in chk.find_decision_capture_gaps()}
    assert gaps.get("unlisted-seam") == "new_violation"


def test_allowlisted_gap_is_pending_not_failing(monkeypatch):
    # Symmetric: an unwired kind that IS allowlisted is reported as pending_debt (does not fail
    # ok=), so the ratchet mechanism itself works when a real temporary gap is accepted.
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "temp-seam": frozenset({"produce_temp_nonexistent"})})
    monkeypatch.setattr(cl, "KNOWN_DECISION_CAPTURE_GAP", frozenset({"temp-seam"}))
    gaps = {g["kind"]: g["status"] for g in chk.find_decision_capture_gaps()}
    assert gaps.get("temp-seam") == "pending_debt"
    result = chk.run()
    assert any(g["kind"] == "temp-seam" for g in result["pending_decision_capture"])
    assert all(g["kind"] != "temp-seam" for g in result["new_decision_capture"])


def test_no_stale_decision_capture_gap():
    # The ratchet cannot lie: every allowlisted kind must ACTUALLY be an unwired gap on real
    # code (a kind whose producer got wired must be DROPPED from the allowlist). Empty today,
    # so this holds vacuously — but it fails the moment a stale row outlives its fix.
    real_gap_kinds = {g["kind"] for g in chk.find_decision_capture_gaps()}
    stale = sorted(k for k in cl.KNOWN_DECISION_CAPTURE_GAP if k not in real_gap_kinds)
    assert not stale, (
        "KNOWN_DECISION_CAPTURE_GAP lists a kind that is no longer an unwired gap "
        f"(producer got wired — drop the row): {stale}")
