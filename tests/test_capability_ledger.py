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

import ast
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


def test_review_split_dev_instruments_vs_neutral_plumbing():
    # ms-134 e-5061 review 分割: the review VERB + the beacon-review* workflow/
    # attainment skills are profession-neutral (L2); the code-review instrument
    # skills (ax/philosophy/maintainability) review CODE = a dev artifact (L3-dev).
    assert cl.scope_of("review_context") == "L2"       # the verb stays plumbing
    assert cl.skill_scope_of("beacon-review") == "L2"  # entry + attainment plumbing
    assert cl.skill_scope_of("beacon-review-run") == "L2"
    for instrument in ("ax-review", "philosophy-review", "maintainability-review"):
        assert cl.skill_scope_of(instrument) == "L3", instrument
        assert cl.skill_owner_of(instrument) == "dev", instrument


def test_dir_form_skills_are_enumerated():
    # The review instruments are dir-form skills (skills/<name>/SKILL.md), not
    # top-level *.md; enumerate_skills must still see them so they are classified.
    skills = cl.enumerate_skills()
    for instrument in ("ax-review", "philosophy-review", "maintainability-review"):
        assert instrument in skills, instrument


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
    # ms-134 e-5061 step 2: class-derived CRUD promoted L3→L2 (a milestone IS a
    # Target, a task IS a WorkItem, a commit/log IS Evidence — the operation rule
    # is profession-common). Their still-concrete reads/records are tracked as
    # expected-red debt (KNOWN_COLLECTION_COUPLING / KNOWN_SYMBOL_REACH, owner=ms-143)
    # until ms-143 PR #2 abstracts them.
    for v in ("milestone_add", "task_add", "log_finalize", "save", "sync",
              "opportunity_add", "opportunity_activity", "communication_add",
              "acquisition_list", "retro_prepare"):
        assert cl.scope_of(v) == "L2", v
    # genuinely dev/sales-specific defaults stay L3 (a PR / a customer account are
    # not Target-class instances), so their use of the profession concrete is legit.
    for v in ("pr_add", "account_add", "deploy", "push", "contact_add"):
        assert cl.scope_of(v) == "L3", v
    assert cl.scope_of("bus_ack") == "L1"
    # ms-134 e-5061: doctor/update/project/skill/migrate/reset promoted L0→L1
    # (instance-universal tooling, not Beacon-repo-only operation). No verb is L0
    # anymore; the L0 scope is carried by skills (beacon-drift-check) instead.
    assert cl.scope_of("doctor") == "L1"
    assert cl.scope_of("update") == "L1"
    assert cl.scope_of("project_export") == "L1"


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
    assert cl.owner_of("pr_add") == "dev"
    assert cl.owner_of("deploy") == "dev"
    assert cl.owner_of("account_add") == "sales"
    assert cl.owner_of("contact_add") == "sales"
    # shared (L1/L2) capabilities have NO single owner — a correct empty. The
    # class-derived CRUD promoted L3→L2 in e-5061 step 2 are now unowned (correct):
    assert cl.owner_of("milestone_add") == ""  # L2 (was dev L3, e-5061)
    assert cl.owner_of("task_done") == ""       # L2 (was dev L3)
    assert cl.owner_of("opportunity_add") == ""  # L2 (was sales L3)
    assert cl.owner_of("doc_add") == ""      # L2
    assert cl.owner_of("bus_ack") == ""      # L1
    assert cl.owner_of("doctor") == ""       # L1 (was L0, e-5061; still unowned)


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


def test_reclassification_2026_08_03_is_pinned():
    # Pin the e-4737 台帳-review reclassifications so a local edit that reverts one
    # fails immediately (the sync test only checks L3 completeness, so an L0↔L1
    # mis-revert would otherwise pass silently — maintainability review 2026-08-03).
    assert cl.scope_of("master_sync_drain") == "L1"   # was L0
    assert cl.scope_of("morning") == "L1"             # was L3/sales
    assert cl.scope_of("profile_list") == "L1"        # was L3/sales
    assert cl.scope_of("watch_set") == "L3"           # was L1
    assert cl.owner_of("watch_set") == "sales"        # now a sales default
    # the three that moved to L1 must have NO owner (shared).
    assert cl.owner_of("morning") == ""
    assert cl.owner_of("profile_list") == ""
    assert cl.owner_of("master_sync_drain") == ""


