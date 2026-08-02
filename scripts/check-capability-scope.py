#!/usr/bin/env python3
"""check-capability-scope.py — the capability scope invariant checker (ms-134
e-4709 / e-4721).

Turns the L0..L4 sharing-scope ledger (``lib/capability_ledger.py``) from a
description into an ENFORCED norm. Two checks:

  1. Coverage (e-4709 AC "every capability classified, none unclassified"):
     every live CLI verb must resolve to a scope. A verb under a new noun the
     rules do not know is reported as unclassified.

  2. Dependency invariant (e-4709/e-4720 core): a profession-SHARED capability
     (scope L1/L2) must NOT reach into a profession-specific concrete —
     ``core.save_entry`` / ``core.find_target_milestone`` (the dev milestone
     recorder/resolver). Shared capabilities record through the abstraction
     ``occupation.record_target_entry`` instead. This is the boundary e-4720
     closed for ``doc``; the checker stops it from re-opening.

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
exit 0 = clean / 1 = coverage gap or invariant violation (CI / pre-commit gate)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import capability_ledger as cl  # noqa: E402


def _commands_path() -> str:
    return os.path.join(REPO, "lib", "commands.py")


# --- AST helpers -----------------------------------------------------------

def _forbidden_attr(node: ast.AST) -> str:
    """Return ``core.<attr>`` when ``node`` is a call to one of the forbidden
    dev-concrete symbols (``core.save_entry`` / ``core.find_target_milestone``),
    else ``""``. Matches ``core.attr(...)`` — the way commands.py invokes them."""
    if not isinstance(node, ast.Call):
        return ""
    fn = node.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
            and fn.value.id == "core":
        dotted = f"core.{fn.attr}"
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


def _verb_of_handler(handler: str) -> str:
    """``cmd_task_done`` -> ``task_done`` (the dispatch key). ``""`` for a
    non-handler name."""
    return handler[4:] if handler.startswith("cmd_") else ""


# --- the two checks --------------------------------------------------------

def check_coverage() -> dict:
    """Coverage check: return the reconcile result (unclassified + per-scope)."""
    return cl.reconcile()


def find_invariant_violations(commands_path: str = "") -> list:
    """Return the list of invariant violations: profession-shared (L1/L2)
    capabilities that call a profession-specific concrete symbol.

    Each violation is a dict: ``{verb, scope, symbol, advice, via, lineno}``.
    ``via`` names the helper when the call is transitive (else "")."""
    path = commands_path or _commands_path()
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = _build_function_index(tree)

    violations = []
    for node in ast.walk(tree):
        dotted = _forbidden_attr(node)
        if not dotted:
            continue
        encloser = _enclosing_function(funcs, node.lineno)
        # Resolve the encloser to the capability handler(s) whose scope governs
        # this call, then flag any that are profession-shared (L1/L2).
        offenders = []  # (verb, via)
        if encloser.startswith("cmd_"):
            offenders.append((_verb_of_handler(encloser), ""))
        elif encloser:
            # a helper — attribute to every cmd_ handler that calls it.
            for h in _cmd_handlers_calling(tree, encloser):
                offenders.append((_verb_of_handler(h), encloser))
        for verb, via in offenders:
            scope = cl.scope_of(verb)
            if cl.is_profession_shared(scope):
                violations.append({
                    "verb": verb,
                    "scope": scope,
                    "symbol": dotted,
                    "advice": cl.PROFESSION_CONCRETE_SYMBOLS[dotted],
                    "via": via,
                    "lineno": node.lineno,
                })
    # de-duplicate (a helper called by several shared handlers can repeat).
    seen, unique = set(), []
    for v in violations:
        key = (v["verb"], v["symbol"], v["via"], v["lineno"])
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return sorted(unique, key=lambda v: (v["verb"], v["lineno"]))


def run(commands_path: str = "") -> dict:
    """Run all checks and return a structured result with an ``ok`` verdict."""
    cov = check_coverage()
    skill_cov = cl.reconcile_skills()
    viol = find_invariant_violations(commands_path)
    ok = (not cov["unclassified"] and not skill_cov["unclassified"]
          and not viol)
    return {"ok": ok, "coverage": cov, "skill_coverage": skill_cov,
            "violations": viol}


def main() -> int:
    ap = argparse.ArgumentParser(description="Beacon capability scope checker")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--commands-path", default="", help="override commands.py path")
    args = ap.parse_args()

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
    if result["violations"]:
        print(f"  INVARIANT VIOLATIONS ({len(result['violations'])}):")
        for v in result["violations"]:
            via = f" (via {v['via']})" if v["via"] else ""
            print(f"    - {v['verb']} [{v['scope']}] calls {v['symbol']}"
                  f"{via} @ commands.py:{v['lineno']}")
            print(f"      → {v['advice']}")
    if result["ok"]:
        print("  OK: every capability is classified and no profession-shared "
              "capability reaches a profession concrete.")
    else:
        print("  → Fix the items above (classify the verb/skill, or route the "
              "shared capability through occupation.record_target_entry), then "
              "re-run: python3 scripts/check-capability-scope.py")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
