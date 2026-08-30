"""Coverage for the e-5803 independent-review fixes (PR #696).

Pins the fixes accepted from the full-PR AX + maintainability review:
  * AX-8: the allowlist-miss sentinel is private (not an importable foot-gun).
  * AX-3: render_halt_inject emits a runnable, scope-aware `beacon resume` command.
  * Maint-F4: the Claude hook's `bus_delivery is None` fallback fails CLOSED
    (every auto-execute downgraded), previously untested.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import bus_delivery as bd  # noqa: E402
import stop_signal as ss  # noqa: E402

HOOK_PATH = REPO / "bin" / "beacon-bus-inbox-hook.py"

_T1 = {"tier": "T1-system", "issuer": "beacon-system"}


def _load_claude_hook():
    spec = importlib.util.spec_from_file_location("claude_inbox_hook_e5803", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["claude_inbox_hook_e5803"] = module
    spec.loader.exec_module(module)
    return module


class TestAllowlistMissSentinelPrivate:
    def test_public_constant_is_gone(self):
        # AX-8: an importable empty-string sentinel is a foot-gun
        # (reason == bd.DOWNGRADE_ALLOWLIST_MISS aliases "unset").
        assert not hasattr(bd, "DOWNGRADE_ALLOWLIST_MISS")
        assert hasattr(bd, "_DOWNGRADE_ALLOWLIST_MISS")

    def test_allowlist_miss_reason_still_empty_for_parity(self):
        e = {"delivery": "auto-execute", "channel": "operation-trigger",
             "envelope": _T1}
        assert bd.classify_auto_execute(e, allowlist=[]) == (
            "propose-to-ai", "auto-execute", "")


class TestRenderHaltInjectResumeCommand:
    def test_global_resume_is_runnable(self):
        out = ss.render_halt_inject({"scope": "global", "reason": "x"})
        assert "beacon resume global" in out
        assert "beacon resume ..." not in out

    def test_scoped_resume_names_the_target(self):
        out = ss.render_halt_inject(
            {"scope": "scoped", "target": {"kind": "ms", "id": "ms-1"}, "reason": "x"})
        assert "beacon resume scoped --target ms:ms-1" in out
        assert "beacon resume ..." not in out


class TestClaudeHookFallbackFailsClosed:
    def test_auto_execute_downgraded_when_module_missing(self, monkeypatch):
        # Maint-F4: no shared module → fail CLOSED, never a forced Skill invoke.
        m = _load_claude_hook()
        monkeypatch.setattr(m, "_import_bus_delivery", lambda: None)
        delivery, dgf, _reason = m._classify_delivery(
            {"delivery": "auto-execute", "channel": "operation-trigger"},
            ["operation-trigger"])
        assert (delivery, dgf) == ("propose-to-ai", "auto-execute")

    def test_non_auto_execute_passes_through_when_module_missing(self, monkeypatch):
        m = _load_claude_hook()
        monkeypatch.setattr(m, "_import_bus_delivery", lambda: None)
        assert m._classify_delivery(
            {"delivery": "propose-to-ai", "channel": "dm"}, []) == (
            "propose-to-ai", "", "")