def test_ownership_is_orthogonal_to_scope_and_origin():
    # ownership (who) is a third axis, distinct from scope (how widely shared)
    # and origin (who authored). A capability can be L3+dev+beacon-default.
    # (pr_add stays L3-dev; milestone_add moved to L2 in e-5061 step 2.)
    assert cl.scope_of("pr_add") == "L3"
    assert cl.owner_of("pr_add") == "dev"
    assert cl.origin_of("pr_add") == "beacon-default"


# --- invariant gate (e-4720/e-4721): real tree must be clean ---------------

def test_no_profession_shared_capability_reaches_a_concrete():
    # Only a NEW (non-allowlisted) symbol reach fails: an accepted-pending reach
    # (KNOWN_SYMBOL_REACH, ms-143-owned class-derived recorder) is debt, not a
    # failure — symmetric to the collection ratchet (ms-134 e-5061).
    viol = [v for v in chk.find_invariant_violations()
            if v["status"] == "new_violation"]
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
def cmd_pr_synthetic():
    # pr_* is L3 (profession-specific dev) — using the dev concrete is legit.
    # (task_* moved to L2 in e-5061 step 2, so it is no longer a valid L3 example.)
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


# --- symbol-reach ratchet (ms-134 e-5061), symmetric to the collection ratchet -

def test_no_l0_verb_in_shipped_distribution(monkeypatch):
    # Distribution exclusion (ms-134 e-5062): no L0 verb may be dispatchable in the
    # shipped CLI surface. Empty by construction after e-5061 (no verb is L0).
    assert cl.shipped_l0_verbs() == [], cl.shipped_l0_verbs()
    assert chk.run()["l0_distribution_leak"] == []
    # Mechanism: if a live verb were (mis)classified L0, the guard flags it. doctor
    # is a real live verb — pin it L0 and it must surface, then the gate goes red.
    monkeypatch.setitem(cl._NOUN_SCOPE, "doctor", "L0")
    assert "doctor" in cl.shipped_l0_verbs()
    assert chk.run()["ok"] is False


def test_no_l0_skill_in_shipped_distribution(monkeypatch):
    # Distribution exclusion (ms-134 e-5086, the skill-side twin of e-5062): no L0
    # Skill may ship in the bundled skills/ tree (every *.md ships via pyproject
    # package-data). Empty by construction after e-5086 — beacon-drift-check, the
    # only former L0 Skill, was reclassified L1 (universal source↔installed drift
    # check that a distributed pipx user can hit too).
    assert cl.shipped_l0_skills() == [], cl.shipped_l0_skills()
    assert chk.run()["l0_skill_distribution_leak"] == []
    # Mechanism: if a shipped Skill were (mis)classified L0, the guard flags it.
    # beacon-drift-check is a real shipped Skill — pin it L0 and it must surface,
    # then the gate goes red. This proves the guard is on the axis a leak appears on
    # (skills), not just the empty verb axis.
    monkeypatch.setitem(cl._SKILL_SCOPE, "beacon-drift-check", "L0")
    assert "beacon-drift-check" in cl.shipped_l0_skills()
    assert chk.run()["ok"] is False


def test_beacon_drift_check_is_l1_universal_install_drift(monkeypatch):
    # ms-134 e-5086: beacon-drift-check detects source↔installed skill drift, which
    # any beacon user (a pipx install that `beacon update`d without re-running
    # `beacon skill install`, not only a repo developer) can have — so it is L1
    # (instance-universal), not L0 (Beacon-repo-only operation).
    assert cl.skill_scope_of("beacon-drift-check") == "L1"


