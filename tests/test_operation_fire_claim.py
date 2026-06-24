"""Tests for ms-95 / e-1668 + e-2350 — operation fire cloud claim gate.

Validates the CLI-side ``_claim_operation_fire_for_bus_push`` helper and
its integration with ``_auto_fire_operation_triggers``:

  * local mode (no cloud.json) → claim helper returns True (no gate)
  * cloud mode + server responds claimed=True → helper returns True
  * cloud mode + server responds claimed=False → helper returns False
  * cloud mode + server raises → helper returns True (= fail-open)
  * _auto_fire_operation_triggers skips _push_operation_trigger_to_bus
    when the claim helper returns False (= other session already fired)

The actual Firestore transaction (= ``claim_operation_fire_if_new`` in
firestore_client.py) is exercised against the real backend in integration
tests; here we mock at the api_client boundary so the unit tests stay
network-free.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


@pytest.fixture
def project_with_open_op(tmp_path, monkeypatch):
    """Bare project with a single open Operation that fires every day."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    project = {
        "name": "test",
        "milestones": [],
        "operations": [
            {
                "id": "op-1",
                "title": "Daily Op",
                "status": "open",
                "log_source": "test_log",
                "schedule": {
                    "frequency": "daily",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                },
                "entries": [],
                "meta": {},
            },
        ],
    }
    (beacon_dir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_OPERATION_TRIGGER_BROADCAST", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------

def test_local_mode_returns_true(project_with_open_op):
    """No cloud.json → no cross-cwd race surface, helper returns True so
    the bus push proceeds. Local file gate above (= os.path.exists on the
    per-cwd trigger file) is the only dedup needed in local mode."""
    assert commands._claim_operation_fire_for_bus_push("op-1") is True


def test_cloud_mode_claimed_true(project_with_open_op, monkeypatch):
    """Cloud mode + server says claimed=True → helper returns True so the
    bus push proceeds. This is the "I won the race today" path."""
    _stub_cloud_config(project_with_open_op)
    _stub_api_client(monkeypatch, response={"claimed": True,
                                            "claimed_by": "sv-me",
                                            "claimed_at": "2026-06-24T00:00:00Z"})
    assert commands._claim_operation_fire_for_bus_push("op-1") is True


def test_cloud_mode_claimed_false(project_with_open_op, monkeypatch):
    """Cloud mode + server says claimed=False → helper returns False so the
    bus push is skipped. This is the "another session already fired today"
    path that this whole gate exists for (= e-1668 N-multiplied fires,
    e-2350 4-6 min retrigger storms)."""
    _stub_cloud_config(project_with_open_op)
    _stub_api_client(monkeypatch,
                     response={"claimed": False,
                               "claimed_by": "sv-other",
                               "claimed_at": "2026-06-24T00:00:00Z"})
    assert commands._claim_operation_fire_for_bus_push("op-1") is False


def test_cloud_mode_network_failure_returns_true(project_with_open_op,
                                                 monkeypatch):
    """Cloud mode + network failure → helper returns True (= fail-open).

    SPEC §設計方針 1 fail-open policy: a duplicate operation-trigger event
    (= cheap, inbox hook is idempotent on op_id + date) is preferable to a
    silently missed daily Operation fire (= user-facing ritual broken).
    """
    _stub_cloud_config(project_with_open_op)

    def raising_post(*_args, **_kwargs):
        raise RuntimeError("simulated network failure")

    _stub_api_client(monkeypatch, post_override=raising_post)
    assert commands._claim_operation_fire_for_bus_push("op-1") is True


def test_cloud_mode_missing_credentials_returns_true(project_with_open_op,
                                                    monkeypatch):
    """Cloud mode but creds not available → helper returns True.

    Credentials may be missing legitimately on a fresh machine or during
    auth rotation. Refusing to fire would break the daily ritual; the
    duplicate-fire cost from skipping the gate is acceptable.
    """
    _stub_cloud_config(project_with_open_op)
    monkeypatch.setitem(sys.modules, "auth", _FakeAuth(missing_creds=True))
    assert commands._claim_operation_fire_for_bus_push("op-1") is True


# ---------------------------------------------------------------------------
# Integration test against _auto_fire_operation_triggers
# ---------------------------------------------------------------------------

def test_auto_fire_skips_bus_push_when_claim_lost(project_with_open_op,
                                                  monkeypatch):
    """When the claim helper says "another session already fired", the
    full _auto_fire_operation_triggers run must not call
    _push_operation_trigger_to_bus. The local trigger file is still
    written (= per-cwd UI state, harmless across cwds)."""
    monkeypatch.setattr(commands, "_claim_operation_fire_for_bus_push",
                        lambda _op_id: False)
    push_calls: list[tuple] = []
    monkeypatch.setattr(commands, "_push_operation_trigger_to_bus",
                        lambda *args, **kw: push_calls.append((args, kw)))

    commands._auto_fire_operation_triggers()

    assert push_calls == [], (
        "bus push must be suppressed when cloud claim is lost; "
        f"saw {len(push_calls)} push call(s)"
    )
    # Local trigger file should still exist — per-cwd UI surface.
    trig_path = (project_with_open_op / ".beacon" / "triggers"
                 / "operation_check_op-1.json")
    assert trig_path.exists(), (
        "local trigger file must still be written even when bus push is "
        "skipped — `beacon trigger check` output is a per-cwd concern"
    )


def test_auto_fire_pushes_bus_when_claim_won(project_with_open_op,
                                             monkeypatch):
    """Mirror test: when claim returns True, bus push happens."""
    monkeypatch.setattr(commands, "_claim_operation_fire_for_bus_push",
                        lambda _op_id: True)
    push_calls: list[tuple] = []
    monkeypatch.setattr(commands, "_push_operation_trigger_to_bus",
                        lambda *args, **kw: push_calls.append((args, kw)))

    commands._auto_fire_operation_triggers()

    assert len(push_calls) == 1, (
        f"expected 1 bus push, saw {len(push_calls)}"
    )
    # First positional arg is op_id
    assert push_calls[0][0][0] == "op-1"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _stub_cloud_config(project_dir):
    """Write a stub cloud.json so the helper takes the cloud branch."""
    cloud_path = project_dir / ".beacon" / "cloud.json"
    cloud_path.write_text(json.dumps({"project_id": "test-project-123"}))


def _stub_api_client(monkeypatch, *, response=None, post_override=None):
    """Replace api_client.ApiClient with a stub that returns ``response``
    on claim_operation_fire (or runs ``post_override`` for failure cases)."""

    class _StubClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def claim_operation_fire(self, project_id, op_id, session_id=""):
            if post_override is not None:
                return post_override(project_id, op_id, session_id)
            return dict(response or {})

    fake_api = type(sys)("api_client")
    fake_api.ApiClient = _StubClient
    monkeypatch.setitem(sys.modules, "api_client", fake_api)

    # auth.load_credentials and helpers must not fail; install a benign auth
    monkeypatch.setitem(sys.modules, "auth", _FakeAuth())

    # _resolve_active_api_url and _extract_token must return something
    monkeypatch.setattr(commands, "_resolve_active_api_url",
                        lambda: "https://test")
    monkeypatch.setattr(commands, "_extract_token", lambda _creds: "token-stub",
                        raising=False)


class _FakeAuth:
    """Minimal stand-in for the ``auth`` module."""

    def __init__(self, missing_creds: bool = False):
        self._missing = missing_creds

    def load_credentials(self):
        if self._missing:
            return None
        return {"token": "stub"}
