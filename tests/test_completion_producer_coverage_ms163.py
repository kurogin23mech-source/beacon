"""Completion-producer coverage checker (ms-163 e-5876 / e-5877 / e-5878).

The reach families (ms-134/142) police the DIRECTION of a dependency; these pin the
ORTHOGONAL axis this MS adds — an L2 dimension produced at 完遂 (deliverable / decision)
must be produced GENERICALLY across every terminable target-class, not hardwired to the
dev milestone.

Load-bearing (AC7 — the checker DETECTS the 混用 before the fix and is clean after):
  - ``test_completion_seam_detects_current_混用`` proves the current gaps (opportunity /
    operation / acquisition / descriptor dropped from decision/deliverable capture) ARE
    surfaced, and milestone — fully wired — is NOT flagged. A green checker run means
    "clean", not "checker asleep".
  - ``test_fresh_seam_gap_fails_when_not_allowlisted`` proves a gap NOT in the ratchet
    allowlist is a ``new_violation`` (fails CI), so a new terminable class cannot silently
    ship without decision/deliverable capture.
  - ``test_no_stale_completion_seam_gap`` forces an allowlist row's deletion once the seam
    is fixed (the ratchet cannot rot into a lie — same discipline as the reach allowlists).
"""

import os

# sys.path (lib / scripts / tests) is centralized in tests/conftest.py (ms-142 e-5144).
import capability_ledger as cl  # noqa: E402
import target_state as _ts  # noqa: E402
import importlib.util  # noqa: E402

_CHK_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def _all_defined_functions() -> set:
    """Every function name defined across the completion-seam scan population (lib CLI
    family + server/*.py)."""
    names = set()
    for _rel, _tree, funcs in chk._load_trees(chk._completion_scan_paths()):
        for name, _s, _e in funcs:
            names.add(name)
    return names


def _all_wired_call_tokens() -> set:
    """Every call token INVOKED anywhere in the completion-seam scan population — used to
    assert a producer is wired (its def may live in a leaf module that is not itself a
    completion seam)."""
    tokens = set()
    for calls in chk._direct_call_tokens(
            chk._load_trees(chk._completion_scan_paths())).values():
        tokens |= calls
    return tokens


# --- registry honesty (a rename cannot rot the coverage matrix into a false pass) ---

def test_completion_terminal_handlers_resolve_to_real_functions():
    defined = _all_defined_functions()
    declared = set()
    for handlers in cl.COMPLETION_TERMINAL_HANDLERS.values():
        declared |= set(handlers)
    declared |= set(cl.DESCRIPTOR_TERMINAL_HANDLERS)
    declared |= set(cl.SHARED_SPINE_TERMINAL_HANDLERS)
    missing = sorted(h for h in declared if h not in defined)
    assert not missing, (
        "COMPLETION_TERMINAL_HANDLERS names a function that does not exist "
        f"(rename → registry rot): {missing}")


def test_every_terminable_class_has_a_terminal_handler_entry():
    # SYNC forcing function (maintainability review): a new terminable class added to
    # target_state.BUILTIN_TARGET_CLASSES MUST get a COMPLETION_TERMINAL_HANDLERS row. This
    # fails EARLY with a clear message naming the missing kind — before the (also-correct but
    # generic) seam-gap new_violation the checker would otherwise emit.
    missing = sorted(kind for kind, _gate in chk._terminable_builtin_classes()
                     if kind not in cl.COMPLETION_TERMINAL_HANDLERS)
    assert not missing, (
        "terminable built-in class(es) with no COMPLETION_TERMINAL_HANDLERS entry "
        f"(add a row + wire its terminal to on_target_completion): {missing}")


def test_producer_call_tokens_are_wired():
    wired = _all_wired_call_tokens()
    for dim, tokens in cl.COMPLETION_PRODUCER_CALLS.items():
        # each dimension's producer must be INVOKED at ≥1 completion seam (the def may live
        # in a leaf module like deliverable_capture.py that is not itself a seam).
        assert any(t in wired for t in tokens), (
            f"no producer for dimension {dim!r} is wired at any completion seam "
            f"(tokens {sorted(tokens)}) — an L2-in-name-only dimension")