def test_no_new_symbol_reach_on_real_tree():
    """The symbol ratchet gate: a NEW (non-allowlisted) shared capability calling
    a profession recorder/resolver symbol fails CI. An allowlisted (KNOWN_SYMBOL_
    REACH, ms-143-owned) reach is accepted debt. Mirrors the collection gate."""
    result = chk.run()
    new = result["new_symbol_reach"]
    assert new == [], (
        "NEW profession symbol reach — route it through "
        "occupation.record_target_entry (never add a KNOWN_SYMBOL_REACH entry to "
        "silence a fresh violation): "
        + "; ".join(f"{v['verb']}[{v['scope']}]→{v['symbol']}"
                    f"{' via '+v['via'] if v['via'] else ''}" for v in new))


def test_symbol_reach_allowlist_makes_a_reach_pending(tmp_path, monkeypatch):
    # The mechanism: a (verb, symbol) in KNOWN_SYMBOL_REACH is classified
    # pending_debt (not new_violation) so it no longer fails the gate. Proven on a
    # synthetic tree with a monkeypatched allowlist so the assertion is deterministic
    # regardless of the real allowlist's ms-143 remainder.
    path = _write(tmp_path, "commands.py", _SYNTH_DIRECT)
    before = chk.find_invariant_violations(path)
    assert before[0]["status"] == "new_violation"  # doc_synthetic not allowlisted
    monkeypatch.setattr(cl, "KNOWN_SYMBOL_REACH",
                        {("doc_synthetic", "core.save_entry")})
    after = chk.find_invariant_violations(path)
    assert len(after) == 1 and after[0]["status"] == "pending_debt", after


def test_no_stale_symbol_reach_allowlist_entries():
    """Ratchet hygiene (symmetric to test_no_stale_collection_allowlist_entries):
    every (verb, symbol) in KNOWN_SYMBOL_REACH must still be detected on the real
    tree, so a row cannot rot into a lie after its handler is abstracted. Now that
    step 2 has registered the ms-143 remainder, this enforces that each entry
    still reaches a concrete until ms-143 PR #2 abstracts it and deletes the row."""
    detected = {(v["verb"], v["symbol"]) for v in chk.find_invariant_violations()}
    stale = sorted(cl.KNOWN_SYMBOL_REACH - detected)
    assert not stale, (
        "pending symbol-reach entries no longer detected (delete them — the "
        "handler was abstracted): " + ", ".join(f"{v}→{s}" for v, s in stale))


# --- arm-name coupling ratchet (ms-142 e-5012), the third reach class ---------

_SYNTH_ARM_READ = '''
import occupation
def collect_synthetic(data):
    """A shared-frame aggregator that walks Targets through the abstraction but
    then reads the dev `entries` arm directly. The docstring mentions entries but
    only the real read must be flagged (AST, not grep)."""
    out = []
    for tgt in occupation.iter_target_records(data):
        for e in tgt.get("entries", []):   # <- arm-name coupling
            out.append(e)
    return out
'''

_SYNTH_ARM_WRITE = '''
def build(data):
    # a WRITE to a same-named local key is not a reach into a Target record and
    # must not be a false positive (LOAD-only Subscript guard).
    result = {}
    result["entries"] = []
    return result
'''


def test_no_new_arm_coupling_on_real_tree():
    """The arm ratchet gate: a NEW (non-allowlisted) shared-frame aggregator
    reading a profession arm name off a Target record fails CI. The existing
    session_log read is accepted debt; adding a fresh one flips this red. Mirrors
    the collection/symbol gates."""
    result = chk.run()
    new = result["new_arm_coupling"]
    assert new == [], (
        "NEW profession arm coupling — route it through "
        "occupation.profession_manifest evidence_arms / work_item_arm (never add a "
        "KNOWN_ARM_REACH entry to silence a fresh read): "
        + "; ".join(f"{a['site']}→tgt['{a['arm']}'] @ {a['file']}:{a['lineno']}"
                    for a in new))


