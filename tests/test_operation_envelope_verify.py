"""Unit tests for `beacon operation envelope verify` (ms-60 / e-1340).

Cover the AI self-check primitive: given an operation id and a requested
action, return whether the action is permitted by the current active
envelope. Mocks the api_client + cloud config helpers.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import commands  # noqa: E402


PROJECT_ID = "test-proj"
OP_ID = "op-1"


class _FakeClient:
    def __init__(self) -> None:
        self.list_returns: list[dict] = []

    def list_operation_envelopes(self, project_id, op_id, *, status=None):
        return list(self.list_returns)


@pytest.fixture
def fake_client(monkeypatch):
    fc = _FakeClient()

    def _get_api_client():
        return fc, {"project_id": PROJECT_ID}

    monkeypatch.setattr(commands, "_get_api_client", _get_api_client)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    return fc


def _set_envvars(monkeypatch, op_id=OP_ID, action="", json_mode=False):
    monkeypatch.setenv("BEACON_OPERATION_ID", op_id)
    monkeypatch.setenv("BEACON_ACTION", action)
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")


# ---------------------------------------------------------------------------
# Happy path: permitted
# ---------------------------------------------------------------------------

def test_verify_permitted_exact_match(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["deploy:v0.21.x", "extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is True
    assert result["active"] is True
    assert result["envelope_id"] == "env-1"


def test_verify_permitted_wildcard(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*", "task done:e-*"],
    }]
    _set_envvars(monkeypatch, action="extract:profile:user-42", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["permitted"] is True


def test_verify_permitted_prefix_wildcard(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["task done:e-*"],
    }]
    _set_envvars(monkeypatch, action="task done:e-1234", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["permitted"] is True


# ---------------------------------------------------------------------------
# Not permitted
# ---------------------------------------------------------------------------

def test_verify_rejected_outside_scope(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is False
    assert result["active"] is True
    assert "outside approved_actions" in result["reason"]


def test_verify_rejected_no_active_envelope(fake_client, monkeypatch, capsys):
    fake_client.list_returns = []  # no active envelope
    _set_envvars(monkeypatch, action="extract:profile:user-1", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is False
    assert result["active"] is False
    assert result["envelope_id"] is None
    assert "no active envelope" in result["reason"]


def test_verify_rejected_different_depth(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],  # 3 segments
    }]
    # Requested action has only 2 segments — must not match (matcher requires
    # equal depth).
    _set_envvars(monkeypatch, action="extract:profile", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Usage errors (exit code 2)
# ---------------------------------------------------------------------------

def test_verify_missing_op_id(monkeypatch, capsys):
    _set_envvars(monkeypatch, op_id="", action="x:y:z")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "operation id required" in capsys.readouterr().err


def test_verify_missing_action(monkeypatch, capsys):
    _set_envvars(monkeypatch, action="")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "action required" in capsys.readouterr().err


def test_verify_rejects_local_mode(monkeypatch, capsys):
    _set_envvars(monkeypatch, action="x:y:z")
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: False)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "cloud mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Human-readable output (non-JSON mode)
# ---------------------------------------------------------------------------

def test_verify_human_output_permitted(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["deploy:v0.21.x"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "permitted" in out
    assert "deploy:v0.21.x" in out


def test_verify_human_output_rejected(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v1.0")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not permitted" in out
    assert "outside approved_actions" in out


# ---------------------------------------------------------------------------
# _push_operation_trigger_to_bus — best-effort, must not throw on local mode
# ---------------------------------------------------------------------------

def test_push_operation_trigger_local_mode_safe(monkeypatch):
    """When no cloud.json exists, the helper should silently return without
    raising. Local-mode users must not see errors from this autonomous-path
    plumbing (ms-60 / e-1340)."""
    monkeypatch.setattr(
        commands, "_get_cloud_config_path",
        lambda: "/nonexistent/cloud.json",
    )
    # Must not raise.
    commands._push_operation_trigger_to_bus(
        "op-1", "test_source", {"name": "operation_check_op-1",
                                "message": "test", "created_at": "2026-06-09"},
        spec_doc_id="doc-1",
    )
