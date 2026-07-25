"""Regression (ms-66): the trek-scheduler tick must bind ``now_iso``
unconditionally, at the top level of ``trek_scheduler_tick_endpoint``, BEFORE it
calls ``_fire_due_scheduled(now_iso)``.

Root cause this guards (found 2026-07-25 by a fresh-context investigation):
``now_iso`` was previously assigned only inside a conditional trek-quiesce branch.
Python therefore made it a function-local for the whole function, so on every
tick where that branch did NOT run (the common case), ``_fire_due_scheduled(
now_iso)`` raised ``UnboundLocalError`` — swallowed by a bare ``except`` — and the
server tick's Operation-firing path silently died on every tick since ms-107
(commit 804dfa16). Trek fanout was unaffected (it calls ``trek_mod.utcnow_iso()``
directly). Symptom: a due, envelope-approved, server_tick Operation never fired
(its ``meta.last_fired_at`` stayed null) even though the tick itself was alive.

This is an AST test (not a runtime one) because the defect is a variable-scope
bug in the endpoint, independent of the DB backend — asserting the binding is
unconditional and precedes the call catches it precisely and cheaply.
"""

from __future__ import annotations

import ast
import os

APP = os.path.join(os.path.dirname(__file__), "..", "server", "app.py")


def _tick_fn() -> ast.FunctionDef:
    tree = ast.parse(open(APP, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "trek_scheduler_tick_endpoint":
            return node
    raise AssertionError("trek_scheduler_tick_endpoint not found in server/app.py")


def _toplevel_now_iso_lineno(fn: ast.AST):
    """Line of a ``now_iso = ...`` assignment at the FUNCTION TOP LEVEL (i.e. a
    direct statement of the function body, not nested in an if/for/try)."""
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "now_iso":
                    return stmt.lineno
    return None


def _fire_due_scheduled_call_lineno(fn: ast.AST):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_fire_due_scheduled":
            return node.lineno
    return None


def test_now_iso_bound_unconditionally_before_fire_due_scheduled():
    fn = _tick_fn()
    bind_line = _toplevel_now_iso_lineno(fn)
    assert bind_line is not None, (
        "now_iso must be assigned at the TOP LEVEL of trek_scheduler_tick_endpoint "
        "(not only inside a conditional branch). Otherwise _fire_due_scheduled("
        "now_iso) raises UnboundLocalError on every tick without the quiesce "
        "branch, and the Operation-firing path silently dies (ms-66 regression)."
    )
    fire_line = _fire_due_scheduled_call_lineno(fn)
    assert fire_line is not None, "_fire_due_scheduled(now_iso) call not found"
    assert bind_line < fire_line, (
        "the top-level now_iso binding must PRECEDE the _fire_due_scheduled call "
        f"(bind at line {bind_line}, call at line {fire_line})."
    )


def test_scheduled_fire_error_is_not_silently_swallowed():
    """The bare except around _fire_due_scheduled must log the error (to stderr /
    server logger), so this class of silent-death never hides again (ms-66)."""
    src = open(APP, encoding="utf-8").read()
    # locate the except block guarding _fire_due_scheduled and assert it logs.
    assert "_fire_due_scheduled(now_iso)" in src
    # the handler references the server logger with the op-exc, right after the
    # call. Use rindex so a doc-comment mention of the call doesn't shadow the
    # real call site (the actual `try: scheduled_fires = _fire_due_scheduled...`).
    idx = src.rindex("_fire_due_scheduled(now_iso)")
    window = src[idx: idx + 600]
    assert "_server_logger.error" in window, (
        "the except handler around _fire_due_scheduled must log the exception "
        "(a bare swallow is exactly what hid the ms-66 UnboundLocalError)."
    )