def test_every_detected_arm_read_is_classified():
    # On the real tree every detected arm read must be CLASSIFIED — accepted debt
    # (pending_debt) OR a human-reviewed legitimate read (reviewed_correct, the C2
    # recovery path for a generic arm name off a non-Target record); a new_violation
    # is caught above, asserted here for a clear message. (The assert must accept
    # reviewed_correct: hard-coding pending_debt would false-fail the moment the
    # REVIEWED_LEGITIMATE_ARM_READS mechanism is first used — PR #629 re-review low#1.)
    for a in chk.find_arm_coupling():
        assert a["status"] in ("pending_debt", "reviewed_correct"), (
            f"un-classified arm read: {a}")


def test_no_stale_arm_reach_allowlist_entries():
    """Ratchet hygiene (symmetric to the collection/symbol stale checks): every
    (site, arm) in KNOWN_ARM_REACH AND in REVIEWED_LEGITIMATE_ARM_READS must still
    be detected on the real tree, so a row cannot rot into a lie after the module is
    routed through the manifest (debt) or the read is refactored away (reviewed)."""
    detected = {(a["site"], a["arm"]) for a in chk.find_arm_coupling()}
    stale_debt = sorted(cl.KNOWN_ARM_REACH - detected)
    assert not stale_debt, (
        "pending arm-reach entries no longer detected (delete them — the module "
        "was routed through profession_manifest): "
        + ", ".join(f"{s}→{arm}" for s, arm in stale_debt))
    stale_reviewed = sorted(set(cl.REVIEWED_LEGITIMATE_ARM_READS) - detected)
    assert not stale_reviewed, (
        "reviewed-legitimate arm entries no longer detected (delete them — the read "
        "was refactored away): " + ", ".join(f"{s}→{arm}" for s, arm in stale_reviewed))


def test_registered_arm_reads_are_the_detected_debt():
    # The concrete anchors: both shared-frame aggregators that abstracted target
    # enumeration but kept a dev-arm read (session_log aggregation + cmd_project
    # export) are detected and are exactly the allowlisted debt (not fresh
    # violations). cmd_project was surfaced by the arm scan itself — the collection
    # ratchet is blind to an arm read (ms-142 e-5012).
    by_key = {(a["site"], a["arm"]): a["status"] for a in chk.find_arm_coupling()}
    assert by_key.get(("session_log", "entries")) == "pending_debt"
    assert by_key.get(("cmd_project", "entries")) == "pending_debt"
    assert ("session_log", "entries") in cl.KNOWN_ARM_REACH
    assert ("cmd_project", "entries") in cl.KNOWN_ARM_REACH


def test_checker_flags_a_shared_frame_arm_read(tmp_path):
    # A synthetic aggregator reading tgt['entries'] is flagged as a NEW arm read
    # (its site is not in KNOWN_ARM_REACH), proving the checker detects it.
    path = _write(tmp_path, "aggregator.py", _SYNTH_ARM_READ)
    hits = chk.find_arm_coupling(path)
    assert len(hits) == 1, hits
    assert hits[0]["site"] == "aggregator"
    assert hits[0]["arm"] == "entries"
    assert hits[0]["status"] == "new_violation"


def test_arm_scan_ignores_a_write_to_a_same_named_local(tmp_path):
    # result["entries"] = [] is a WRITE, not a reach into a Target record — the
    # LOAD-only Subscript guard must keep it from being a false positive.
    path = _write(tmp_path, "builder.py", _SYNTH_ARM_WRITE)
    assert chk.find_arm_coupling(path) == []


def test_arm_reach_allowlist_makes_a_read_pending(tmp_path, monkeypatch):
    # The mechanism: a (site, arm) in KNOWN_ARM_REACH is classified pending_debt
    # (not new_violation) so it no longer fails the gate. Proven on a synthetic
    # tree with a monkeypatched allowlist so the assertion is deterministic.
    path = _write(tmp_path, "aggregator.py", _SYNTH_ARM_READ)
    before = chk.find_arm_coupling(path)
    assert before[0]["status"] == "new_violation"  # aggregator not allowlisted
    monkeypatch.setattr(cl, "KNOWN_ARM_REACH", {("aggregator", "entries")})
    after = chk.find_arm_coupling(path)
    assert len(after) == 1 and after[0]["status"] == "pending_debt", after