def test_terminable_classes_match_target_state_never_terminal():
    # account is never_terminal → excluded; every other built-in with a completion_gate is
    # in the terminable set the seam check walks.
    kinds = {k for k, _g in chk._terminable_builtin_classes()}
    assert "account" not in kinds
    for k in ("milestone", "operation", "opportunity", "acquisition"):
        assert k in kinds, f"{k} should be terminable (has a completion_gate)"


# --- producer 被覆 (e-5876) ---

def test_producer_coverage_clean_on_real_code():
    # deliverable is declaration-driven; decision has real producers → no dimension is
    # L2-in-name-only.
    assert chk.find_producer_coverage_gaps() == []


def test_producer_coverage_flags_a_dimension_nobody_produces(monkeypatch):
    # A phantom builtin-producer dimension whose producer token does not exist must be
    # flagged (AC2: an L2 dimension no built-in can produce, not declaration-driven).
    monkeypatch.setattr(cl, "COMPLETION_DIMENSIONS",
                        {**cl.COMPLETION_DIMENSIONS,
                         "phantom": {"mode": "builtin-producer", "advice": "x"}})
    monkeypatch.setattr(cl, "COMPLETION_PRODUCER_CALLS",
                        {**cl.COMPLETION_PRODUCER_CALLS,
                         "phantom": frozenset({"produce_phantom_nonexistent"})})
    gaps = chk.find_producer_coverage_gaps()
    assert [g["dimension"] for g in gaps] == ["phantom"]
    assert gaps[0]["status"] == "new_violation"


def test_producer_coverage_declaration_driven_needs_no_producer(monkeypatch):
    # A declaration-driven dimension with NO producer token is still satisfied (②): it is
    # produced only when a class declares it, which no-ops otherwise.
    monkeypatch.setattr(cl, "COMPLETION_DIMENSIONS",
                        {"decl_only": {"mode": "declaration-driven", "advice": "x"}})
    monkeypatch.setattr(cl, "COMPLETION_PRODUCER_CALLS", {"decl_only": frozenset()})
    assert chk.find_producer_coverage_gaps() == []


# --- 完遂 seam 被覆 (e-5877 + e-5878 server scan) ---

def test_completion_seam_clean_after_fix():
    # AC7 (fix 後 clean): every terminable class's completion now routes through the generic
    # seam target_completion.on_target_completion, so there are ZERO gaps on real code.
    assert chk.find_completion_seam_gaps() == []
    result = chk.run()
    assert result["new_completion_seam"] == []
    assert result["pending_completion_seam"] == []
    assert result["ok"] is True


# Pre-fix tokens (WITHOUT the generic seam) reconstruct the 混用 state deterministically —
# so the DETECTION proof does not depend on the real code being broken (it is now fixed).
_PREFIX_PRODUCER_CALLS = {
    "deliverable": frozenset({"capture_target_completion"}),
    "decision": frozenset({"decision_event_from_completion_verdict",
                           "_record_completion_verdict_decision"}),
}


def test_completion_seam_detects_the_混用_synthetically(monkeypatch):
    # AC7 (fix 前 detect): unwire the generic seam (simulate the pre-fix state where only
    # the milestone seams call a producer). The checker MUST surface exactly the 混用 — the
    # sales/operation/acquisition/descriptor terminals dropped from capture — and NOT flag
    # milestone. Deterministic (synthetic tokens), so a green run means "clean", not "asleep".
    monkeypatch.setattr(cl, "COMPLETION_PRODUCER_CALLS", _PREFIX_PRODUCER_CALLS)
    gaps = {(g["class"], g["dimension"]) for g in chk.find_completion_seam_gaps()}
    for expected in (("opportunity", "decision"), ("opportunity", "deliverable"),
                     ("operation", "deliverable"), ("acquisition", "decision"),
                     ("acquisition", "deliverable"),
                     (cl.DESCRIPTOR_TERMINAL_SENTINEL, "decision"),
                     (cl.DESCRIPTOR_TERMINAL_SENTINEL, "deliverable")):
        assert expected in gaps, f"seam check failed to detect gap {expected}"
    # milestone keeps its own direct seams (cmd_milestone_done capture + spine/server
    # decision), so it is covered even without the generic seam.
    assert ("milestone", "decision") not in gaps
    assert ("milestone", "deliverable") not in gaps
    # operation's decision is still covered via the shared review-gated approve path (only
    # its deliverable was a gap) — proves the shared-spine modelling, not a blanket flag.
    assert ("operation", "decision") not in gaps


