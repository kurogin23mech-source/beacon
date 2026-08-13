#!/usr/bin/env python3
"""check-capability-scope.py — the capability scope invariant checker (ms-134
e-4709 / e-4721).

Turns the L0..L4 sharing-scope ledger (``lib/capability_ledger.py``) from a
description into an ENFORCED norm. Three checks:

  1. Coverage (e-4709 AC "every capability classified, none unclassified"):
     every live CLI verb must resolve to a scope. A verb under a new noun the
     rules do not know is reported as unclassified.

  2. Dependency invariant — symbol reach (e-4709/e-4720 core): a profession-
     SHARED capability (scope L1/L2) must NOT reach into a profession-specific
     concrete — ``core.save_entry`` / ``core.find_target_milestone`` (the dev
     milestone recorder/resolver). Shared capabilities record through the
     abstraction ``occupation.record_target_entry`` instead. This is the boundary
     e-4720 closed for ``doc``; the checker stops it from re-opening. Like (3),
     an accepted-pending reach (a class-derived verb promoted L2 whose recorder
     is not yet abstracted) is an allowlisted ratchet (KNOWN_SYMBOL_REACH,
     reported as pending debt, owner MS named inline); a NEW one fails (ms-134
     e-5061).

  3. Dependency invariant — collection coupling (e-4740): a profession-SHARED
     capability must NOT read a profession-specific project-data collection
     directly (``data["milestones"]`` etc.) — the same leak as (2) but expressed
     as a raw dict read the symbol check cannot see. Existing couplings are an
     allowlisted ratchet (reported as pending debt); a NEW one fails the checker.

  3b. Dependency invariant — arm-name coupling (ms-142 e-5012): a shared-frame
     aggregator module (``lib/session_log.py`` …) that walks Target records
     through ``occupation.iter_target_records`` must NOT then read a hardcoded
     profession ARM name off each record (``tgt["entries"]``) — the coupling one
     level deeper than (3): the enumeration is abstracted but the arm is not, so a
     sales Target's work under ``activities`` / ``communications`` is dropped. Keyed
     by (module, arm) since these reads are not CLI-verb-attributed. Existing arm
     reads are an allowlisted ratchet (``KNOWN_ARM_REACH``, pending debt); a NEW one
     fails the checker. Fix = route through ``occupation.profession_manifest``.

  4. Distribution exclusion (e-5062): no L0 (Beacon-product-operation, 非配布) verb
     may appear in the shipped dispatch surface (``enumerate_live_verbs`` = the
     wheel-packaged commands.py/dispatch.py dispatch). Since source dispatch ==
     shipped dispatch, this is verb-granular and needs no built wheel. Empty today
     (no verb is L0 after e-5061); a guard against a future L0 verb being wired into
     public dispatch. (Skill-side distribution exclusion is a deferred follow-up.)

The invariant scan uses the AST (not text/grep) so a mention of the forbidden
symbol inside a comment or docstring is NOT a false hit — only a real call is.
A call inside a private helper (``_foo``) is attributed to the scope of the
cmd_<verb> handlers that call that helper (transitive), so a shared handler
cannot dodge the check by moving the concrete call into a helper. NOTE: this is
ONE level (cmd → helper); a helper → helper → concrete chain is not followed.
commands.py has no such 2-level chain today; if one is introduced, deepen
``_cmd_handlers_calling`` to walk helper-to-helper edges.

usage:
  python3 scripts/check-capability-scope.py            # human report
  python3 scripts/check-capability-scope.py --json     # machine-readable
  python3 scripts/check-capability-scope.py --propose  # classification proposal
  python3 scripts/check-capability-scope.py --propose --json  # (for the Skill)
exit 0 = clean / 1 = coverage gap or invariant violation (CI / pre-commit gate)

--propose (ms-134 e-4739) is the AUTHORING-TIME face of the same gaps: instead of
just failing with "UNCLASSIFIED", it emits, per gap, the exact ledger edit site +
the L0..L4 menu + a best-effort guess, for the /beacon-scope-classify Skill to turn
into an AI-proposes → human-confirms classification. It is READ-ONLY and ADVISORY:
always exits 0 (it does not gate — the default mode above is the gate). Because it
exits 0 whether or not gaps exist, an automation must branch on the JSON ``ok``
field (false = gaps exist), NOT on the exit code (AX review 581).
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import capability_ledger as cl  # noqa: E402
import work_model as _wm  # noqa: E402  (canonical Target id-prefix table, e-5253)


def _scanned_paths(commands_path: str = "") -> list:
    """Files the invariant scan walks.

    Default (production / CI / pre-commit): ``commands.py`` + ``commands_shared.py``
    + every extracted family module ``lib/cmd_<family>.py``. The god-module split
    (ms-127) relocates ``cmd_<verb>`` handlers and their helpers out of
    ``commands.py``; a single-file scan would lose sight of a profession-collection
    read the moment its handler or helper moved to a family module — a silent gap
    in the ms-134 invariant. Scanning the whole family set keeps the handler→helper
    attribution (``_governing_shared_verbs``) working across module boundaries.

    When ``commands_path`` is given (test fixtures / ``--commands-path`` override)
    the scan is restricted to THAT single file, preserving the fixture isolation
    the synthetic-source tests rely on (a fixture must not pick up reads from the
    real family modules)."""
    if commands_path:
        return [commands_path]
    lib = os.path.join(REPO, "lib")
    candidates = [os.path.join(lib, "commands.py"),
                  os.path.join(lib, "commands_shared.py")]
    candidates += sorted(glob.glob(os.path.join(lib, "cmd_*.py")))
    seen, out = set(), []
    for p in candidates:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.exists(p):
            seen.add(ap)
            out.append(p)
    return out


def _load_trees(paths: list) -> list:
    """Return ``[(rel_path, tree, funcs)]`` for each scanned file — rel_path is
    relative to the repo root, used for human-readable violation locations
    (``@ lib/cmd_session.py:120``) now that reads can live outside commands.py."""
    out = []
    for p in paths:
        src = open(p, encoding="utf-8").read()
        tree = ast.parse(src)
        rel = os.path.relpath(p, REPO)
        out.append((rel, tree, _build_function_index(tree)))
    return out


# --- AST helpers -----------------------------------------------------------

def _forbidden_attr(node: ast.AST) -> str:
    """Return ``<module>.<attr>`` when ``node`` is a call to one of the forbidden
    profession-concrete symbols (dev: ``core.save_entry`` /
    ``core.find_target_milestone``; sales: ``sales_entities.activity_add`` etc.),
    else ``""``. Matches ``module.attr(...)`` — the way commands.py invokes them.
    Both profession sides are checked so the invariant is symmetric (ms-134)."""
    if not isinstance(node, ast.Call):
        return ""
    fn = node.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        dotted = f"{fn.value.id}.{fn.attr}"
        if dotted in cl.PROFESSION_CONCRETE_SYMBOLS:
            return dotted
    return ""


def _build_function_index(tree: ast.AST) -> list:
    """Return [(name, start_lineno, end_lineno)] for every top-level and nested
    function, innermost last-wins handled by the caller via range containment."""
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            funcs.append((node.name, node.lineno, end))
    return funcs


def _enclosing_function(funcs: list, lineno: int) -> str:
    """Return the name of the innermost function whose line range contains
    ``lineno`` (smallest containing range wins), or ``""``."""
    best, best_span = "", None
    for name, start, end in funcs:
        if start <= lineno <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _cmd_handlers_calling(tree: ast.AST, helper: str) -> list:
    """Return the cmd_<verb> handler names whose body calls ``helper`` (by bare
    name or ``self.helper`` — commands.py uses module-level helpers so bare name
    is the norm). Used to attribute a concrete call inside a helper back to the
    handlers that reach it."""
    callers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name) and f.id == helper:
                        callers.append(node.name)
                        break
    return callers


def _cmd_handlers_calling_any(trees: list, helper: str) -> list:
    """``cmd_<verb>`` handlers across ALL scanned trees that call ``helper`` by
    bare name. Cross-module because the god-module split can place the handler
    (e.g. ``cmd_session_end`` in ``cmd_session.py``) and the helper it calls
    (e.g. ``_release_all_occupations_for_session`` in ``commands.py`` /
    ``commands_shared.py``) in different files — a single-tree search would miss
    the edge and drop the read from attribution."""
    callers = []
    for _rel, tree, _funcs in trees:
        callers.extend(_cmd_handlers_calling(tree, helper))
    return callers


def _verb_of_handler(handler: str) -> str:
    """``cmd_task_done`` -> ``task_done`` (the dispatch key). ``""`` for a
    non-handler name."""
    return handler[4:] if handler.startswith("cmd_") else ""


def _governing_shared_verbs(trees: list, file_funcs: list, lineno: int) -> list:
    """Return ``[(verb, scope, via)]`` for the profession-SHARED (L1/L2) cmd
    handlers whose scope governs the node at ``lineno`` — either the handler
    directly, or (one level) every ``cmd_`` handler that calls the helper the
    node lives in. Shared attribution used by BOTH invariant checks (the symbol
    reach and the collection read), so they attribute a call/read to a capability
    identically.

    ``file_funcs`` is the function index of the file the node lives in, so the
    enclosing function is resolved within the node's own module; the cmd→helper
    edge is then searched across ALL ``trees`` (the handler may live in a
    different family module than the helper — ms-127 module-aware scan)."""
    encloser = _enclosing_function(file_funcs, lineno)
    candidates = []  # (verb, via)
    if encloser.startswith("cmd_"):
        candidates.append((_verb_of_handler(encloser), ""))
    elif encloser:
        for h in _cmd_handlers_calling_any(trees, encloser):
            candidates.append((_verb_of_handler(h), encloser))
    out = []
    for verb, via in candidates:
        scope = cl.scope_of(verb)
        if cl.is_profession_shared(scope):
            out.append((verb, scope, via))
    return out


def _collection_key(node: ast.AST) -> str:
    """Return the profession-concrete collection key when ``node`` READS one
    directly — ``data["milestones"]`` (Subscript load) or ``obj.get("milestones")``
    (a ``.get`` Call) for a key in ``PROFESSION_CONCRETE_COLLECTIONS`` — else
    ``""``. Matched via the AST so a string literal in a comment/docstring is not
    a false hit, mirroring ``_forbidden_attr``.

    Only a Subscript in LOAD context counts (maintainability review 2026-08-03):
    a WRITE to a same-named local dict (``result["milestones"] = [...]`` /
    ``del d["milestones"]``) is not a reach into project data and must not be a
    false positive. ``.get`` is inherently a read, so no ctx guard is needed
    there."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str) \
                and sl.value in cl.PROFESSION_CONCRETE_COLLECTIONS:
            return sl.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args:
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
                and a0.value in cl.PROFESSION_CONCRETE_COLLECTIONS:
            return a0.value
    return ""