def test_profession_arms_cover_decomposition():
    """PROFESSION_CONCRETE_ARMS must stay a SUPERSET of every fat arm any built-in
    profession Target declares in occupation.TARGET_DECOMPOSITION — so a new arm
    added to a profession cannot slip in without the arm checker learning to
    police it (the forcing function that keeps the denylist honest)."""
    import occupation
    declared = {arm
                for spec in occupation.TARGET_DECOMPOSITION.values()
                for arm in spec["arms"]}
    missing = sorted(declared - set(cl.PROFESSION_CONCRETE_ARMS))
    assert not missing, (
        "profession Target arms not in PROFESSION_CONCRETE_ARMS (add them so a "
        "shared aggregator reading them is policed): " + ", ".join(missing))


# --- arm-scan population is machine-derived (PR #629 review C1) ----------------

def test_arm_scan_population_is_machine_derived():
    """C1: the arm-scan population is DERIVED (every lib/*.py that calls
    iter_target_records), not a hand tuple — so a NEW aggregator is scanned without
    editing a list. It must cover the known aggregators and EXCLUDE the abstraction
    definer (occupation, which owns the arm registry)."""
    pop = set(chk.arm_scanned_modules())
    assert {"session_log", "cmd_project"} <= pop, pop
    assert "occupation" not in pop, (
        "occupation.py (the arm-registry definer) must be excluded from the scan")


def test_no_iter_target_records_caller_escapes_the_arm_scan():
    """DERIVATION regression guard (PR #629 re-review low#2): assert the scan
    population STAYS machine-derived from the ``_calls_iter_target_records``
    predicate. Because both sides here use that same predicate, an all-clear is
    trivially true WHILE the derivation is in place — the value is catching a REVERT:
    if ``_arm_scanned_paths`` is ever changed back to a hand-list that omits a real
    caller, ``scanned`` drops it while ``callers`` (recomputed from the predicate)
    keeps it, so ``escaped`` becomes non-empty and this fails. It is NOT the
    behavioral escape gate — that is ``test_no_new_arm_coupling_on_real_tree`` (a new
    caller with a real arm read fails CI there, mutation-confirmed in review)."""
    import ast
    import glob
    lib = os.path.join(os.path.dirname(__file__), "..", "lib")
    callers = set()
    for p in glob.glob(os.path.join(lib, "*.py")):
        tree = ast.parse(open(p, encoding="utf-8").read())
        if chk._calls_iter_target_records(tree):
            callers.add(os.path.splitext(os.path.basename(p))[0])
    scanned = set(chk.arm_scanned_modules())
    excluded = {os.path.splitext(m)[0] for m in chk._ARM_SCAN_EXCLUDE}
    escaped = sorted(callers - scanned - excluded)
    assert not escaped, (
        "iter_target_records callers escaping the arm scan (they will not be checked "
        "for a hardcoded arm read — add to the population or _ARM_SCAN_EXCLUDE with a "
        "rationale): " + ", ".join(escaped))


# --- reviewed-legitimate arm recovery path (PR #629 review C2) -----------------

def test_reviewed_arm_read_makes_a_read_legitimate(tmp_path, monkeypatch):
    """C2: the arm matcher is receiver-blind (fires on any x['entries']), so a legit
    read of a generic arm name off a NON-Target record (an L1 operation's entries)
    needs a recovery path or it forces a wrong remediation. A (site, arm) in
    REVIEWED_LEGITIMATE_ARM_READS classifies reviewed_correct, not new_violation.
    Proven on a synthetic tree with a monkeypatched allowlist (deterministic)."""
    path = _write(tmp_path, "aggregator.py", _SYNTH_ARM_READ)
    before = chk.find_arm_coupling(path)
    assert before[0]["status"] == "new_violation"
    monkeypatch.setattr(cl, "REVIEWED_LEGITIMATE_ARM_READS",
                        {("aggregator", "entries"): "L1 operation entries, not a Target arm"})
    after = chk.find_arm_coupling(path)
    assert len(after) == 1 and after[0]["status"] == "reviewed_correct", after
    assert after[0]["advice"] == "L1 operation entries, not a Target arm"


