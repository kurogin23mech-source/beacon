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
    # Prints that reach the checker but are NOT success claims about the write.
    # Each entry carries the reason it is allowed. Every line is verified still
    # necessary by the stale check in collect(with_stale=True): a fixed ordering
    # must drop its line, or that line goes on exempting the NEXT regression at
    # the same spot. (The first version of this list carried 5 entries that the
    # tightened path analysis had already made dead — exactly that failure,
    # caught the moment the stale check existed.)
    #
    # These three reach the checker through the second shape it looks for: the
    # write is conditional, the print is not. The print is still true when no
    # write happened, so they are exempt — but they are listed rather than
    # excluded by a rule, because distinguishing "accurate either way" from
    # "success claim" is a judgement a parser cannot make. Following the
    # doctrine stated in scripts/check-pid-liveness.py: a false positive costs
    # one allowlist line, a false negative goes unnoticed until something breaks.
    ("commands.py", "cmd_channel_opt_out", ""):
        "a trailing footer ('Lift later with: …'); it describes how to undo, "
        "not that anything was written. Each preceding branch prints its own "
        "accurate already-set / written line.",
    ("commands.py", "cmd_sales_reply_watch_op_ensure", "reply-watch operation:"):
        "reports the operation's resulting state and says 'created' vs 'exists' "
        "explicitly; when nothing changed there was nothing to write and the "
        "line is still true.",
    ("commands.py", "cmd_sales_reply_watch_op_ensure", "  → 自動発火を有効にするには"):
        "a next-step hint about approval, not a claim about the write.",
}


# The write primitive this checker understands. Named so the scope is greppable
# and so widening it later is one edit — NOT a claim that every save-like
# primitive in the tree is covered. Known out of scope today:
# ``trek_store.save_trek`` (20+ sites in lib/cmd_trek.py) and the server-side
# stores. Widening means auditing those call sites; see the follow-up task.
SAVE_PRIMITIVES = ("save_project",)

# How many levels of module-local indirection to follow. A success line
# extracted into a helper (``_report_done(entry)``) is a refactor someone will
# make, and at depth 0 the guard would silently stop covering that function.
# Depth 1 catches the realistic shape; deeper chains remain a stated blind spot.
_RESOLVE_DEPTH = 1


def _own_body(node):
    """Statements belonging to this function, excluding nested def bodies.

    ``ast.walk`` descends into a nested ``def``, which made merely DEFINING a
    helper that calls save_project look like performing the write.
    """
    out = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                              ast.ClassDef)):
            continue
        out.append(child)
        out.extend(_own_body(child))
    return out


def _terminates(stmt) -> bool:
    """Does this statement unconditionally leave the function?"""
    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        f = stmt.value.func
        if getattr(f, "attr", None) == "exit" or getattr(f, "id", None) == "exit":
            return True
    if isinstance(stmt, ast.If):
        return bool(stmt.orelse and _block_terminates(stmt.body)
                    and _block_terminates(stmt.orelse))
    return False


def _block_terminates(stmts) -> bool:
    return any(_terminates(s) for s in stmts)


def _is_save_call(node, helpers) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if name in SAVE_PRIMITIVES:
        return True
    return bool(name and helpers.get(name, (False, False))[0])


def _direct_save(stmt, helpers) -> bool:
    """Does this ONE statement perform the write unconditionally (no branching)?

    Defining a function is not calling it: ``def _w(): save_project(d)`` must
    not count, or the write looks done at the point the helper is declared.
    (``ast.walk`` cannot express this — skipping a FunctionDef *node* still
    walks its body — so the traversal is over ``_own_body``, which stops at
    nested definitions.)
    """
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    nodes = [stmt] + _own_body(stmt)
    if any(isinstance(n, (ast.If, ast.Try, ast.For, ast.While, ast.AsyncFor))
           for n in nodes):
        return False          # branching: use _definitely_saves instead
    return any(_is_save_call(n, helpers) for n in nodes)