def _arm_key(node: ast.AST) -> str:
    """Return the profession-concrete ARM name when ``node`` READS one off a
    record — ``tgt["entries"]`` (Subscript load) or ``tgt.get("entries")`` (a
    ``.get`` Call) for a key in ``PROFESSION_CONCRETE_ARMS`` — else ``""``. The
    arm-name twin of ``_collection_key`` (ms-142 e-5012): same AST-only matching
    (a string literal in a comment/docstring is not a hit) and same LOAD-only
    Subscript guard (a WRITE to a same-named local, ``d["entries"] = [...]``, is
    not a reach). ``.get`` is inherently a read, so no ctx guard there."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str) \
                and sl.value in cl.PROFESSION_CONCRETE_ARMS:
            return sl.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args:
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
                and a0.value in cl.PROFESSION_CONCRETE_ARMS:
            return a0.value
    return ""


# The arm-name scan population is DERIVED, not hand-listed (PR #629 review C1).
# The scanned set = every lib/*.py module that CALLS occupation.iter_target_records.
# That call is the exact membership signal: a module walking Target records through
# the enumeration abstraction has ALREADY dropped its collection coupling, so a
# hardcoded arm name is its SOLE residual profession coupling — precisely the leak
# the collection scan is blind to. Deriving the population by AST makes the
# forcing-function claim REAL: a NEW shared-frame aggregator is scanned the moment
# it lands, not contingent on someone remembering to append it to a tuple (the
# original hand-tuple could silently pass a new arm read — C1). A CLI L2 verb that
# still reads data['milestones'] directly is NOT a caller, so it is correctly
# EXCLUDED — its arm read rides with its collection-coupling remediation
# (KNOWN_COLLECTION_COUPLING, ms-143), never double-tracked here.
#
# occupation.py is the one caller EXCLUDED: it is the abstraction DEFINER (it owns
# the arm registry _ARM_ROLES / TARGET_DECOMPOSITION and walks arms by design), so
# its arm handling is the source of truth, not a coupling to police.
_ARM_SCAN_EXCLUDE = {"occupation.py"}


def _calls_iter_target_records(tree: ast.AST) -> bool:
    """True when ``tree`` contains a CALL to ``iter_target_records`` (bare or
    ``occupation.iter_target_records``). AST-based, so a comment/docstring mention
    (e.g. capability_ledger's prose) is NOT a false positive — only a real call
    makes a module a Target aggregator subject to the arm scan."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "iter_target_records") \
                    or (isinstance(f, ast.Name) and f.id == "iter_target_records"):
                return True
    return False


def arm_scanned_modules() -> list:
    """Return the derived arm-scan population as module stems (PR #629 C1) — every
    lib/*.py that calls ``iter_target_records`` minus ``_ARM_SCAN_EXCLUDE``. Exposed
    so a reconcile test can pin that the population is machine-derived and covers
    the known aggregators (session_log / cmd_project) without a hand-maintained
    list that can rot."""
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in _arm_scanned_paths())