def test_arm_debt_and_reviewed_are_disjoint():
    # A (site, arm) cannot be BOTH pending debt and reviewed-legitimate — one or the
    # other, so the two lists cannot silently contradict (symmetry with collections).
    overlap = set(cl.KNOWN_ARM_REACH) & set(cl.REVIEWED_LEGITIMATE_ARM_READS)
    assert overlap == set(), f"an arm read is both debt and reviewed: {overlap}"


def test_reviewed_correct_reads_are_classified_correctly():
    # The human-reviewed correct reads (e-4737) report as "reviewed_correct",
    # not debt — so they are not nagged for remediation.
    by_key = {(c["verb"], c["collection"]): c["status"]
              for c in chk.find_collection_coupling()}
    assert by_key.get(("target_list", "milestones")) == "reviewed_correct"
    # ms-142 T7 (e-5162): (session_end, milestones) was REMOVED — session_end's
    # occupation release is now target-class-generic (walks claim_target_collections
    # via core.release_occupation), so it no longer reads data['milestones'] and is
    # no longer a milestone-scoped read to classify. It must NOT reappear here.
    assert ("session_end", "milestones") not in by_key
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
def cmd_pr_synthetic_coll():
    # pr_* is L3 (dev profession default) — reading data['milestones'] from a
    # dev-specific handler is legit, not a coupling violation. (milestone_* moved
    # to L2 in e-5061 step 2, so it is no longer a valid L3 example.)
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


def test_governing_verbs_attribution_crosses_module_boundary():
    """ms-127 module-aware scan: a helper's profession-collection read in ONE file
    is attributed to a profession-shared cmd_<verb> handler that lives in ANOTHER
    file. The god-module split moves cmd_<verb> handlers into cmd_<family>.py while
    the helper they call may stay in commands.py / commands_shared.py; a single-tree
    scan would miss that cmd→helper edge and silently drop the read from the ms-134
    invariant. This asserts the cross-module edge (and the single-tree negative
    control that motivates it)."""
    # file A: a helper (no cmd_ handler) that reads a profession collection.
    tree_a = ast.parse("def _synth_xwalk(d):\n    return d.get('milestones')\n")
    funcs_a = chk._build_function_index(tree_a)
    # file B: a SHARED (doc→L2) handler that calls the helper — different module.
    tree_b = ast.parse("def cmd_doc_synthxmod():\n    _synth_xwalk({})\n")
    funcs_b = chk._build_function_index(tree_b)
    trees = [("a.py", tree_a, funcs_a), ("b.py", tree_b, funcs_b)]

    # The `.get('milestones')` read is on line 2 of file A. With BOTH trees, the
    # read is attributed to doc_synthxmod (via the helper) even though the handler
    # is in the other file.
    gov = chk._governing_shared_verbs(trees, funcs_a, 2)
    assert {v for v, _s, _via in gov} == {"doc_synthxmod"}, gov
    assert all(via == "_synth_xwalk" for _v, _s, via in gov), gov

    # Negative control: scanning file A ALONE loses the handler → no attribution.
    # This is exactly the silent gap the module-aware scan closes.
    gov_single = chk._governing_shared_verbs([("a.py", tree_a, funcs_a)], funcs_a, 2)
    assert gov_single == [], gov_single


def test_scanned_paths_default_includes_family_modules_and_shared():
    """Default scan set = commands.py + commands_shared.py + every lib/cmd_*.py.
    A single-file override (test fixture / --commands-path) restricts to that one
    file so synthetic-source fixtures stay isolated from the real family modules."""
    default = chk._scanned_paths()
    bases = {os.path.basename(p) for p in default}
    assert "commands.py" in bases
    assert "commands_shared.py" in bases  # e-4316/e-4317 shared foundation is scanned
    # override → exactly the one file, nothing auto-discovered.
    assert chk._scanned_paths("/tmp/fixture_commands.py") == ["/tmp/fixture_commands.py"]


