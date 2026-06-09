"""Unit tests for `bus send` cross-project pre-flight (ms-60 follow-up).

The 2026-06-09 dogfood surfaced a silent-misroute pattern: the sender's
cwd-derived project_id and the recipient's polling project_id can differ,
and the send goes to the wrong bus with no signal beyond a missing
`delivered` receipt. `_validate_recipient_project` closes that gap by
looking up the recipient in the cross-project discovery and auto-routing.

These tests stub out `discover_and_aggregate` so we don't need real
bridges, then exercise the decision matrix:

  * non-dm channel        → no change
  * empty recipient       → no change
  * skip env set          → no change
  * found in target proj  → no change
  * found in other proj   → auto-route (or hard error in strict mode)
  * not found anywhere    → soft warn, no route change
  * discovery raises      → no change (defensive bypass)
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


RECIPIENT = "cfgw5d79ll-1781009858157-94a72e40"
TARGET = "beacon-b95643"
OTHER = "trailnode-cd652b"


def _stub_discover(rows):
    """Return an object exposing discover_and_aggregate -> rows."""
    class _M:
        discover_and_aggregate = staticmethod(lambda **kw: list(rows))
    return _M


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    # Make sure each test starts from a clean env so the opt-out flags
    # don't leak.
    monkeypatch.delenv("BEACON_BUS_SKIP_TO_CHECK", raising=False)
    monkeypatch.delenv("BEACON_BUS_NO_AUTO_ROUTE", raising=False)


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------

def test_non_dm_channel_skips():
    """Broadcast channels have no fixed recipient — never re-route."""
    assert commands._validate_recipient_project(
        RECIPIENT, TARGET, "session-broadcast"
    ) == TARGET


def test_empty_recipient_skips():
    assert commands._validate_recipient_project("", TARGET, "dm") == TARGET


def test_skip_env_bypasses(monkeypatch):
    monkeypatch.setenv("BEACON_BUS_SKIP_TO_CHECK", "1")
    # Even if discovery would route elsewhere, the env bypass wins.
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        [{"session_id": RECIPIENT, "project_id": OTHER}]
    )
    assert commands._validate_recipient_project(
        RECIPIENT, TARGET, "dm"
    ) == TARGET


def test_recipient_in_target_project_unchanged():
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        [{"session_id": RECIPIENT, "project_id": TARGET}]
    )
    assert commands._validate_recipient_project(
        RECIPIENT, TARGET, "dm"
    ) == TARGET


# ---------------------------------------------------------------------------
# Auto-route case (= the dogfood bug fix)
# ---------------------------------------------------------------------------

def test_recipient_in_other_project_auto_routes(monkeypatch, capsys):
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        [{"session_id": RECIPIENT, "project_id": OTHER}]
    )
    routed = commands._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == OTHER
    err = capsys.readouterr().err
    assert "Auto-routing" in err
    assert OTHER in err
    assert TARGET in err


def test_recipient_in_other_project_strict_mode_errors(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_BUS_NO_AUTO_ROUTE", "1")
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        [{"session_id": RECIPIENT, "project_id": OTHER}]
    )
    with pytest.raises(SystemExit) as exc:
        commands._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "polls project" in err
    assert OTHER in err


def test_prefers_target_match_over_other_match(monkeypatch, capsys):
    """If the same recipient session id appears in both target and other
    projects (= aggregator quirk / dual registration), prefer the target."""
    rows = [
        {"session_id": RECIPIENT, "project_id": OTHER},
        {"session_id": RECIPIENT, "project_id": TARGET},
    ]
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        rows
    )
    routed = commands._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == TARGET
    err = capsys.readouterr().err
    # No auto-route notice should fire when the match is in the target.
    assert "Auto-routing" not in err


# ---------------------------------------------------------------------------
# Not-found case
# ---------------------------------------------------------------------------

def test_recipient_not_found_anywhere_soft_warns(capsys):
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        [{"session_id": "other-session", "project_id": OTHER}]
    )
    routed = commands._validate_recipient_project(RECIPIENT, TARGET, "dm")
    # Soft-fail: keep going against the target project; rely on the
    # receipt check to surface the actual delivery failure.
    assert routed == TARGET
    err = capsys.readouterr().err
    assert "not visible in any locally-running bridge" in err


def test_empty_discovery_soft_warns(capsys):
    """Aggregator returns no live bridges at all — still soft-warn."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([])
    routed = commands._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == TARGET
    assert "not visible" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Defensive bypass when discovery itself blows up
# ---------------------------------------------------------------------------

def test_discovery_module_missing_bypasses(monkeypatch):
    """Older install without dm_discover should not break bus send."""
    # Temporarily hide the real module to simulate an older install.
    saved = sys.modules.pop("beacon_cli.skills_helpers.dm_discover", None)
    try:
        # Use a finder that fails on the import.
        import importlib

        real_import = importlib.import_module

        def _fake_import(name, *args, **kwargs):
            if name == "beacon_cli.skills_helpers.dm_discover":
                raise ImportError("simulated missing module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", _fake_import)
        assert commands._validate_recipient_project(
            RECIPIENT, TARGET, "dm"
        ) == TARGET
    finally:
        if saved is not None:
            sys.modules["beacon_cli.skills_helpers.dm_discover"] = saved


def test_discovery_exception_bypasses(capsys):
    """psutil missing / network hiccup → don't refuse the send."""
    class _BoomModule:
        @staticmethod
        def discover_and_aggregate(**kw):
            raise RuntimeError("simulated discovery failure")

    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _BoomModule
    assert commands._validate_recipient_project(
        RECIPIENT, TARGET, "dm"
    ) == TARGET