def test_milestone_completion_is_fully_covered():
    # milestone reaches BOTH producers (deliverable via cmd_milestone_done, decision via the
    # server done_milestone / shared spine approve) — it must NOT be flagged.
    gaps = {(g["class"], g["dimension"]) for g in chk.find_completion_seam_gaps()}
    assert ("milestone", "decision") not in gaps
    assert ("milestone", "deliverable") not in gaps


def test_fresh_seam_gap_fails_when_a_producer_is_unwired(monkeypatch):
    # A regression that unwires a producer (or a fresh terminable class with no capture) is a
    # new_violation that FAILS the checker — the allowlist is empty, so nothing masks it.
    monkeypatch.setattr(cl, "COMPLETION_PRODUCER_CALLS", _PREFIX_PRODUCER_CALLS)
    gaps = chk.find_completion_seam_gaps()
    assert gaps, "expected gaps to surface"
    assert all(g["status"] == "new_violation" for g in gaps)
    assert chk.run()["ok"] is False


def test_known_completion_seam_gap_is_empty():
    # The fix emptied the ratchet allowlist; it must stay empty (a new pending gap would be a
    # regression re-introducing the 混用).
    assert cl.KNOWN_COMPLETION_SEAM_GAP == frozenset()


def test_no_stale_completion_seam_gap():
    # Every allowlist row must correspond to an ACTUALLY-detected gap: once the seam is
    # fixed (e-5879/5880) the gap disappears and its row must be DELETED — this forces it,
    # so the ratchet cannot rot into a lie (same discipline as KNOWN_COLLECTION_COUPLING).
    detected = {(g["class"], g["dimension"]) for g in chk.find_completion_seam_gaps()}
    stale = sorted(row for row in cl.KNOWN_COMPLETION_SEAM_GAP if row not in detected)
    assert not stale, (
        "KNOWN_COMPLETION_SEAM_GAP has rows with no corresponding detected gap — the seam "
        f"was fixed but the allowlist row lingers, drop it: {stale}")


def test_server_is_in_the_completion_scan_population():
    # e-5878: server/ must be scanned, else the server-side done_milestone producer wiring
    # (and any server-side dev-only completion producer) is invisible.
    paths = chk._completion_scan_paths()
    assert any(os.path.sep + "server" + os.path.sep in p for p in paths), (
        "server/ is not in the completion-seam scan population (e-5878)")


# --- the generic seam itself (e-5879/5880) ---

def test_on_target_completion_captures_a_declared_deliverable():
    # the seam delegates deliverable capture — a milestone (declares 機能) completing through
    # it appends the produced-value entry, exactly as the direct capture would (AC5).
    import target_completion as tc
    import deliverable_changelog as dc
    data = {"name": "P", "profession": "dev", "milestones": []}
    tc.on_target_completion(data, {"id": "ms-9", "title": "X", "status": "done"},
                            verdict="done", reason="done それ")
    assert [e["title"] for e in dc.active_deliverables(data)] == ["X"]


def test_on_target_completion_noops_and_never_raises_in_local_mode():
    # a class without a deliverable slot + local mode (no cloud) → no capture, no decision,
    # and crucially NO exception (best-effort: a completion flow must never break).
    import target_completion as tc
    import deliverable_changelog as dc
    data = {"name": "P", "profession": "sales", "opportunities": []}
    tc.on_target_completion(data, {"id": "opp-3"}, verdict="closed_won", reason="決着")
    assert dc.CHANGELOG_KEY not in data


def test_all_four_gap_terminals_reference_the_generic_seam():
    # AC5/AC6 wiring guard: the four previously-uncovered terminals must each call the
    # generic seam, so a refactor that drops the call is caught here (not only by the
    # coverage matrix). Uses the checker's own direct-call attribution over lib + server.
    calls = chk._direct_call_tokens(chk._load_trees(chk._completion_scan_paths()))
    for handler in ("cmd_opportunity_judge", "cmd_operation_close",
                    "cmd_acquisition_status", "cmd_target_close"):
        assert "on_target_completion" in calls.get(handler, set()), (
            f"{handler} no longer calls the generic completion seam "
            "target_completion.on_target_completion (e-5879/5880 wiring dropped)")
