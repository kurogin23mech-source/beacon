"""Tests for the capability scope ledger + invariant checker (ms-134 e-4709 /
e-4720 / e-4721).

Load-bearing tests:
  - ``test_every_live_verb_is_classified`` pins full coverage: a newly-added
    verb under an unknown noun fails CI until it gets a scope (e-4709 AC).
  - ``test_no_profession_shared_capability_reaches_a_concrete`` is the invariant
    gate: it fails CI if any L1/L2 capability calls a profession concrete
    (``core.save_entry`` / ``find_target_milestone``) — the boundary e-4720
    closed for ``doc`` and e-4721 keeps closed.
  - ``test_checker_flags_a_shared_handler_calling_the_concrete`` /
    ``…_via_helper`` prove the checker DETECTS the violation (deterministic,
    synthetic source — not dependent on git history), so a green run means
    "clean", not "checker asleep".
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import capability_ledger as cl  # noqa: E402
import importlib.util  # noqa: E402

# load the checker script as a module (hyphenated filename → importlib)
_CHK_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


# --- coverage (e-4709 AC: every capability classified, none unclassified) ---

def test_every_live_verb_is_classified():
    rec = cl.reconcile()
    assert rec["unclassified"] == [], (
        "unclassified verbs (give the noun a scope in _NOUN_SCOPE): "
        + ", ".join(rec["unclassified"]))


def test_every_skill_is_classified():
    rec = cl.reconcile_skills()
    assert rec["unclassified"] == [], (
        "unclassified skills (give a scope in _SKILL_SCOPE / _SKILL_PREFIX_SCOPE): "
        + ", ".join(rec["unclassified"]))


def test_key_skills_have_expected_scope():
    assert cl.skill_scope_of("beacon-sales-email") == "L3"
    assert cl.skill_scope_of("beacon-task") == "L3"
    assert cl.skill_scope_of("beacon-trek-execute") == "L1"
    assert cl.skill_scope_of("beacon-map") == "L2"
    assert cl.skill_scope_of("_beacon-spec-methodology") == "L1"


def test_ledger_uses_shared_surface_source():
    """The ledger reconciles against the SAME live surface the verb ledger and
    map-drift lint use, so the three can never drift apart."""
    import cli_surface
    assert cl.enumerate_live_verbs() == cli_surface.enumerate_cli_verbs()


def test_key_capabilities_have_expected_scope():
    # doc is the class-abstraction (L2) capability the whole MS turns on.
    assert cl.scope_of("doc_add") == "L2"
    assert cl.scope_of("doc_update") == "L2"
    assert cl.scope_of("claim_view") == "L2"
    assert cl.scope_of("status") == "L2"
    # profession-specific defaults must be L3 (NOT shared) so their legitimate
    # use of the dev concrete is not flagged.
    for v in ("milestone_add", "task_add", "log_finalize", "save", "sync",
              "pr_add", "account_add", "opportunity_add"):
        assert cl.scope_of(v) == "L3", v
    assert cl.scope_of("bus_ack") == "L1"
    assert cl.scope_of("doctor") == "L0"


def test_scope_values_are_valid():
    live = cl.enumerate_live_verbs()
    for v in live:
        assert cl.scope_of(v) in cl.SCOPE_LEVELS, v


# --- dependency partial order (设计方针1/4) ---------------------------------

def test_may_depend_partial_order():
    # broad may be depended on by narrower; narrower must not be depended on by broad.
    assert cl.may_depend("L2", "L1") is True     # L2 → L1 ok (depend on broader)
    assert cl.may_depend("L3", "L2") is True     # L3 → L2 ok
    assert cl.may_depend("L1", "L3") is False    # L1 must not depend on L3 (narrower)
    assert cl.may_depend("L2", "L3") is False    # the doc→milestone shape: forbidden
    assert cl.may_depend("L2", "L2") is True     # same scope ok
    # L0 (product operation) may use public levels but must not be depended on.
    assert cl.may_depend("L0", "L1") is True
    assert cl.may_depend("L2", "L0") is False


def test_origin_is_orthogonal_axis():
    # origin is a separate axis, not a scope level (the rejected L5).
    assert cl.origin_of("doc_add") in cl.ORIGINS
    assert "L5" not in cl.SCOPE_LEVELS


# --- ownership axis (ms-134 e-4738) ----------------------------------------

def test_owner_required_only_for_l3_l4():
    assert cl.owner_required("L3") is True
    assert cl.owner_required("L4") is True
    for shared in ("L0", "L1", "L2", ""):
        assert cl.owner_required(shared) is False, shared


def test_owner_of_dispatches_by_scope():
    # L3 dev / sales resolve to their profession; shared scopes have no owner.
    assert cl.owner_of("milestone_add") == "dev"
    assert cl.owner_of("task_done") == "dev"
    assert cl.owner_of("opportunity_add") == "sales"
    assert cl.owner_of("account_add") == "sales"
    # shared (L1/L2/L0) capabilities have NO single owner — a correct empty.
    assert cl.owner_of("doc_add") == ""      # L2
    assert cl.owner_of("bus_ack") == ""      # L1
    assert cl.owner_of("doctor") == ""       # L0


def test_owner_values_are_valid_professions():
    for v in cl.enumerate_live_verbs():
        o = cl.owner_of(v)
        if o:
            assert o in cl.PROFESSIONS, (v, o)


def test_every_l3_l4_verb_has_owner():
    # The ownership coverage gate: every L3 verb resolves to a profession (and
    # every L4 to a project). A new L3 noun without an owner fails here.
    rec = cl.reconcile_ownership()
    assert rec["unowned"] == [], (
        "L3/L4 verbs with no owner (add the noun to _L3_NOUN_PROFESSION): "
        + ", ".join(rec["unowned"]))


def test_every_l3_skill_has_owner():
    rec = cl.reconcile_skills_ownership()
    assert rec["unowned"] == [], (
        "L3 skills with no owner (add to _SKILL_OWNER / _SKILL_OWNER_PREFIX): "
        + ", ".join(rec["unowned"]))


def test_skill_owner_map_has_no_stale_entries():
    # Symmetry with the verb-side stale check (maintainability review 2026-08-03):
    # every exact-name key in _SKILL_OWNER must still classify as L3 in
    # _SKILL_SCOPE. A skill demoted L3→L2 with a leftover _SKILL_OWNER entry is a
    # stale lie about ownership; this catches it (the reconcile coverage test only
    # catches the missing-owner direction).
    stale = sorted(name for name in cl._SKILL_OWNER
                   if cl.skill_scope_of(name) != "L3")
    assert stale == [], (
        "stale _SKILL_OWNER entries (no longer L3 in _SKILL_SCOPE): "
        + ", ".join(stale))
    # every owner-prefix must correspond to a scope-prefix that yields L3.
    for prefix, _owner in cl._SKILL_OWNER_PREFIX:
        assert cl.skill_scope_of(prefix + "x") == "L3", (
            f"owner prefix {prefix!r} does not resolve to an L3 skill scope")


def test_l3_profession_map_is_in_sync_with_scope():
    # Every L3 noun in _NOUN_SCOPE must have a profession, and no entry in
    # _L3_NOUN_PROFESSION may be stale (point at a non-L3 noun). This keeps the
    # ownership axis honest as the scope map evolves.
    l3_nouns = {n for n, s in cl._NOUN_SCOPE.items() if s == "L3"}
    owned_nouns = set(cl._L3_NOUN_PROFESSION)
    assert l3_nouns - owned_nouns == set(), (
        "L3 nouns missing a profession owner: " + ", ".join(sorted(l3_nouns - owned_nouns)))
    assert owned_nouns - l3_nouns == set(), (
        "stale _L3_NOUN_PROFESSION entries (not L3 in _NOUN_SCOPE): "
        + ", ".join(sorted(owned_nouns - l3_nouns)))


def test_skill_owner_of_representative():
    assert cl.skill_owner_of("beacon-sales-email") == "sales"
    assert cl.skill_owner_of("beacon-task") == "dev"
    assert cl.skill_owner_of("beacon-deploy") == "dev"
    # shared skills have no owner.
    assert cl.skill_owner_of("beacon-trek-execute") == ""   # L1
    assert cl.skill_owner_of("beacon-map") == ""            # L2


def test_ownership_is_orthogonal_to_scope_and_origin():
    # ownership (who) is a third axis, distinct from scope (how widely shared)
    # and origin (who authored). A capability can be L3+dev+beacon-default.
    assert cl.scope_of("milestone_add") == "L3"
    assert cl.owner_of("milestone_add") == "dev"
    assert cl.origin_of("milestone_add") == "beacon-default"


# --- invariant gate (e-4720/e-4721): real tree must be clean ---------------

def test_no_profession_shared_capability_reaches_a_concrete():
    viol = chk.find_invariant_violations()
    assert viol == [], (
        "profession-shared (L1/L2) capabilities reaching a profession concrete: "
        + "; ".join(f"{v['verb']}[{v['scope']}]→{v['symbol']}"
                    f"{' via '+v['via'] if v['via'] else ''}" for v in viol))


def test_run_is_ok_on_real_tree():
    result = chk.run()
    assert result["ok"] is True, result


# --- the checker actually detects violations (not asleep) -------------------

_SYNTH_DIRECT = '''
import core
def cmd_doc_synthetic():
    """A shared (doc→L2) handler that wrongly reaches the dev concrete.
    This docstring mentions core.save_entry but that must NOT be flagged."""
    data = {}
    # a comment mentioning core.find_target_milestone must also be ignored
    core.save_entry(data, ms_id="", description="x", source="auto", date="")
'''

_SYNTH_HELPER = '''
import core
def _synthetic_doc_helper(data):
    core.save_entry(data, ms_id="", description="x", source="auto", date="")
def cmd_doc_synthetic2():
    data = {}
    _synthetic_doc_helper(data)
'''

_SYNTH_ALLOWED = '''
import core
def cmd_task_synthetic():
    # task_* is L3 (profession-specific dev) — using the dev concrete is legit.
    data = {}
    core.save_entry(data, ms_id="", description="x", source="auto", date="")
'''


def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src, encoding="utf-8")
    return str(p)


def test_checker_flags_a_shared_handler_calling_the_concrete(tmp_path):
    path = _write(tmp_path, "commands.py", _SYNTH_DIRECT)
    viol = chk.find_invariant_violations(path)
    assert len(viol) == 1, viol
    assert viol[0]["verb"] == "doc_synthetic"
    assert viol[0]["scope"] == "L2"
    assert viol[0]["symbol"] == "core.save_entry"
    assert viol[0]["via"] == ""


def test_checker_flags_concrete_via_helper(tmp_path):
    path = _write(tmp_path, "commands.py", _SYNTH_HELPER)
    viol = chk.find_invariant_violations(path)
    assert len(viol) == 1, viol
    assert viol[0]["verb"] == "doc_synthetic2"
    assert viol[0]["via"] == "_synthetic_doc_helper"


def test_checker_ignores_comment_and_docstring_mentions(tmp_path):
    # _SYNTH_DIRECT mentions the symbol in a docstring and a comment; only the
    # single real call must be flagged (proves AST-based, not text grep).
    path = _write(tmp_path, "commands.py", _SYNTH_DIRECT)
    viol = chk.find_invariant_violations(path)
    assert len(viol) == 1


def test_checker_allows_profession_specific_concrete_use(tmp_path):
    # a L3 (dev) handler using the dev concrete is legitimate — not a violation.
    path = _write(tmp_path, "commands.py", _SYNTH_ALLOWED)
    viol = chk.find_invariant_violations(path)
    assert viol == []


_SYNTH_SALES_CONCRETE = '''
import sales_entities
def cmd_doc_synthetic3():
    # a shared (doc→L2) handler must not reach a SALES concrete either (symmetry).
    data = {}
    sales_entities.activity_add(data, "opp-1", "x")
'''


def test_checker_flags_shared_handler_reaching_sales_concrete(tmp_path):
    """Symmetry (philosophy #1): an L1/L2 capability reaching a SALES concrete
    (sales_entities.activity_add) is flagged, not just the dev side."""
    path = _write(tmp_path, "commands.py", _SYNTH_SALES_CONCRETE)
    viol = chk.find_invariant_violations(path)
    assert len(viol) == 1, viol
    assert viol[0]["verb"] == "doc_synthetic3"
    assert viol[0]["symbol"] == "sales_entities.activity_add"


def test_sales_concretes_are_in_denylist():
    # both profession sides present so the invariant is symmetric.
    assert "core.save_entry" in cl.PROFESSION_CONCRETE_SYMBOLS
    assert "sales_entities.activity_add" in cl.PROFESSION_CONCRETE_SYMBOLS


# --- non-enumerated collection coupling (ms-134 e-4740) --------------------

def test_no_new_collection_coupling_on_real_tree():
    """The ratchet gate: a NEW (non-allowlisted) shared capability reading a
    profession collection directly (data['milestones'] etc.) fails CI. Existing
    couplings are accepted debt; adding a fresh one flips this red."""
    result = chk.run()
    new = result["new_collection_coupling"]
    assert new == [], (
        "NEW profession-collection coupling — route it through the abstraction "
        "occupation.iter_target_records(data) (see the KNOWN_COLLECTION_COUPLING "
        "comment: never add a new entry to silence a fresh violation): "
        + "; ".join(f"{c['verb']}[{c['scope']}]→data['{c['collection']}']"
                    f"{' via '+c['via'] if c['via'] else ''}" for c in new))


def test_every_detected_coupling_is_classified():
    # On the real tree, every detected coupling must be either accepted debt
    # (pending_debt) or a human-reviewed correct read (legitimate); a
    # new_violation is caught by the test above, asserted here for a clear msg.
    coupling = chk.find_collection_coupling()
    for c in coupling:
        assert c["status"] in ("pending_debt", "reviewed_correct"), (
            f"un-classified coupling: {c}")


def test_no_stale_collection_allowlist_entries():
    """Ratchet hygiene: every (verb, collection) in KNOWN_COLLECTION_COUPLING
    AND in REVIEWED_LEGITIMATE_COLLECTION_READS must still be detected in the
    real tree. When a handler is remediated (debt) or refactored away the read
    disappears, so its row MUST be deleted — this test fails until it is,
    preventing either list from rotting into a stale lie about what still reads a
    profession collection."""
    detected = {(c["verb"], c["collection"]) for c in chk.find_collection_coupling()}
    stale_debt = sorted(cl.KNOWN_COLLECTION_COUPLING - detected)
    assert not stale_debt, (
        "pending-debt entries no longer detected (delete them — remediated): "
        + ", ".join(f"{v}→{coll}" for v, coll in stale_debt))
    stale_legit = sorted(set(cl.REVIEWED_LEGITIMATE_COLLECTION_READS) - detected)
    assert not stale_legit, (
        "reviewed-legitimate entries no longer detected (delete them — the read "
        "was refactored away): " + ", ".join(f"{v}→{coll}" for v, coll in stale_legit))


def test_debt_and_legitimate_are_disjoint():
    # A (verb, collection) cannot be BOTH pending debt and reviewed-legitimate —
    # it is one or the other. Keeps the two lists from silently contradicting.
    overlap = set(cl.KNOWN_COLLECTION_COUPLING) & set(cl.REVIEWED_LEGITIMATE_COLLECTION_READS)
    assert overlap == set(), f"a read is both debt and legitimate: {overlap}"


def test_reviewed_correct_reads_are_classified_correctly():
    # The human-reviewed correct reads (e-4737) report as "reviewed_correct",
    # not debt — so they are not nagged for remediation.
    by_key = {(c["verb"], c["collection"]): c["status"]
              for c in chk.find_collection_coupling()}
    assert by_key.get(("target_list", "milestones")) == "reviewed_correct"
    assert by_key.get(("session_end", "milestones")) == "reviewed_correct"
    # session_fork is milestone-scoped by design (a git-worktree op) — an initial
    # over-eager remediation was reverted after independent AX review 2026-08-03,
    # and it is now classified reviewed-correct, not remediated.
    assert by_key.get(("session_fork", "milestones")) == "reviewed_correct"


def test_reviewed_correct_advice_is_evidence_not_routing_hint():
    # A reviewed-correct read must NOT carry the "route via iter_target_records"
    # advice (which would be wrong for it); its advice is the review evidence
    # (maintainability review 2026-08-03).
    for c in chk.find_collection_coupling():
        if c["status"] == "reviewed_correct":
            assert "iter_target_records" not in c["advice"] or "NOT" in c["advice"]
            assert c["advice"] == cl.REVIEWED_LEGITIMATE_COLLECTION_READS[
                (c["verb"], c["collection"])]


def test_operations_is_not_a_profession_collection():
    # operations is the L1 cross-profession scheduling collection — reading it
    # from a shared handler is legitimate and must NOT be a concrete collection.
    assert "operations" not in cl.PROFESSION_CONCRETE_COLLECTIONS
    assert "milestones" in cl.PROFESSION_CONCRETE_COLLECTIONS
    assert "opportunities" in cl.PROFESSION_CONCRETE_COLLECTIONS


_SYNTH_COLLECTION_NEW = '''
def load_project():
    return {}
def cmd_doc_synthetic_coll():
    # doc_* is L2 (shared); reading a dev collection directly is a NEW coupling.
    data = load_project()
    for m in data["milestones"]:
        pass
    also = data.get("opportunities", [])
'''

_SYNTH_COLLECTION_VIA_HELPER = '''
def _synth_walk(data):
    return list(data.get("milestones", []))
def cmd_status_synthetic_coll():
    # status is L2; the read lives in a helper → attributed transitively.
    data = {}
    _synth_walk(data)
'''

_SYNTH_COLLECTION_L3_OK = '''
def cmd_milestone_synthetic_coll():
    # milestone_* is L3 (dev profession default) — reading data['milestones']
    # is its legitimate job, not a coupling violation.
    data = {}
    for m in data["milestones"]:
        pass
'''

_SYNTH_COLLECTION_OPERATIONS_OK = '''
def cmd_doc_synthetic_ops():
    # operations is L1 cross-profession, not a profession concrete → not flagged.
    data = {}
    for o in data.get("operations", []):
        pass
'''


def test_checker_flags_new_collection_read_direct(tmp_path):
    path = _write(tmp_path, "commands.py", _SYNTH_COLLECTION_NEW)
    hits = chk.find_collection_coupling(path)
    cols = sorted((h["verb"], h["collection"]) for h in hits)
    assert cols == [("doc_synthetic_coll", "milestones"),
                    ("doc_synthetic_coll", "opportunities")], hits
    # synthetic verbs aren't allowlisted → all fresh violations.
    assert all(h["status"] == "new_violation" for h in hits)


def test_checker_flags_collection_read_via_helper(tmp_path):
    path = _write(tmp_path, "commands.py", _SYNTH_COLLECTION_VIA_HELPER)
    hits = chk.find_collection_coupling(path)
    assert len(hits) == 1, hits
    assert hits[0]["verb"] == "status_synthetic_coll"
    assert hits[0]["collection"] == "milestones"
    assert hits[0]["via"] == "_synth_walk"


def test_checker_ignores_l3_collection_read(tmp_path):
    # a profession-specific (L3) handler reading its own collection is fine.
    path = _write(tmp_path, "commands.py", _SYNTH_COLLECTION_L3_OK)
    assert chk.find_collection_coupling(path) == []


def test_checker_ignores_operations_collection(tmp_path):
    path = _write(tmp_path, "commands.py", _SYNTH_COLLECTION_OPERATIONS_OK)
    assert chk.find_collection_coupling(path) == []


_SYNTH_COLLECTION_WRITE_CTX = '''
def cmd_doc_synthetic_write():
    # A shared (doc→L2) handler that WRITES a same-named local dict key is NOT a
    # reach into project data — must not be a false positive (maintainability
    # review 2026-08-03: only ast.Load subscripts count).
    result = {}
    result["milestones"] = []
    result["opportunities"] = compute()
    del result["accounts"]
'''


def test_checker_ignores_collection_write_context(tmp_path):
    # a WRITE (Store/Del) to a same-named local dict key is not a read of
    # project data — the ctx guard keeps it from being a false positive.
    path = _write(tmp_path, "commands.py", _SYNTH_COLLECTION_WRITE_CTX)
    assert chk.find_collection_coupling(path) == []
