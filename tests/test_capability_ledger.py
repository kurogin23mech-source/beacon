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