def _definitely_saves(stmts, helpers) -> bool:
    """Does EVERY path through this block that falls out of it pass a write?

    The predicate the first version got wrong: it asked "is there a save
    anywhere inside", which is true for a save in one arm of an ``if``, or in a
    ``try`` whose handler swallows the abort, or in a loop that may run zero
    times. Each of those leaves a path on which the write did not happen while
    the checker went on believing it had — so the success print after it was
    waved through. Both independent reviews of PR #777 reproduced exactly that.
    """
    for s in stmts:
        if _terminates(s):
            return True
        if _direct_save(s, helpers):
            return True
        if isinstance(s, ast.If):
            if not s.orelse:
                continue          # the false path writes nothing
            if (_definitely_saves(s.body, helpers)
                    and _definitely_saves(s.orelse, helpers)):
                return True
        elif isinstance(s, ast.Try):
            # finally always runs; otherwise the try body only counts when every
            # handler also saves or leaves (an exception must not route past it).
            if s.finalbody and _definitely_saves(s.finalbody, helpers):
                return True
            if (_definitely_saves(s.body, helpers)
                    and s.handlers
                    and all(_definitely_saves(h.body, helpers) for h in s.handlers)
                    and (not s.orelse or _definitely_saves(s.orelse, helpers))):
                return True
        elif isinstance(s, (ast.For, ast.While, ast.AsyncFor)):
            # zero iterations is a real path; only the else clause is certain.
            if s.orelse and _definitely_saves(s.orelse, helpers):
                return True
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            if _definitely_saves(s.body, helpers):
                return True
    return False


def _stdout_print_call(node) -> bool:
    """A print() that goes to stdout.

    ``file=`` is only an exemption when it actually names stderr — the rule is
    about the stream, not about the keyword being present. ``file=sys.stdout``
    is stdout, and an expression we cannot resolve is treated as in scope
    (unknown must not read as safe).
    """
    if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
        return False
    for kw in node.keywords:
        if kw.arg != "file":
            continue
        v = kw.value
        if isinstance(v, ast.Attribute) and v.attr == "stderr":
            return False
        return True       # sys.stdout, or anything we cannot resolve
    return True


def _direct_stdout_prints(stmt, helpers):
    """stdout prints this ONE statement performs unconditionally.

    A bare ``print(...)``, or a call to a module-local helper that itself
    prints to stdout (depth 1). A print nested in a conditional inside ``stmt``
    is a *maybe*, and flagging a maybe is how a checker earns a reputation for
    crying wolf.
    """
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return []
    call = stmt.value
    if _stdout_print_call(call):
        return [call]
    name = getattr(call.func, "id", None)
    if name and helpers.get(name, (False, False))[1]:
        return [call]
    return []


def _helper_table(tree, depth=_RESOLVE_DEPTH):
    """module-local function name -> (calls a write, prints to stdout).

    Resolved to ``depth`` levels so a success line moved into a helper does not
    silently fall out of the guard's reach.
    """
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies[node.name] = node
    table = {name: (False, False) for name in bodies}
    for _ in range(depth + 1):
        changed = False
        for name, fn in bodies.items():
            saves, prints = table[name]
            for n in _own_body(fn):
                for sub in ast.walk(n):
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue
                    if _is_save_call(sub, table):
                        saves = True
                    if _stdout_print_call(sub):
                        prints = True
                    callee = getattr(getattr(sub, "func", None), "id", None)
                    if callee and callee in table:
                        s2, p2 = table[callee]
                        saves = saves or s2
                        prints = prints or p2
            if (saves, prints) != table[name]:
                table[name] = (saves, prints)
                changed = True
        if not changed:
            break
    return table


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


def _allowed(filename, fnname, text, used=None) -> bool:
    """Is this print exempt? ``used`` collects the ALLOW keys that matched, so
    an entry that no longer matches anything can be reported as stale."""
    for key in ALLOW:
        f, fn, prefix = key
        if f == filename and fn == fnname and text.startswith(prefix):
            if used is not None:
                used.add(key)
            return True
    return False


def _scan(stmts, inherited, filename, fnname, hits, helpers, committed=False, used=None):
    """Walk one block, reporting stdout prints the write does not stand behind.

    A print is reported when, on the path reaching it, the function's write is
    not GUARANTEED to have run. Two shapes fall under that:

      * the print comes before the write (the historical e-6688 bug);
      * the write is conditional — one arm of an ``if``, a ``try`` whose handler
        swallows the abort, a loop that may not iterate — and the print is
        unconditional. The success line then outlives a write that did not
        happen, which is the same harm by a different route. Both independent
        reviews of PR #777 reproduced this and the first version missed it.

    ``committed`` means every path reaching here has passed the write; from
    that point the verb's result is true and nothing later is a false claim.
    """
    pending = [] if committed else list(inherited)
    for s in stmts:
        if _direct_save(s, helpers):
            _report(pending, filename, fnname, hits, used)
            pending = []
            committed = True
            continue
        if isinstance(s, (ast.If, ast.Try, ast.For, ast.While, ast.With,
                          ast.AsyncFor, ast.AsyncWith)):
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(s, attr, None) or []
                if sub:
                    _scan(sub, pending, filename, fnname, hits, helpers, committed, used)
            for h in getattr(s, "handlers", None) or []:
                _scan(h.body, pending, filename, fnname, hits, helpers, committed, used)
            if _definitely_saves([s], helpers):
                _report(pending, filename, fnname, hits, used)
                pending = []
                committed = True
            continue
        if _terminates(s):
            return
        if not committed:
            pending.extend(_direct_stdout_prints(s, helpers))