def _arm_scanned_paths(arm_path: str = "") -> list:
    """Files the arm-name scan walks — DERIVED (PR #629 C1): every ``lib/*.py`` that
    CALLS ``iter_target_records`` (a shared-frame Target aggregator), minus
    ``_ARM_SCAN_EXCLUDE`` (occupation.py, the abstraction definer). So a new
    aggregator is scanned automatically — the population cannot rot behind a hand
    tuple. ``arm_path`` (a single path) overrides for test fixtures, mirroring
    ``_scanned_paths``' ``commands_path`` isolation."""
    if arm_path:
        return [arm_path]
    out = []
    for p in sorted(glob.glob(os.path.join(REPO, "lib", "*.py"))):
        if os.path.basename(p) in _ARM_SCAN_EXCLUDE:
            continue
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        if _calls_iter_target_records(tree):
            out.append(p)
    return out


# --- the checks ------------------------------------------------------------

def check_coverage() -> dict:
    """Coverage check: return the reconcile result (unclassified + per-scope)."""
    return cl.reconcile()


# --- ratchet scan table (ms-142 e-5143) ------------------------------------
# The scanner half of ``capability_ledger.RATCHET_FAMILIES``: each family binds
# its AST ``key_extractor`` (node → concrete token, "" for a miss) and its
# ``attribution`` (how the ratchet key's first element is derived from the call
# site). Two attribution modes cover all three reach classes:
#   * ``"verbs"`` — the read is attributed to the profession-SHARED cmd handler(s)
#     governing the line (collection reads / symbol calls live on the CLI-verb
#     surface). Key = (verb, token); the population is the commands.py family trees.
#   * ``"site"`` — the read is attributed to the shared-frame MODULE it lives in
#     (arm reads live in aggregator modules, not a CLI verb). Key = (site, token);
#     the population is the ``iter_target_records``-calling lib modules.
# The classify (reviewed→debt→new + advice) is the ledger's single ``classify_reach``.
# A new reach family is one row HERE + one row in ``cl.RATCHET_FAMILIES`` — no new
# per-family scan function (that 3-way duplication is what this removed).
# ``output_key`` = the dict key the token is emitted under in the returned hits
# (matches the family's legacy field name so consumers/tests are unchanged); it is
# NOT the family name or an AST attribute. The family names here MUST equal
# ``cl.RATCHET_FAMILIES``' keys — ``test_ratchet_family_tables_agree`` pins the two
# in sync so adding one table's row without the other fails CI, not at runtime.
_RATCHET_SCAN = {
    "symbol": {"output_key": "symbol", "attribution": "verbs",
               "key_extractor": _forbidden_attr, "population": _scanned_paths},
    "collection": {"output_key": "collection", "attribution": "verbs",
                   "key_extractor": _collection_key, "population": _scanned_paths},
    "arm": {"output_key": "arm", "attribution": "site",
            "key_extractor": _arm_key, "population": _arm_scanned_paths},
}