# --- classification proposal (ms-134 e-4739) -------------------------------

def test_propose_clean_ledger_has_no_gaps():
    # The live ledger is fully classified/owned (the coverage tests assert this),
    # so propose() reports nothing to classify and stays advisory-clean.
    p = cl.propose()
    assert p["ok"] is True
    assert p["gap_count"] == 0
    assert p["proposals"] == []
    # the menus travel with the result so a caller need not re-import constants
    assert set(p["scope_menu"]) == set(cl.SCOPE_LEVELS)
    assert set(p["owner_menu"]) == cl.PROFESSIONS


def test_propose_unknown_noun_verb_is_a_low_confidence_scope_gap():
    # A brand-new noun with no signal → a scope gap with an empty guess (no
    # fabricated layer) but the exact _NOUN_SCOPE edit site named.
    p = cl.propose(live={"payroll_run"}, skills_dir="/nonexistent")
    assert p["ok"] is False and p["gap_count"] == 1
    gap = p["proposals"][0]
    assert gap["kind"] == "verb" and gap["gap"] == "scope"
    assert gap["noun"] == "payroll"
    assert gap["proposed_scope"] == "" and gap["confidence"] == "low"
    edit = gap["edits"][0]
    assert edit["dict"] == "_NOUN_SCOPE" and edit["key"] == "payroll"


def test_propose_profession_token_verb_is_high_confidence_l3():
    # A noun that names a profession is the one confident signal a pure guess
    # trusts → L3/<profession> with the owner follow-up flagged in the note.
    p = cl.propose(live={"backoffice_report"}, skills_dir="/nonexistent")
    gap = p["proposals"][0]
    assert gap["proposed_scope"] == "L3"
    assert gap["proposed_owner"] == "backoffice"
    assert gap["confidence"] == "high"
    assert "_L3_NOUN_PROFESSION" in gap["edits"][0]["note"]


def test_propose_skill_profession_token_is_l3_owned(tmp_path):
    # An unclassified skill whose name carries a profession token → L3/<prof> with
    # the _SKILL_SCOPE edit site + owner follow-up.
    d = tmp_path / "skills"
    d.mkdir()
    (d / "beacon-backoffice-payroll.md").write_text("x", encoding="utf-8")
    p = cl.propose(live=set(), skills_dir=str(d))
    gap = next(g for g in p["proposals"] if g["kind"] == "skill")
    assert gap["gap"] == "scope"
    assert gap["proposed_scope"] == "L3" and gap["proposed_owner"] == "backoffice"
    assert gap["edits"][0]["dict"] == "_SKILL_SCOPE"


def test_propose_is_read_only():
    # propose() must not mutate the ledger tables (it is the read-only scaffold;
    # the write happens in the Skill's human-confirmed step).
    before = dict(cl._NOUN_SCOPE)
    cl.propose(live={"payroll_run", "backoffice_report"}, skills_dir="/nonexistent")
    assert cl._NOUN_SCOPE == before


# --- L4 skill ownership (ms-134 e-4739; the mechanism dogfoods itself) ------

def test_scope_classify_skill_is_l4_owned_by_beacon():
    # e-4739's own Skill is the first live L4 capability: project-local to the
    # Beacon repo (it edits this very ledger). Its scope/owner must resolve so it
    # is neither unclassified nor unowned.
    assert cl.skill_scope_of("beacon-scope-classify") == "L4"
    assert cl.skill_owner_of("beacon-scope-classify") == "beacon"


