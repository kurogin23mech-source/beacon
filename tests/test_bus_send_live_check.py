"""Unit tests for `bus send` recipient liveness gate (ms-61 / e-1402).

2026-06-10 LPS dogfood: an AI bypassed the /beacon-dm-send Skill and
re-used a session_id from conversation memory; the sid had shutdown 1
hour earlier, the DM landed in a dead session. The Skill's per-send
discovery was the structural defence but only effective when the Skill
was used. `_check_recipient_live_health` plugs that hole at the CLI
surface (= raw `bus send` paths).

These tests stub `discover_and_aggregate` so no real bridge is required,
and exercise the decision matrix:

  * non-dm channel        → no check (no warning)
  * empty recipient       → no check (no warning)
  * opt-out env set       → no check (no warning)
  * recipient is live     → no warning
  * recipient not live    → soft warn to stderr (send still proceeds)
  * dm_discover missing   → defensive bypass (no warning, no crash)
  * discovery raises      → defensive bypass (no warning, no crash)
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


LIVE_SID = "cfgw5d79ll-1781054714814-869d1ee0"
DEAD_SID = "cfgw5d79ll-1781049495026-6f24fc57"


def _stub_discover(rows):
    """Return a fake dm_discover module exposing discover_and_aggregate."""
    class _M:
        discover_and_aggregate = staticmethod(lambda **kw: list(rows))
    return _M


def _stub_discover_raising(exc: Exception):
    class _M:
        @staticmethod
        def discover_and_aggregate(**kw):
            raise exc
    return _M


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Each test starts with no opt-out flags."""
    monkeypatch.delenv("BEACON_BUS_NO_LIVE_CHECK", raising=False)


# ---------------------------------------------------------------------------
# Skip paths (= the check correctly opts out)
# ---------------------------------------------------------------------------

def test_non_dm_channel_skips(capsys):
    """Operation-trigger / broadcast channels have no fixed sid — never check."""
    commands._check_recipient_live_health(LIVE_SID, "operation-trigger")
    assert capsys.readouterr().err == ""


def test_empty_recipient_skips(capsys):
    """No sid means we're broadcasting; nothing to check."""
    commands._check_recipient_live_health("", "dm")
    assert capsys.readouterr().err == ""


def test_opt_out_env_bypasses(monkeypatch, capsys):
    """BEACON_BUS_NO_LIVE_CHECK=1 lets CI / automation skip the check."""
    monkeypatch.setenv("BEACON_BUS_NO_LIVE_CHECK", "1")
    # Even with discovery saying the sid is dead, the env bypass wins.
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([])
    commands._check_recipient_live_health(DEAD_SID, "dm")
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Pass through (= live sid case)
# ---------------------------------------------------------------------------

def test_live_recipient_no_warning(capsys):
    """A sid present in the live+healthy rows triggers no stderr output."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([
        {"session_id": LIVE_SID, "project_id": "beacon-b95643"},
    ])
    commands._check_recipient_live_health(LIVE_SID, "dm")
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# The dogfood bug fix (= dead sid path)
# ---------------------------------------------------------------------------

def test_dead_recipient_soft_warns(capsys):
    """The 2026-06-10 LPS stale-sid case: target session has shutdown,
    not in the live directory. The check should emit a loud stderr
    warning naming the Skill, but NOT raise / exit (= soft-warn)."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([
        # Some unrelated live sid, but not DEAD_SID.
        {"session_id": LIVE_SID, "project_id": "beacon-b95643"},
    ])
    commands._check_recipient_live_health(DEAD_SID, "dm")
    err = capsys.readouterr().err
    assert "not in the live+healthy directory" in err
    assert DEAD_SID[:24] in err
    # Skill name should appear so the AI knows how to recover.
    assert "/beacon-dm-send" in err
    # Opt-out hint should be visible too.
    assert "BEACON_BUS_NO_LIVE_CHECK" in err


def test_dead_recipient_does_not_exit(capsys):
    """Even on the dead-sid path the function must return normally so the
    send can still proceed (soft-warn vs hard-refuse design decision)."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([])
    # If this raised SystemExit pytest would surface it.
    result = commands._check_recipient_live_health(DEAD_SID, "dm")
    assert result is None
    capsys.readouterr()  # drain stderr


# ---------------------------------------------------------------------------
# Defensive bypass (= the gate must never become a footgun)
# ---------------------------------------------------------------------------

def test_missing_dm_discover_module_bypasses(monkeypatch, capsys):
    """Older installs without beacon_cli.skills_helpers.dm_discover should
    silently bypass the check rather than crashing the send. Mirrors the
    sibling e-1362 test pattern (patch importlib.import_module directly,
    since the helper uses importlib not builtins.__import__)."""
    saved = sys.modules.pop("beacon_cli.skills_helpers.dm_discover", None)
    try:
        import importlib
        real_import = importlib.import_module

        def _fake_import(name, *args, **kwargs):
            if name == "beacon_cli.skills_helpers.dm_discover":
                raise ImportError("simulated: module not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", _fake_import)
        commands._check_recipient_live_health(DEAD_SID, "dm")
        assert capsys.readouterr().err == ""
    finally:
        if saved is not None:
            sys.modules["beacon_cli.skills_helpers.dm_discover"] = saved


def test_discovery_raises_bypasses(capsys):
    """If discover_and_aggregate raises (psutil missing, network hiccup),
    bypass silently — a transient discovery failure mustn't break sends."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = (
        _stub_discover_raising(RuntimeError("simulated bridge directory down"))
    )
    commands._check_recipient_live_health(DEAD_SID, "dm")
    assert capsys.readouterr().err == ""
