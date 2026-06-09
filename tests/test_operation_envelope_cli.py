"""Unit tests for `beacon operation approve / revoke` CLI commands (ms-60 / e-1339).

These tests mock the ApiClient + cloud config helpers so the command
functions can be invoked in-process without a server or network.
Endpoint-level behavior is covered separately in
``test_operation_envelopes.py``.
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
        self.approve_calls: list[dict] = []
        self.revoke_calls: list[dict] = []
        self.list_returns: list[dict] = []
        self.approve_return = {
            "envelope_id": "env-XYZ",
            "op_id": OP_ID,
            "spec_doc_id": "spec-1",
            "approved_actions": ["extract:profile:*", "task done:e-*"],
            "envelope": {
                "tier": "T2",
                "scope": OP_ID,
                "expires_at": "2056-06-09T00:00:00.000000Z",
                "signature": "sig",
            },
            "created_by": "alice@example.com",
            "status": "active",
        }
        self.revoke_return = {
            "envelope_id": "env-XYZ",
            "op_id": OP_ID,
            "status": "revoked",
            "revoke_reason": "manual stop",
            "revoked_at": "2026-06-09T18:00:00.000000Z",
        }

    def operation_approve(self, project_id, op_id, *, spec_doc_id, ttl_seconds=None):
        self.approve_calls.append({
            "project_id": project_id, "op_id": op_id,
            "spec_doc_id": spec_doc_id, "ttl_seconds": ttl_seconds,
        })
        return dict(self.approve_return)

    def operation_revoke(self, project_id, op_id, envelope_id, *, reason):
        self.revoke_calls.append({
            "project_id": project_id, "op_id": op_id,
            "envelope_id": envelope_id, "reason": reason,
        })
        return dict(self.revoke_return)

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


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def test_approve_calls_api_with_args(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    monkeypatch.setenv("BEACON_TTL_SECONDS", "")  # default
    commands.cmd_operation_approve()
    assert len(fake_client.approve_calls) == 1
    call = fake_client.approve_calls[0]
    assert call["op_id"] == OP_ID
    assert call["spec_doc_id"] == "spec-1"
    assert call["ttl_seconds"] is None
    out = capsys.readouterr().out
    assert "Approved: op-1" in out
    assert "env-XYZ" in out
    assert "extract:profile:*" in out


def test_approve_passes_ttl(fake_client, monkeypatch):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    monkeypatch.setenv("BEACON_TTL_SECONDS", "3600")
    commands.cmd_operation_approve()
    assert fake_client.approve_calls[0]["ttl_seconds"] == 3600


def test_approve_json_mode_emits_json(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    monkeypatch.setenv("BEACON_TTL_SECONDS", "")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_operation_approve()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["envelope_id"] == "env-XYZ"


def test_approve_requires_op_id(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", "")
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    with pytest.raises(SystemExit):
        commands.cmd_operation_approve()
    assert "operation id required" in capsys.readouterr().err


def test_approve_requires_spec_doc(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "")
    with pytest.raises(SystemExit):
        commands.cmd_operation_approve()
    assert "--spec" in capsys.readouterr().err


def test_approve_rejects_local_mode(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: False)
    with pytest.raises(SystemExit):
        commands.cmd_operation_approve()
    assert "cloud mode" in capsys.readouterr().err


def test_approve_rejects_invalid_ttl(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_SPEC_DOC_ID", "spec-1")
    monkeypatch.setenv("BEACON_TTL_SECONDS", "-1")
    with pytest.raises(SystemExit):
        commands.cmd_operation_approve()
    assert "positive integer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------

def test_revoke_auto_picks_active(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [
        {"envelope_id": "env-XYZ", "status": "active"},
    ]
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_ENVELOPE_ID", "")
    monkeypatch.setenv("BEACON_REASON", "manual stop")
    commands.cmd_operation_revoke()
    assert len(fake_client.revoke_calls) == 1
    assert fake_client.revoke_calls[0]["envelope_id"] == "env-XYZ"
    out = capsys.readouterr().out
    assert "Revoked: op-1 envelope env-XYZ" in out


def test_revoke_respects_explicit_envelope_id(fake_client, monkeypatch):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_ENVELOPE_ID", "env-CUSTOM")
    monkeypatch.setenv("BEACON_REASON", "")
    commands.cmd_operation_revoke()
    assert fake_client.revoke_calls[0]["envelope_id"] == "env-CUSTOM"
    # Default reason kicks in when --reason omitted.
    assert fake_client.revoke_calls[0]["reason"] == "manual revoke"


def test_revoke_errors_when_no_active(fake_client, monkeypatch, capsys):
    fake_client.list_returns = []  # no active envelopes
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setenv("BEACON_ENVELOPE_ID", "")
    monkeypatch.setenv("BEACON_REASON", "")
    with pytest.raises(SystemExit):
        commands.cmd_operation_revoke()
    err = capsys.readouterr().err
    assert "no active envelope" in err


def test_revoke_rejects_local_mode(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPERATION_ID", OP_ID)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: False)
    with pytest.raises(SystemExit):
        commands.cmd_operation_revoke()
    assert "cloud mode" in capsys.readouterr().err