def _find_family_reaches(family: str, path: str = "") -> list:
    """Generic driver for every ratchet family (ms-142 e-5143): walk the family's
    population, extract its concrete token per node, attribute it (to the governing
    shared verbs, or to the module site), and classify via the ledger's single
    ``classify_reach``. Returns the family's item dicts with byte-identical shape to
    the old per-family functions — ``verbs`` families carry
    ``{verb, scope, <token>, advice, via, file, lineno, status}``; ``site`` families
    carry ``{site, <token>, advice, file, lineno, status}`` — so every existing
    consumer/test is unchanged. Dedup + sort match the old functions exactly."""
    spec = _RATCHET_SCAN[family]
    ok, extract = spec["output_key"], spec["key_extractor"]
    hits = []
    if spec["attribution"] == "verbs":
        trees = _load_trees(spec["population"](path))
        for rel, tree, funcs in trees:
            for node in ast.walk(tree):
                token = extract(node)
                if not token:
                    continue
                for verb, scope, via in _governing_shared_verbs(
                        trees, funcs, node.lineno):
                    status, advice = cl.classify_reach(family, (verb, token))
                    hits.append({"verb": verb, "scope": scope, ok: token,
                                 "advice": advice, "via": via, "file": rel,
                                 "lineno": node.lineno, "status": status})
        dedup = lambda h: (h["verb"], h[ok], h["via"], h["file"], h["lineno"])
        order = lambda h: (h["verb"], h["file"], h["lineno"])
    else:  # "site" attribution
        for p in spec["population"](path):
            tree = ast.parse(open(p, encoding="utf-8").read())
            rel = os.path.relpath(p, REPO)
            site = os.path.splitext(os.path.basename(p))[0]
            for node in ast.walk(tree):
                token = extract(node)
                if not token:
                    continue
                status, advice = cl.classify_reach(family, (site, token))
                hits.append({"site": site, ok: token, "advice": advice,
                             "file": rel, "lineno": node.lineno,
                             "status": status})
        dedup = lambda h: (h["site"], h[ok], h["file"], h["lineno"])
        order = lambda h: (h["site"], h["file"], h["lineno"])
    seen, unique = set(), []
    for h in hits:
        k = dedup(h)
        if k not in seen:
            seen.add(k)
            unique.append(h)
    return sorted(unique, key=order)


def find_invariant_violations(commands_path: str = "") -> list:
    """Return the list of invariant violations: profession-shared (L1/L2)
    capabilities that call a profession-specific concrete symbol.

    Each violation is a dict: ``{verb, scope, symbol, advice, via, file,
    lineno}``. ``via`` names the helper when the call is transitive (else "");
    ``file`` is the module the call lives in (repo-relative) since the scan now
    spans commands.py + commands_shared.py + the family modules. ms-142 e-5143: this
    is the ``symbol`` row of the generic ratchet driver (there is no reviewed class
    for symbols — a shared verb calling a profession recorder is never exact)."""
    return _find_family_reaches("symbol", commands_path)


def find_collection_coupling(commands_path: str = "") -> list:
    """Return profession-shared (L1/L2) capabilities that read a profession-
    specific project-data collection directly (``data["milestones"]`` etc.) —
    the non-enumerated coupling the symbol denylist cannot see (ms-134 e-4740).

    Each item is a dict: ``{verb, scope, collection, advice, via, lineno,
    status}``. ``status`` is one of (self-describing — a caller filters on it
    without consulting docs, AX review 2026-08-03):
      - ``"reviewed_correct"`` — a HUMAN-reviewed correct read (the data lives
        only in that collection by design); not coupling, not debt (e-4737). The
        process-naming (not a bare "legitimate") signals the human-review gate.
      - ``"pending_debt"`` — a genuine coupling accepted pending remediation
        (ratchet allowlist).
      - ``"new_violation"`` — neither of the above; a fresh coupling that fails
        the checker.

    ``file`` (repo-relative) names the module the read lives in — the scan spans
    commands.py + commands_shared.py + the family modules (ms-127). ms-142 e-5143:
    this is the ``collection`` row of the generic ratchet driver."""
    return _find_family_reaches("collection", commands_path)


def find_arm_coupling(arm_path: str = "") -> list:
    """Return shared-frame aggregators that read a profession-specific ARM name
    off a Target record directly (``tgt.get("entries")`` etc.) — the coupling one
    level deeper than the collection read: the enumeration is abstracted through
    ``occupation.iter_target_records`` but the arm is hardcoded (ms-142 e-5012).

    Each item is a dict: ``{site, arm, advice, file, lineno, status}``. ``site``
    is the module stem (the ratchet key — arm reads are attributed to the
    shared-frame MODULE they live in, not a CLI verb; the population is derived by
    ``_arm_scanned_paths``). ``status`` is one of (self-describing, so a caller
    filters without docs):
      - ``"reviewed_correct"`` — a HUMAN-reviewed legitimate arm read (the arm is
        an L1 collection's, e.g. an operation's ``entries``, not a profession
        Target's); not coupling, not debt. Added in PR #629 review (C2): the
        name-based match is receiver-blind, so a legit read of a generic arm name
        needs a recovery path or it would force a wrong remediation. Mirrors the
        collection ratchet's ``reviewed_correct``.
      - ``"pending_debt"`` — an accepted coupling in ``KNOWN_ARM_REACH`` (owning MS
        named inline);
      - ``"new_violation"`` — a fresh arm read that fails the checker.

    ms-142 e-5143: this is the ``arm`` row of the generic ratchet driver — the only
    ``site``-attributed family (arm reads are attributed to their module, not a CLI
    verb)."""
    return _find_family_reaches("arm", arm_path)