def test_skill_owner_of_dispatches_l3_profession_and_l4_project():
    # L3 → profession, L4 → project, shared → "". The dispatch mirrors owner_of
    # for verbs so the two axes read the same on both surfaces.
    assert cl.skill_owner_of("beacon-task") == "dev"          # L3 dev
    assert cl.skill_owner_of("beacon-sales-email") == "sales"  # L3 sales (prefix)
    assert cl.skill_owner_of("beacon-scope-classify") == "beacon"  # L4 project
    assert cl.skill_owner_of("beacon-map") == ""              # L2 shared, no owner


def test_l4_skill_is_owned_not_flagged_unowned():
    # The ownership reconciler must count the L4 skill under its project, not in
    # (unowned) — the coverage gate stays green with a live L4 capability present.
    rec = cl.reconcile_skills_ownership()
    assert "beacon-scope-classify" not in rec["unowned"]
    assert rec["by_owner"].get("beacon") == 1


# --- propose owner-gap paths (maintainability review 581: these 2 loops of
#     propose() were untested — scope gaps were, owner gaps were not) ----------

def test_propose_verb_owner_gap(monkeypatch):
    # A verb whose noun IS classified L3 but has no profession owner → an OWNER
    # gap (not a scope gap), pointing at _L3_NOUN_PROFESSION. Synthesize the state
    # (all real L3 nouns are owned) by adding an L3 noun without an owner entry.
    monkeypatch.setitem(cl._NOUN_SCOPE, "synthledger", "L3")
    p = cl.propose(live={"synthledger_add"}, skills_dir="/nonexistent")
    gap = next(g for g in p["proposals"] if g["capability"] == "synthledger_add")
    assert gap["gap"] == "owner" and gap["kind"] == "verb"
    assert gap["known_scope"] == "L3" and gap["proposed_scope"] == "L3"
    assert gap["edits"][0]["dict"] == "_L3_NOUN_PROFESSION"
    assert gap["edits"][0]["key"] == "synthledger"


def test_propose_skill_owner_gap(monkeypatch, tmp_path):
    # An L3 skill present on the live surface but with no owner → an OWNER gap
    # pointing at _SKILL_OWNER. Its name has no profession token, so low confidence.
    d = tmp_path / "skills"
    d.mkdir()
    (d / "beacon-synthledger.md").write_text("x", encoding="utf-8")
    monkeypatch.setitem(cl._SKILL_SCOPE, "beacon-synthledger", "L3")
    p = cl.propose(live=set(), skills_dir=str(d))
    gap = next(g for g in p["proposals"] if g["capability"] == "beacon-synthledger")
    assert gap["gap"] == "owner" and gap["kind"] == "skill"
    assert gap["known_scope"] == "L3"
    assert gap["confidence"] == "low"
    assert gap["edits"][0]["dict"] == "_SKILL_OWNER"


def test_propose_reports_scanned_counts():
    # scanned surfaces how many capabilities were inspected, so an empty result is
    # distinguishable from "scanner looked in the wrong place" (AX review 581).
    p = cl.propose(live={"payroll_run"}, skills_dir="/nonexistent")
    assert p["scanned"]["verbs"] == 1
    assert p["scanned"]["skills"] == 0


# --- render_proposal: the lib↔script schema contract (maintainability 581) ---

def test_render_proposal_renders_gap(capsys):
    # render_proposal() reads many propose() keys; a schema change must not break
    # it silently. Assert the human report surfaces the gap + edit site.
    prop = cl.propose(live={"payroll_run"}, skills_dir="/nonexistent")
    chk.render_proposal(prop)
    out = capsys.readouterr().out
    assert "payroll_run" in out
    assert "scope gap" in out
    assert "_NOUN_SCOPE" in out


def test_render_proposal_ok_shows_scanned(capsys):
    # The OK branch surfaces scanned counts as the "did the scanner look here?"
    # diagnostic (AX review 581).
    chk.render_proposal({"ok": True, "gap_count": 0, "proposals": [],
                         "scope_menu": {}, "owner_menu": [],
                         "scanned": {"verbs": 305, "skills": 57}})
    out = capsys.readouterr().out
    assert "no open gaps" in out
    assert "305 verbs" in out and "57 skills" in out