def _report(prints, filename, fnname, hits, used=None):
    for p in prints:
        text = _literal_prefix(p)
        if not _allowed(filename, fnname, text, used):
            hits.append((filename, fnname, p.lineno, text))


def _scan_function(fn, filename, hits, helpers, used=None):
    """Scan one function, then report anything still pending at its end.

    Reporting the leftovers is what catches the conditional-write shapes: a
    print that was never backed by a guaranteed write reaches the end of the
    function still pending.
    """
    if not _contains_save(fn, helpers):
        return            # a verb that never writes makes no write claims
    leftovers = []
    _scan(fn.body, [], filename, fn.name, hits, helpers, used=used)
    _scan_tail(fn.body, [], filename, fn.name, leftovers, helpers, used=used)
    for h in leftovers:
        if h not in hits:
            hits.append(h)


def _contains_save(fn, helpers) -> bool:
    for n in _own_body(fn):
        for sub in ast.walk(n):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if _is_save_call(sub, helpers):
                return True
    return False


def _scan_tail(stmts, inherited, filename, fnname, hits, helpers, committed=False, used=None):
    """Like _scan, but also reports what is still pending when the block ends."""
    pending = [] if committed else list(inherited)
    for s in stmts:
        if _direct_save(s, helpers):
            pending = []
            committed = True
            continue
        if isinstance(s, (ast.If, ast.Try, ast.For, ast.While, ast.With,
                          ast.AsyncFor, ast.AsyncWith)):
            if _definitely_saves([s], helpers):
                pending = []
                committed = True
            continue
        if _terminates(s):
            return
        if not committed:
            pending.extend(_direct_stdout_prints(s, helpers))
    _report(pending, filename, fnname, hits, used)


def collect(lib_dir: pathlib.Path = LIB, with_stale: bool = False):
    """Report prints a write does not stand behind.

    ``with_stale=True`` also returns the ALLOW entries that matched nothing —
    a fixed ordering must drop its allowlist line, or the NEXT regression on
    that same (file, function, prefix) is exempted silently. The sibling guard
    ``check-cli-help-drift.collect_help_flag_drift`` carries the same check for
    the same reason; an allowlist written under a deliberate
    "bias toward over-detection" doctrine only stays honest while something
    verifies each line is still earning its place.
    """
    hits = []
    used = set()
    for path in sorted(lib_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            # Unparseable must not read as clean (ms-160 e-6349): report it.
            hits.append((path.name, "(whole file)", 0, "could not be parsed"))
            continue
        helpers = _helper_table(tree)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan_function(fn, path.name, hits, helpers, used)
    seen, uniq = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    uniq.sort()
    if not with_stale:
        return uniq
    stale = sorted(
        "lib/{0}:{1}() -> {2!r}".format(f, fn, prefix)
        for (f, fn, prefix) in ALLOW if (f, fn, prefix) not in used)
    return uniq, stale


def main() -> int:
    hits, stale = collect(with_stale=True)
    scanned = len(list(LIB.glob("*.py")))
    if not hits and not stale:
        print("[print-before-save] OK: in lib/*.py, no success line is printed "
              "before its save_project() write ({0} files scanned). Scope is "
              "lib/ + the save_project primitive only — trek_store.save_trek "
              "and the server stores are NOT checked.".format(scanned))
        return 0
    if hits:
        print("[print-before-save] a success line is printed BEFORE its write:",
              file=sys.stderr)
        for filename, fnname, lineno, text in hits:
            print(f"  lib/{filename}:{lineno}  {fnname}()  -> {text[:70]!r}",
                  file=sys.stderr)
        print("", file=sys.stderr)
        print("  save_project() exits non-zero when the lost-update guard trips, so a line", file=sys.stderr)
        print("  printed above it reports a write that never happened (ms-160 e-6688).", file=sys.stderr)
        print("  -> move the print BELOW save_project(), or add it to ALLOW in", file=sys.stderr)
        print("     scripts/check-print-before-save.py with the reason it is not a", file=sys.stderr)
        print("     success claim about that write.", file=sys.stderr)
    if stale:
        print("[print-before-save] ALLOW entries that no longer match anything:",
              file=sys.stderr)
        for row in stale:
            print("  " + row, file=sys.stderr)
        print("", file=sys.stderr)
        print("  The ordering they excused is fixed (or the code moved), so the line", file=sys.stderr)
        print("  now only hides the NEXT regression at that same spot.", file=sys.stderr)
        print("  -> delete the entry from ALLOW, or correct it to match where the", file=sys.stderr)
        print("     print actually lives now.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