# ---------------------------------------------------------------------------
# Iterator NARROWING detector (ms-142 e-5253 / 思想レビュー finding P1). The three
# reach families above catch a shared capability reading a profession CONCRETE. This
# catches the class BLIND to all of them AND to the positive matrix's 4 probed
# iterators: a capability that consumes ``iter_target_records`` (so it touches no
# concrete NAME) but then NARROWS the abstracted records by a dev-specific signal —
# an id-PREFIX filter or a dev-milestone STATE-vocab branch — silently dropping other
# professions' Targets. The semantic signal set lives in the ledger
# (``DEV_NARROWING_STATE_VOCAB`` / ``KNOWN_ITERATOR_NARROWING``); the id-prefixes are
# DERIVED here from the canonical ``work_model`` table (declaration-driven, so a new
# Target class's prefix is covered without editing this scanner).
# ---------------------------------------------------------------------------

def _target_id_prefixes() -> tuple:
    """The canonical Target id-prefixes (``ms-`` / ``opp-`` / ``acc-`` …), from
    ``work_model``'s PUBLIC accessor so a NEW Target class is covered without a
    hardcoded list here (e-5253 review: read the accessor, not the private table)."""
    return _wm.known_target_prefixes()


def _narrowing_hits(tree: ast.AST) -> list:
    """Return ``[(signal_kind, token, lineno)]`` for every dev-specific NARROWING of
    abstracted records in ``tree``: an id-prefix ``.startswith("ms-")`` (bare or a
    tuple of prefixes) or a comparison against a ``DEV_NARROWING_STATE_VOCAB``
    literal. AST-based, so a prefix/literal in a comment or docstring is not a hit."""
    prefixes = set(_target_id_prefixes())
    hits = []
    for node in ast.walk(tree):
        # (a) id-prefix filter: <x>.startswith("ms-") or .startswith(("ms-","op-"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "startswith":
            consts = []
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    consts.append(a.value)
                elif isinstance(a, ast.Tuple):
                    consts += [e.value for e in a.elts
                               if isinstance(e, ast.Constant)
                               and isinstance(e.value, str)]
            for v in consts:
                if v in prefixes:
                    hits.append(("id-prefix", v, node.lineno))
        # (b) dev-state-vocab branch: == / != / in / not in against a dev-milestone
        # literal — INCLUDING the membership idiom ``status in ("observing", ...)``
        # whose comparator is a Tuple/Set/List. The elts are expanded exactly like the
        # startswith tuple above: the two sister branches MUST stay symmetric or the
        # most common narrowing form slips the blind-spot detector (e-5253 leader
        # review HIGH — a blind spot in the blind-spot detector).
        if isinstance(node, ast.Compare):
            consts = []
            for c in [node.left, *node.comparators]:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    consts.append(c.value)
                elif isinstance(c, (ast.Tuple, ast.Set, ast.List)):
                    consts += [e.value for e in c.elts
                               if isinstance(e, ast.Constant)
                               and isinstance(e.value, str)]
            for v in consts:
                if v in cl.DEV_NARROWING_STATE_VOCAB:
                    hits.append(("dev-state", v, node.lineno))
    return hits


_NARROWING_ADVICE = {
    "id-prefix":
        "filtering iter_target_records by an id-prefix keeps only ONE class and "
        "drops the others (a sales project's opp-/acc- Targets). Branch on the "
        "manifest's target-class kind (occupation.profession_manifest) or handle "
        "every enumerated record, not one prefix.",
    "dev-state":
        "branching on a dev-milestone state literal assumes the dev lifecycle a "
        "sales opportunity (phases) / operation (open/closed) never has. Read the "
        "class's state model (target_state.state_model_for) instead of a hardcoded "
        "dev status.",
}


def find_iterator_narrowing(path: str = "") -> list:
    """Shared-frame aggregators (modules calling ``occupation.iter_target_records``,
    the same derived population as the arm scan) that NARROW the abstracted records
    by a dev-specific signal — an id-prefix filter or a dev-milestone state-vocab
    branch (ms-142 e-5253). Each item: ``{module, signal_kind, token, advice, file,
    lineno, status}``. ``signal_kind`` is the enum literal ``"id-prefix"`` |
    ``"dev-state"`` (the two keys of ``_NARROWING_ADVICE``); ``token`` is the concrete
    signal (the prefix ``"ms-"`` or the state literal ``"observing"``). ``status`` is
    ``"pending_debt"`` (in ``KNOWN_ITERATOR_NARROWING``) or ``"new_violation"`` (fails
    CI). ``module`` is the file stem (the ratchet key, like the arm family —
    narrowing is attributed to the aggregator MODULE, not a CLI verb).

    COARSE-by-design (like the arm scan's ``_arm_key``): ``_narrowing_hits`` flags an
    id-prefix / dev-state literal ANYWHERE in an ``iter_target_records``-consuming
    module, not only when provably applied to the iterated result — a full dataflow
    trace is not worth it here because these modules ARE Target aggregators, so such a
    literal is almost certainly narrowing Target records.

    ROUTING (single rule, e-5253 leader review — the 3 messages were inconsistent):
    ``KNOWN_ITERATOR_NARROWING`` is the pending-DEBT ratchet — a real narrowing that is
    accepted PENDING remediation (owning MS inline), one-way (remediate the handler,
    then drop the row); NEVER add a row to silence a fresh narrowing. A genuine
    FALSE-POSITIVE (a literal that is not actually narrowing Target records) has NO
    terminal state yet — the reviewed-legitimate twin of ``REVIEWED_LEGITIMATE_ARM_
    READS`` is DEFERRED to e-5274 (family-crossing alignment); today every consumer is
    clean so the allowlist is empty and the gap is latent."""
    paths = [path] if path else _arm_scanned_paths()
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        rel = os.path.relpath(p, REPO)
        stem = os.path.splitext(os.path.basename(p))[0]
        for signal_kind, token, lineno in _narrowing_hits(tree):
            status = "pending_debt" \
                if cl.is_known_iterator_narrowing(stem, token) else "new_violation"
            out.append({"module": stem, "signal_kind": signal_kind, "token": token,
                        "advice": _NARROWING_ADVICE[signal_kind], "file": rel,
                        "lineno": lineno, "status": status})
    return out


