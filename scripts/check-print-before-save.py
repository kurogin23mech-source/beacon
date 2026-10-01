#!/usr/bin/env python3
"""No success line may be printed before the write that makes it true (ms-160 e-6688).

Observed 2026-09-29: ``beacon task done e-5981`` printed

    Done: [e-5981] ...                      (stdout)
    Error: Cloud project changed since it was read — aborting ...   (stderr)

and exited 1 — but the task stayed ``todo``. ``save_project`` already aborts
correctly; the defect was ordering. The completion line had been printed
*before* the write was attempted, and in a terminal the two streams interleave,
so the success line is what a reader (human or AI) walks away with. The reader
then moves on believing the write landed.

Ordering is not something a comment can hold. This checker reads each
``lib/*.py`` function and reports any **stdout** print that is reached on the
same execution path as a later ``save_project()`` with no successful write
between them. Print after the write, and an aborted write prints nothing.

Two precision rules keep the report honest — a checker that cries wolf gets
muted, and a muted checker protects nothing:

  * a path that has ALREADY passed a ``save_project()`` is clear. A verb that
    writes, reports, then writes again (``account rename`` syncs an outbox
    afterwards) is correct and must not be flagged.
  * a branch that leaves the function (``return`` / ``raise`` / ``sys.exit``)
    never reaches the save. Validation errors and no-op notices print and
    return; they are not success claims about a write.

stderr is out of scope: a warning is not a success claim.

Run standalone (CI calls it from ci-strict-drift-guards.sh, which also runs in
jobs that have no pytest — so this stays a plain script with no test framework
import; see ms-160 e-6349).
"""

import ast
import os
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parent.parent / "lib"

# Prints that precede a save but are NOT success claims about that write.
# Each entry is "<file>:<function>:<line-text-prefix>" with the reason it is
# allowed. Keep this list short and justified: every entry is a place where a
# reader could still be misled, accepted deliberately.
ALLOW = {
    ("cmd_deploy.py", "cmd_deploy_record", "Tagged: "):
        "the git tag really was created at this point — it is a report about an "
        "external action already taken, not a claim that the deploy record saved.",
    ("cmd_deploy.py", "cmd_deploy_record", "Warning: git tag "):
        "reports that the external tag step failed; true regardless of the save.",
    ("cmd_issue.py", "cmd_issue_import", "Warning: Issue #"):
        "reports GitHub's state (already closed); true regardless of the save.",
    ("cmd_pr.py", "cmd_pr_add", "PR body (prefill):"):
        "echoes the body being used as input; not a claim that anything was written.",
}


def _terminates(stmt) -> bool:
    """Does this statement leave the function unconditionally?"""
    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        f = stmt.value.func
        if getattr(f, "attr", None) == "exit" or getattr(f, "id", None) == "exit":
            return True
    if isinstance(stmt, ast.If):
        return bool(_block_terminates(stmt.body) and stmt.orelse
                    and _block_terminates(stmt.orelse))
    return False


def _block_terminates(stmts) -> bool:
    return any(_terminates(s) for s in stmts)


def _calls_save(stmts) -> bool:
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                if name == "save_project":
                    return True
    return False


def _stdout_prints(stmts):
    out = []
    for s in stmts:
        for n in ast.walk(s):
            if (isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "print"
                    and not any(k.arg == "file" for k in n.keywords)):
                out.append(n)
    return out


def _literal_prefix(node) -> str:
    """The literal text a print starts with (f-string holes become {…})."""
    if not node.args:
        return ""
    a = node.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{…}"
            for v in a.values)
    return "(expr)"


def _allowed(filename, fnname, text) -> bool:
    for (f, fn, prefix), _reason in ALLOW.items():
        if f == filename and fn == fnname and text.startswith(prefix):
            return True
    return False


def _direct_stdout_prints(stmt):
    """stdout prints that this ONE statement performs unconditionally.

    Only a bare ``print(...)`` expression statement counts. A print nested in a
    conditional / loop / handler inside ``stmt`` is a *maybe*, and flagging a
    maybe is how a checker earns its reputation for crying wolf.
    """
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return []
    call = stmt.value
    if getattr(call.func, "id", None) != "print":
        return []
    if any(k.arg == "file" for k in call.keywords):
        return []          # stderr: a warning is not a success claim
    return [call]


def _scan(stmts, inherited, filename, fnname, hits, committed=False):
    """Walk one block, carrying the prints certain to have run on the way in.

    ``inherited`` is cleared at every ``save_project()``: anything printed after
    a successful write is reporting a write that happened (``account rename``
    writes, reports, then syncs an outbox — correct, and must not be flagged).
    A statement that leaves the function ends the walk: validation errors and
    no-op notices print and return, and never reach a save.

    ``committed`` means a write has already landed on this path. After that the
    verb's result IS true, so nothing later is a false success claim and
    collection stops — ``account rename`` writes, reports, then saves an outbox
    marker, and the report belongs to the first write.

    Known limit (stated rather than papered over): a verb that writes twice and
    reports the SECOND write before it happens is not caught. No verb does that
    today; the common and observed shape is report-then-first-write.
    """
    pending = [] if committed else list(inherited)
    for s in stmts:
        if _calls_save([s]):
            # Flag first (the save may be nested inside this statement), then
            # recurse so a save deeper in still sees the same prefix.
            for p in pending:
                text = _literal_prefix(p)
                if not _allowed(filename, fnname, text):
                    hits.append((filename, fnname, p.lineno, text))
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)):
                for attr in ("body", "orelse", "finalbody"):
                    sub = getattr(s, attr, None) or []
                    if sub:
                        _scan(sub, pending, filename, fnname, hits, committed)
                for h in getattr(s, "handlers", None) or []:
                    _scan(h.body, pending, filename, fnname, hits, committed)
            pending = []
            committed = True
            continue
        if isinstance(s, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(s, attr, None) or []
                if sub:
                    _scan(sub, pending, filename, fnname, hits, committed)
            for h in getattr(s, "handlers", None) or []:
                _scan(h.body, pending, filename, fnname, hits, committed)
            if _calls_save([s]):
                committed = True
                pending = []
            continue
        if _terminates(s):
            return
        if not committed:
            pending.extend(_direct_stdout_prints(s))


def collect(lib_dir: pathlib.Path = LIB) -> list:
    hits = []
    for path in sorted(lib_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            # Unparseable must not read as clean (ms-160 e-6349): report it.
            hits.append((path.name, "(whole file)", 0, "could not be parsed"))
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan(fn.body, [], path.name, fn.name, hits)
    seen, uniq = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return sorted(uniq)


def main() -> int:
    hits = collect()
    if not hits:
        print("[print-before-save] OK: no success line is printed before the "
              "write that would make it true.")
        return 0
    print("[print-before-save] a success line is printed BEFORE its write:", file=sys.stderr)
    for filename, fnname, lineno, text in hits:
        print(f"  lib/{filename}:{lineno}  {fnname}()  -> {text[:70]!r}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  save_project() exits non-zero when the lost-update guard trips, so a line", file=sys.stderr)
    print("  printed above it reports a write that never happened (ms-160 e-6688).", file=sys.stderr)
    print("  -> move the print BELOW save_project(), or add it to ALLOW in", file=sys.stderr)
    print("     scripts/check-print-before-save.py with the reason it is not a", file=sys.stderr)
    print("     success claim about that write.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
