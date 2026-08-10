"""matrix ↔ ratchet reconcile — the single consistency pin (ms-142 e-5013).

Two artifacts describe the ms-142 completeness picture from opposite sides:

  - the BEHAVIORAL matrix (``capability_profession_matrix``) RUNS each abstraction-
    consuming capability against dev / sales / a synthetic profession and asserts it
    lights up (GREEN = surfaces that profession's Target + work item);
  - the ledger RATCHETS (``KNOWN_COLLECTION_COUPLING`` / ``KNOWN_SYMBOL_REACH`` /
    ``KNOWN_ARM_REACH``) hold the RED debt: capabilities that still hardcode a
    profession concrete (a collection / recorder symbol / arm name) and therefore
    MISS a foreign profession.

Leader 裁定 A (2026-08-10): the matrix keeps NO red registry of its own — every
"misses a new profession" debt lives in the ratchets, a single source of truth. This
module is the reconcile that pins the two sides so "green" and "in a ratchet" can
never silently contradict:

  (A) green ⇒ ratchet-absent — a capability the matrix runs GREEN across every
      profession must not appear in any ratchet (it is genuinely declaration-driven,
      so it carries no debt).
  (B) red ⇒ ratchet-present — a capability that behaviorally MISSES a foreign
      profession must be in a ratchet. This is the leader's safety net (補足 2): a
      site that still under-serves sales after a collection fix (its arm read did not
      disappear) shows RED and, if it is in no ratchet, fails HERE — so "a collection
      fix also fixes the arm read" being false is caught, not a hole.
  (C) no stale ratchet entry — every ratchet row is still a real coupling detected on
      the tree, so a greened row must be deleted (delegates to the checker; the
      per-ratchet stale tests in test_capability_ledger cover each family, this
      re-affirms the union for the reconcile's own contract).

When a capability is remediated its behavioral probe flips GREEN and (B)→(A): the
same event that makes the probe pass also REQUIRES deleting its ratchet row, or this
reconcile fails. That coupling is what keeps the matrix and the ratchets honest
together rather than drifting apart.
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import capability_ledger as cl  # noqa: E402
import occupation  # noqa: E402
import capability_profession_matrix as matrix  # noqa: E402
import session_log  # noqa: E402

# load the hyphenated checker script as a module
_CHK_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


# --- the ratchet capability universe (the RED side, all three families) --------

def _ratchet_capability_universe() -> set:
    """The set of capability keys that carry debt in ANY ratchet — collection/symbol
    verbs plus arm-coupling module sites. This is the RED namespace the matrix's
    GREEN capabilities must stay disjoint from (property A)."""
    verbs = {v for v, _ in cl.KNOWN_COLLECTION_COUPLING}
    verbs |= {v for v, _ in cl.KNOWN_SYMBOL_REACH}
    sites = {s for s, _ in cl.KNOWN_ARM_REACH}
    return verbs | sites


def _matrix_lit_capabilities() -> dict:
    """Run every matrix GREEN_PROBE against every profession and return
    ``{capability: lit_for_all}`` — lit_for_all is True only if the probe surfaces
    the Target/work item for EVERY profession (dev / sales / synthetic)."""
    out = {}
    for cap, probe in matrix.GREEN_PROBES.items():
        lit_all = True
        for name, spec in matrix.PROFESSIONS.items():
            project = spec["project"]()
            if not probe(project, spec["target_id"], spec["work_item_id"]):
                lit_all = False
                break
        out[cap] = lit_all
    return out


# --- (A) green ⇒ ratchet-absent ------------------------------------------------

def test_green_capabilities_are_ratchet_absent():
    """A capability the matrix runs GREEN across every profession must not appear in
    any ratchet — it is declaration-driven, so it carries no debt. This is the
    forward half of the reconcile: proving a lit capability is never also tracked as
    broken."""
    universe = _ratchet_capability_universe()
    lit = _matrix_lit_capabilities()
    green = sorted(cap for cap, ok in lit.items() if ok)
    offenders = sorted(set(green) & universe)
    assert offenders == [], (
        "capabilities are GREEN in the behavioral matrix yet ALSO listed as ratchet "
        "debt (green and broken cannot both be true — delete the ratchet row or fix "
        "the probe): " + ", ".join(offenders))


def test_all_matrix_probes_are_green_so_none_needs_a_ratchet():
    """The current matrix probes are the pure abstraction consumers, so every cell is
    GREEN and (B) is vacuously satisfied — asserted explicitly so a regression that
    turns one RED is caught HERE with a clear message, not only in the matrix test.
    A RED abstraction consumer is an occupation.py BUG to fix, never a ratchet row
    (ratchets track hardcoders, not the iterators themselves)."""
    lit = _matrix_lit_capabilities()
    red = sorted(cap for cap, ok in lit.items() if not ok)
    assert red == [], (
        "abstraction-consuming capability went RED for some profession — fix the "
        "occupation abstraction (do NOT add a ratchet row for it): " + ", ".join(red))


# --- (B) red ⇒ ratchet-present, grounded behaviorally --------------------------

def _sales_project_with_session_stamped_evidence():
    """A sales project whose ONLY session-stamped evidence lives under the sales
    evidence arm (``communications``), not under ``entries``. Isolates the arm
    coupling from the separate meta.session_id gap (e-3702): the evidence IS stamped
    here, so a correct arm walk WOULD collect it — only the hardcoded ``entries`` arm
    makes session_log miss it."""
    return {
        "name": "sales", "profession": "sales", "milestones": [],
        "opportunities": [
            {"id": "opp-1", "label": "O", "status": "open",
             "communications": [
                 {"id": "comm-9", "type": "commit", "description": "shipped deck",
                  "meta": {"session_id": "sess-X"}},
             ]},
        ],
    }


def test_session_log_arm_coupling_is_behaviorally_red_and_ratcheted():
    """The RED half of the reconcile, made concrete: session_log's aggregation
    behaviorally MISSES a sales project's session-stamped evidence (because it reads
    the hardcoded ``entries`` arm, not the occupation's evidence arm), AND that debt
    is registered in the arm ratchet. The two facts are asserted together so they
    move together: when session_log is greened (routed through
    profession_manifest evidence_arms) this collect starts returning comm-9, and THEN
    the ('session_log','entries') row MUST be deleted or test_green ... /
    the stale-arm test fails. That linkage is the reconcile."""
    sales = _sales_project_with_session_stamped_evidence()
    collected = session_log.collect_project_entries(sales, "sess-X")
    # behaviorally RED: the sales evidence under the communications arm is not seen.
    assert "comm-9" not in collected["commit_ids"], (
        "session_log now collects the sales evidence arm — it was greened. Delete "
        "('session_log','entries') from KNOWN_ARM_REACH (the reconcile requires "
        "red ⇔ ratchet-present).")
    # …and the debt has a ratchet home (no un-tracked red).
    assert ("session_log", "entries") in cl.KNOWN_ARM_REACH


# --- (C) no stale ratchet entry (union re-affirmation) -------------------------

def test_no_ratchet_entry_is_stale_on_the_real_tree():
    """Every ratchet row across all three families must still be a coupling the
    checker detects on the real tree — a greened surface whose row was not deleted is
    a lie the reconcile refuses. The per-family stale tests live in
    test_capability_ledger; this asserts the UNION so the reconcile owns the whole
    RED set it reasons about."""
    detected_coll = {(c["verb"], c["collection"])
                     for c in chk.find_collection_coupling()}
    detected_sym = {(v["verb"], v["symbol"])
                    for v in chk.find_invariant_violations()}
    detected_arm = {(a["site"], a["arm"]) for a in chk.find_arm_coupling()}

    stale = []
    stale += [f"collection {v}→{c}" for v, c in
              sorted(cl.KNOWN_COLLECTION_COUPLING - detected_coll)]
    stale += [f"symbol {v}→{s}" for v, s in
              sorted(cl.KNOWN_SYMBOL_REACH - detected_sym)]
    stale += [f"arm {s}→{a}" for s, a in
              sorted(cl.KNOWN_ARM_REACH - detected_arm)]
    assert stale == [], (
        "ratchet rows no longer detected on the tree (the surface was greened — "
        "delete the rows): " + "; ".join(stale))


def test_synthetic_profession_proves_declaration_driven():
    """The reconcile only means something if the matrix's GREEN is real: a SYNTHETIC
    profession whose arms are named nothing like dev/sales must flow green through
    every abstraction consumer. If it does not, the 'abstraction' is a dev-shaped
    assumption and the whole green side is unreliable — fail loudly here."""
    spec = matrix.PROFESSIONS["compliance"]
    project = spec["project"]()
    for cap, probe in matrix.GREEN_PROBES.items():
        assert probe(project, spec["target_id"], spec["work_item_id"]), (
            f"synthetic profession did NOT light up {cap} — the abstraction is not "
            f"declaration-driven (a new occupation would not light up either)")