def run(commands_path: str = "", arm_path: str = "") -> dict:
    """Run all checks and return a structured result with an ``ok`` verdict.

    ``ok`` is the authoritative pass/fail (also the process exit code). It is
    False if ANY of: unclassified verbs/skills, symbol-reach ``violations``,
    ``new_collection_coupling``, ``new_arm_coupling`` (ms-142 e-5012),
    ``new_iterator_narrowing`` (ms-142 e-5253), or unowned L3/L4 verbs/skills
    (ownership axis, e-4738). A consumer that wants the full failing set must read
    the FOUR reach/narrowing families — ``violations`` (symbol reach),
    ``new_collection_coupling`` (collection read), ``new_arm_coupling`` (arm-name
    read), and ``new_iterator_narrowing`` (iterator narrowing, e-5253) — they are
    distinct families with distinct item schemas, so gate on ``ok`` rather than
    iterating one list (AX review 2026-08-03; e-5253 kept this list current)."""
    cov = check_coverage()
    skill_cov = cl.reconcile_skills()
    ownership = cl.reconcile_ownership()
    skill_ownership = cl.reconcile_skills_ownership()
    # viol is the FULL inventory (both statuses); split like the collection reads
    # so a NEW symbol reach fails CI while an allowlisted (ms-143-owned) one is
    # reported as pending debt (ms-134 e-5061 symbol ratchet).
    viol = find_invariant_violations(commands_path)
    new_viol = [v for v in viol if v["status"] == "new_violation"]
    pending_viol = [v for v in viol if v["status"] == "pending_debt"]
    # all_collection_reads is the FULL inventory (all three statuses), named so a
    # caller does not mistake it for a problems-only list (AX review 2026-08-03).
    all_reads = find_collection_coupling(commands_path)
    new_coupling = [c for c in all_reads if c["status"] == "new_violation"]
    pending_coupling = [c for c in all_reads if c["status"] == "pending_debt"]
    reviewed_reads = [c for c in all_reads if c["status"] == "reviewed_correct"]
    # Arm-name coupling (ms-142 e-5012) — the third reach class, scanned in the
    # shared-frame aggregator modules (session_log …) rather than the CLI-verb
    # surface. Split like the collection reads so a NEW arm read fails CI while an
    # allowlisted (KNOWN_ARM_REACH) one is reported as pending debt.
    all_arm_reads = find_arm_coupling(arm_path)
    new_arm_coupling = [a for a in all_arm_reads if a["status"] == "new_violation"]
    pending_arm_coupling = [a for a in all_arm_reads if a["status"] == "pending_debt"]
    reviewed_arm_reads = [a for a in all_arm_reads if a["status"] == "reviewed_correct"]
    # Iterator NARROWING (ms-142 e-5253) — the 4th, differently-shaped reach class:
    # a capability consuming iter_target_records that narrows by a dev id-prefix /
    # state-vocab. Same derived population as the arm scan; split like the reaches so
    # a NEW narrowing fails CI while an allowlisted one is pending debt.
    all_narrowing = find_iterator_narrowing(arm_path)
    new_narrowing = [n for n in all_narrowing if n["status"] == "new_violation"]
    pending_narrowing = [n for n in all_narrowing if n["status"] == "pending_debt"]
    # Distribution exclusion (ms-134 e-5062 verbs / e-5086 skills): no L0
    # (product-operation, 非配布) capability may appear in the shipped distribution.
    # Verbs: the shipped dispatch surface. Skills: the bundled skills/ tree (every
    # *.md ships via pyproject package-data). Both empty today (nothing is L0); twin
    # guards that fail if a future L0 verb/Skill leaks into the public distribution.
    l0_leak = cl.shipped_l0_verbs()
    l0_skill_leak = cl.shipped_l0_skills()
    ok = (not cov["unclassified"] and not skill_cov["unclassified"]
          and not ownership["unowned"] and not skill_ownership["unowned"]
          and not new_viol and not new_coupling and not new_arm_coupling
          and not new_narrowing
          and not l0_leak and not l0_skill_leak)
    return {"ok": ok, "coverage": cov, "skill_coverage": skill_cov,
            "ownership": ownership, "skill_ownership": skill_ownership,
            "violations": viol,
            "new_symbol_reach": new_viol,
            "pending_symbol_reach": pending_viol,
            "all_collection_reads": all_reads,
            "new_collection_coupling": new_coupling,
            "pending_collection_coupling": pending_coupling,
            "reviewed_correct_reads": reviewed_reads,
            "all_arm_reads": all_arm_reads,
            "new_arm_coupling": new_arm_coupling,
            "pending_arm_coupling": pending_arm_coupling,
            "reviewed_correct_arm_reads": reviewed_arm_reads,
            "all_iterator_narrowing": all_narrowing,
            "new_iterator_narrowing": new_narrowing,
            "pending_iterator_narrowing": pending_narrowing,
            "l0_distribution_leak": l0_leak,
            "l0_skill_distribution_leak": l0_skill_leak}


