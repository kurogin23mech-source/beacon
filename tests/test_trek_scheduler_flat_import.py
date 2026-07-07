"""ms-97 / e-2667 — Cloud Run flat-layout import regression test.

Pin the dual-form lazy import behaviour in ``lib/trek_scheduler.py``. Pre-
fix, every lazy import site tried only ``from lib.trek import ...`` which
silent-failed on Cloud Run (= WORKDIR ``/app/server`` +
``PYTHONPATH=/app/lib:/app/server``, so ``lib`` is not a package on the
path — modules are reachable as ``import trek``). The silent fallback to
``None`` made ``should_fire_executor_tick`` short-circuit to
``has_unclaim_todo(trek_doc)`` only, which returned False for any trek
with no fresh todo entries → LPS / PE got skipped from
``executor_targets`` every tick.

This test simulates the Cloud Run import context by:

  1. Adding the ``lib/`` directory itself to ``sys.path`` so the bare
     name ``trek`` resolves (= mimics ``PYTHONPATH=/app/lib``).
  2. Pre-importing ``trek`` under its bare name so it's cached.
  3. Removing both ``lib`` (the package) and ``lib.trek`` from
     ``sys.modules`` and from any import machinery — so the legacy
     ``from lib.trek import ...`` path is FORCED to fail.
  4. Re-importing ``trek_scheduler`` from the bare-name path and calling
     ``should_fire_executor_tick`` against a trek with an active claim
     stamped to a known session_id.

If the fix is in place, the function returns True (= the executor would
have been included in ``executor_targets`` this tick). If a future
refactor reverts the dual-form fallback, this test fails immediately.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest import mock


LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lib"))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _trek_with_active_claim(session_id: str) -> dict:
    """Trek doc with one ``working`` task claimed by ``session_id``.

    ms-99 / e-2833 — the tick decision helpers now read slot inventory,
    so we materialize the claim via ``scope[].claim_session_id`` (=
    SPEC 方針 4 successor of the legacy per-stamp field). Cache still
    carries the ``working`` state for resolved_state derivation.
    """
    return {
        "trek_id": "tk-flat-import-test",
        "status": "active",
        "scope": [
            {"project": "p", "task": "e-9999", "claim_session_id": session_id},
        ],
        "task_states": {
            "e-9999": {
                "state": "working",
                "updated_by_session_id": session_id,
                "updated_at": "2026-06-29T00:00:00Z",
                "last_activity_at": "2026-06-29T00:00:00Z",
            },
        },
    }


def _empty_gp(pid: str) -> dict:
    """Empty project pool — cache-authoritative for the atomic slot."""
    return {}


def _flat_layout_import_scheduler():
    """Re-import trek_scheduler under a simulated flat layout.

    Returns the freshly-imported module so the caller can exercise it.

    The mutation only affects this test's module-level state, but we
    restore ``sys.path`` and ``sys.modules`` after each call so other
    tests in the suite (= the ones that rely on the package layout) stay
    untouched.
    """
    # Save state we will mutate.
    saved_modules = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name in ("trek", "trek_scheduler", "lib", "lib.trek",
                    "lib.trek_scheduler")
    }
    saved_path = list(sys.path)
    try:
        # Step 1: ensure bare ``trek`` is reachable (= PYTHONPATH=/app/lib).
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        # Pre-import bare trek so it's cached under the bare name.
        for name in ("trek", "trek_scheduler", "lib", "lib.trek",
                     "lib.trek_scheduler"):
            sys.modules.pop(name, None)
        # Step 2: drop the repo root if it would let ``lib`` resolve as a
        # package. We want ``from lib.trek import ...`` to raise.
        sys.path = [p for p in sys.path if os.path.abspath(p) != REPO_ROOT]
        # Force-fail any future ``lib.trek`` attempt by also blocking the
        # ``lib`` name at the import-machinery level. Using a meta_path
        # finder that raises is the most robust way, but a simpler shim
        # is to inject a stub ``lib`` ModuleType with no submodules — then
        # ``from lib.trek import ...`` raises ImportError naturally.
        import types
        stub_lib = types.ModuleType("lib")
        # Mark __path__ as empty list so Python treats it as a namespace
        # package but with no findable submodules.
        stub_lib.__path__ = []  # type: ignore[attr-defined]
        sys.modules["lib"] = stub_lib
        # Step 3: import bare trek + trek_scheduler from /app/lib.
        import trek  # noqa: F401  (cached under bare name)
        ts = importlib.import_module("trek_scheduler")
        return ts
    finally:
        # Restore.
        for name in ("trek", "trek_scheduler", "lib", "lib.trek",
                     "lib.trek_scheduler"):
            sys.modules.pop(name, None)
        for name, mod in saved_modules.items():
            sys.modules[name] = mod
        sys.path[:] = saved_path


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------

def test_should_fire_executor_tick_resolves_under_flat_layout():
    """The critical case: prefix-only import silent-failed on Cloud Run.

    Pin that ``should_fire_executor_tick`` returns True for an executor
    with an active claim even when ``lib.trek`` is NOT importable but
    bare ``trek`` IS — i.e. exactly the Cloud Run runtime configuration.
    """
    sid = "sv-flat-import-test-77777"
    trek_doc = _trek_with_active_claim(sid)

    ts = _flat_layout_import_scheduler()
    # Sanity: the lazy import helper resolved the bare module.
    assert ts._import_trek() is not None, (
        "ms-97 / e-2667: _import_trek must resolve via bare ``import trek`` "
        "when ``lib.trek`` is unavailable (= Cloud Run flat layout)"
    )
    # The fix's load-bearing path: with the helper resolving, the per-
    # executor lazy-start returns True for an active-claim session.
    assert ts.should_fire_executor_tick(
        trek_doc, session_id=sid, get_project=_empty_gp,
    ) is True, (
        "ms-97 / e-2667: should_fire_executor_tick must return True for an "
        "executor with an active claim under flat (Cloud Run) layout. "
        "Regression of the dogfood-observed 'executor skip every tick' bug."
    )


def test_should_fire_executor_tick_ignores_scheduler_local_import_trek_stub():
    """ms-99 / e-2833 — Phase 2 refactor moved slot resolution out of the
    scheduler-local ``_import_trek`` fallback and into ``materialize_slots``,
    which carries its own dual-form ``trek`` import. Even if the scheduler-
    local ``_import_trek`` returns None (= a legacy payload-builder path
    can't resolve trek), the tick decisions still work because
    ``materialize_slots`` resolves state independently.

    Pin the invariant so a future refactor that re-couples the decision
    functions to the scheduler-local ``_import_trek`` immediately fails
    this test.
    """
    sys.path.insert(0, REPO_ROOT)
    if "trek_scheduler" not in sys.modules and "lib.trek_scheduler" not in sys.modules:
        from lib import trek_scheduler as _ts  # noqa: F401
    from lib import trek_scheduler as ts

    sid = "sv-scheduler-local-trek-stub"
    trek_doc = _trek_with_active_claim(sid)
    with mock.patch.object(ts, "_import_trek", return_value=None):
        assert ts.should_fire_executor_tick(
            trek_doc, session_id=sid, get_project=_empty_gp,
        ) is True, (
            "Post e-2833, should_fire_executor_tick reads slot inventory "
            "via materialize_slots, which owns its own trek import. The "
            "scheduler-local _import_trek is only used by the leader "
            "digest payload builder and does NOT gate tick decisions."
        )