def render_proposal(prop: dict) -> None:
    """Human-readable render of the classification proposal (ms-134 e-4739)."""
    print("capability scope classification proposal (ms-134 e-4739)")
    scanned = prop.get("scanned", {})
    if prop["ok"]:
        print("  OK: no open gaps — every capability is classified and owned. "
              "Nothing to propose.")
        print(f"      (scanned {scanned.get('verbs', '?')} verbs + "
              f"{scanned.get('skills', '?')} skills. If you expected a gap, "
              f"verify the new capability is on the live surface — a new verb in "
              f"commands.py / a new skills/<name>.md file.)")
        return
    print(f"  {prop['gap_count']} open gap(s). For each, confirm a layer/owner and "
          f"apply the edit (do this via /beacon-scope-classify — AI proposes, you "
          f"confirm, it writes the ledger).")
    print("  scope menu:")
    for lvl, desc in prop["scope_menu"].items():
        print(f"    {lvl} — {desc}")
    print(f"  owner menu (for L3 → a profession): {', '.join(prop['owner_menu'])}")
    for p in prop["proposals"]:
        guess = p["proposed_scope"] or "?"
        if p["proposed_owner"]:
            guess += f"/{p['proposed_owner']}"
        print(f"\n  - [{p['kind']} · {p['gap']} gap] {p['capability']}"
              f"  → guess {guess} ({p['confidence']} confidence)")
        print(f"      why: {p['rationale']}")
        for e in p["edits"]:
            print(f"      edit: {e['file']} :: {e['dict']}[{e['key']!r}] = "
                  f"{e['value_hint']}")
            print(f"            {e['note']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Beacon capability scope checker")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--propose", action="store_true",
                    help="emit a classification proposal for open gaps (ms-134 "
                         "e-4739); read-only, ALWAYS exits 0 — automation must "
                         "branch on the JSON `ok` field (false=gaps), not $?")
    ap.add_argument("--commands-path", default="", help="override commands.py path")
    args = ap.parse_args()

    if args.propose:
        prop = cl.propose()
        if args.json:
            print(json.dumps(prop, ensure_ascii=False))
        else:
            render_proposal(prop)
        return 0  # advisory: never gates (the default mode is the gate)

    result = run(args.commands_path)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1

    cov = result["coverage"]
    scov = result["skill_coverage"]
    print("capability scope checker (ms-134)")
    print(f"  coverage: {sum(cov['by_scope'].values())} verbs classified "
          f"{cov['by_scope']}")
    print(f"  skills:   {sum(scov['by_scope'].values())} skills classified "
          f"{scov['by_scope']}")
    own = result["ownership"]
    sown = result["skill_ownership"]
    print(f"  ownership: verbs {own['by_owner']} / skills {sown['by_owner']}")
    if result["l0_distribution_leak"]:
        print(f"  DISTRIBUTION EXCLUSION VIOLATION ({len(result['l0_distribution_leak'])}) "
              f"— L0 (non-distributed, product-operation) verbs are in the shipped "
              f"dispatch surface (ms-134 e-5062):")
        for v in result["l0_distribution_leak"]:
            print(f"    - {v} [L0] is dispatchable in the public CLI")
        print("    → an L0 capability must not ship: reclassify it (universal "
              "tooling → L1) or move it out of the wheel-packaged dispatch.")
    if result["l0_skill_distribution_leak"]:
        print(f"  DISTRIBUTION EXCLUSION VIOLATION ({len(result['l0_skill_distribution_leak'])}) "
              f"— L0 (non-distributed, product-operation) Skills are in the shipped "
              f"skills/ tree (ms-134 e-5086):")
        for s in result["l0_skill_distribution_leak"]:
            print(f"    - {s} [L0] ships in the bundled skills/ (pyproject package-data)")
        print("    → an L0 Skill must not ship. Decide by what the Skill actually does:")
        print("      · if it is instance-universal (any beacon user, incl. a pipx "
              "install, would run it): reclassify → L1 in _SKILL_SCOPE.")
        print("      · if it is genuinely Beacon-repo-only operation: move it out of "
              "the shipped skills/ tree (a non-packaged dev-tools location).")
    if cov["unclassified"]:
        print(f"  UNCLASSIFIED VERBS ({len(cov['unclassified'])}): "
              f"{', '.join(cov['unclassified'])}")
        print("    → give the new noun a scope in lib/capability_ledger.py "
              "(_NOUN_SCOPE) or a per-verb override.")
    if scov["unclassified"]:
        print(f"  UNCLASSIFIED SKILLS ({len(scov['unclassified'])}): "
              f"{', '.join(scov['unclassified'])}")
        print("    → give the skill a scope in lib/capability_ledger.py "
              "(_SKILL_SCOPE / _SKILL_PREFIX_SCOPE).")
    if own["unowned"]:
        print(f"  UNOWNED L3/L4 VERBS ({len(own['unowned'])}): "
              f"{', '.join(own['unowned'])}")
        print("    → give the L3 noun a profession in _L3_NOUN_PROFESSION "
              "(or the L4 verb a project in _L4_VERB_PROJECT).")
    if sown["unowned"]:
        print(f"  UNOWNED L3/L4 SKILLS ({len(sown['unowned'])}): "
              f"{', '.join(sown['unowned'])}")
        print("    → if the skill matches an existing prefix (e.g. beacon-sales-*): "
              "add its exact name to _SKILL_OWNER. If it introduces a NEW "
              "profession prefix (e.g. beacon-backoffice-*): add a tuple to "
              "_SKILL_OWNER_PREFIX so all future skills under it resolve too.")
    new_viol = result["new_symbol_reach"]
    pending_viol = result["pending_symbol_reach"]
    if new_viol:
        print(f"  INVARIANT VIOLATIONS ({len(new_viol)}):")
        for v in new_viol:
            via = f" (via {v['via']})" if v["via"] else ""
            print(f"    - {v['verb']} [{v['scope']}] calls {v['symbol']}"
                  f"{via} @ {v['file']}:{v['lineno']}")
            print(f"      → {v['advice']}")
    if pending_viol:
        # Accepted debt: enumerated + visible, but not a failure (symbol ratchet).
        pend_sym = sorted({(v["verb"], v["symbol"]) for v in pending_viol})
        print(f"  pending symbol-reach debt ({len(pend_sym)}, allowlisted — "
              f"remediate then drop from KNOWN_SYMBOL_REACH):")
        for verb, sym in pend_sym:
            print(f"    · {verb} calls {sym}")
    new_coupling = result["new_collection_coupling"]
    pending_coupling = result["pending_collection_coupling"]
    if new_coupling:
        print(f"  NEW COLLECTION COUPLING ({len(new_coupling)}) — a shared "
              f"capability reads a profession collection directly:")
        for c in new_coupling:
            via = f" (via {c['via']})" if c["via"] else ""
            print(f"    - {c['verb']} [{c['scope']}] reads data['{c['collection']}']"
                  f"{via} @ {c['file']}:{c['lineno']}")
            print(f"      → {c['advice']}")
    if pending_coupling:
        # Accepted debt: enumerated + visible, but not a failure (ratchet).
        pend_pairs = sorted({(c["verb"], c["collection"]) for c in pending_coupling})
        print(f"  pending collection-coupling debt ({len(pend_pairs)}, "
              f"allowlisted — remediate then drop from KNOWN_COLLECTION_COUPLING):")
        for verb, coll in pend_pairs:
            print(f"    · {verb} reads data['{coll}']")
    reviewed = result["reviewed_correct_reads"]
    if reviewed:
        # Human-reviewed correct reads (data lives only there by design).
        rev_pairs = sorted({(c["verb"], c["collection"]) for c in reviewed})
        print(f"  reviewed-correct reads ({len(rev_pairs)}, human-confirmed "
              f"correct — data lives only there by design, do NOT remediate):")
        for verb, coll in rev_pairs:
            print(f"    ✓ {verb} reads data['{coll}']")
    new_arm = result["new_arm_coupling"]
    pending_arm = result["pending_arm_coupling"]
    if new_arm:
        print(f"  NEW ARM COUPLING ({len(new_arm)}) — a shared-frame aggregator "
              f"reads a profession arm name off a Target record directly:")
        for a in new_arm:
            print(f"    - {a['site']} reads tgt['{a['arm']}'] @ "
                  f"{a['file']}:{a['lineno']}")
            print(f"      → {a['advice']}")
    if pending_arm:
        # Accepted debt: enumerated + visible, but not a failure (arm ratchet).
        pend_arms = sorted({(a["site"], a["arm"]) for a in pending_arm})
        print(f"  pending arm-coupling debt ({len(pend_arms)}, allowlisted — "
              f"remediate then drop from KNOWN_ARM_REACH):")
        for site, arm in pend_arms:
            print(f"    · {site} reads tgt['{arm}']")
    reviewed_arm = result["reviewed_correct_arm_reads"]
    if reviewed_arm:
        # Human-reviewed legitimate arm reads (an L1 record's arm, not a Target's).
        rev_arms = sorted({(a["site"], a["arm"]) for a in reviewed_arm})
        print(f"  reviewed-correct arm reads ({len(rev_arms)}, human-confirmed — an "
              f"L1 record's arm, not a profession Target's, do NOT remediate):")
        for site, arm in rev_arms:
            print(f"    ✓ {site} reads ['{arm}']")
    new_narrow = result["new_iterator_narrowing"]
    pending_narrow = result["pending_iterator_narrowing"]
    if new_narrow:
        print(f"  NEW ITERATOR NARROWING ({len(new_narrow)}) — a shared-frame "
              f"aggregator consumes iter_target_records then narrows by a dev signal:")
        for n in new_narrow:
            print(f"    - {n['module']} narrows by {n['signal_kind']} '{n['token']}' "
                  f"@ {n['file']}:{n['lineno']}")
            print(f"      → {n['advice']}")
    if pending_narrow:
        pend_narr = sorted({(n["module"], n["signal_kind"], n["token"])
                            for n in pending_narrow})
        print(f"  pending iterator-narrowing debt ({len(pend_narr)}, allowlisted — "
              f"remediate then drop from KNOWN_ITERATOR_NARROWING):")
        for mod, signal_kind, token in pend_narr:
            print(f"    · {mod} narrows by {signal_kind} '{token}'")
    if result["ok"]:
        print("  OK: every capability is classified and no profession-shared "
              "capability reaches a profession concrete (no NEW symbol reach or "
              "collection coupling).")
    else:
        print("  → Fix the items above (classify the verb/skill, or route the "
              "shared capability through the occupation abstraction — "
              "occupation.record_target_entry for recording, the work_model "
              "target registry for enumeration), then re-run: "
              "python3 scripts/check-capability-scope.py")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
