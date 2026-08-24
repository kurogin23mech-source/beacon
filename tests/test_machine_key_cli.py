"""CLI dispatcher tests for `beacon machine-key issue|list|revoke` (ms-151 / e-5474).

Stubs api_client + _get_api_client so cmd_machine_key_* is exercised end-to-end at
the dispatcher level: env-var → arg validation → api_client call → human/JSON output.
Mirrors tests/test_dm_respond_cli.py's _StubApiClient / stub_commands pattern.

Asserts:
- issue passes the label through and prints the raw token exactly once (発行時のみ).
- list renders redacted rows (active / revoked markers), and JSON mode dumps rows.
- revoke passes the key_id through; missing key_id → exit 2; 404 → single error line;
  403 (non-owner) → owner-only error line.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))

PID = "beacon-b95643"


class _StubApiClient:
    """Records calls; returns a canned response or raises the given message."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.issue_response = {
            "key": "bmk.beacon-b95643.kid123.s3cr3t",
            "machine_key": {"key_id": "kid123", "label": "PE Lambda",
                            "created_at": "2026-08-24T12:00:00Z",
                            "revoked_at": None, "revoked": False},
        }
        self.list_response = {"machine_keys": [
            {"key_id": "k2", "label": "b", "created_at": "2026-08-24T12:00:00Z",
             "revoked_at": None, "revoked": False},
            {"key_id": "k1", "label": "", "created_at": "2026-08-24T10:00:00Z",
             "revoked_at": "2026-08-24T11:00:00Z", "revoked": True},
        ]}
        self.revoke_response = {"machine_key": {
            "key_id": "kid123", "revoked": True,
            "revoked_at": "2026-08-24T13:00:00Z"}}
        self.raise_msg = None

    def issue_machine_key(self, project_id, *, label=""):
        self.calls.append(("issue", project_id, label))
        if self.raise_msg:
            raise RuntimeError(self.raise_msg)
        return self.issue_response

    def list_machine_keys(self, project_id):
        self.calls.append(("list", project_id))
        if self.raise_msg:
            raise RuntimeError(self.raise_msg)
        return self.list_response

    def revoke_machine_key(self, project_id, key_id):
        self.calls.append(("revoke", project_id, key_id))
        if self.raise_msg:
            raise RuntimeError(self.raise_msg)
        return self.revoke_response


@pytest.fixture
def stub_commands(monkeypatch):
    sys.modules.pop("commands", None)
    import commands as cm

    state = {"client": _StubApiClient()}

    def fake_get_api_client():
        return state["client"], {"project_id": PID}

    monkeypatch.setattr(cm, "_get_api_client", fake_get_api_client)
    # clear env the commands read
    for var in ("BEACON_MACHINE_KEY_LABEL", "BEACON_MACHINE_KEY_ID", "BEACON_JSON"):
        monkeypatch.delenv(var, raising=False)
    yield cm, state
    sys.modules.pop("commands", None)


def test_issue_passes_label_and_prints_raw_once(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    monkeypatch.setenv("BEACON_MACHINE_KEY_LABEL", "PE Lambda")
    cm.cmd_machine_key_issue()
    assert state["client"].calls == [("issue", PID, "PE Lambda")]
    out = capsys.readouterr().out
    assert out.count("bmk.beacon-b95643.kid123.s3cr3t") == 1
    assert "kid123" in out
    assert "PE Lambda" in out


def test_issue_json_mode(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    monkeypatch.setenv("BEACON_JSON", "1")
    cm.cmd_machine_key_issue()
    import json
    body = json.loads(capsys.readouterr().out)
    assert body["key"].startswith("bmk.")
    assert body["machine_key"]["key_id"] == "kid123"


def test_list_renders_active_and_revoked(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    cm.cmd_machine_key_list()
    assert state["client"].calls == [("list", PID)]
    out = capsys.readouterr().out
    assert "k2" in out and "active" in out
    assert "k1" in out and "revoked" in out
    # newest first: k2 line appears before k1.
    assert out.index("k2") < out.index("k1")


def test_revoke_passes_key_id(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    monkeypatch.setenv("BEACON_MACHINE_KEY_ID", "kid123")
    cm.cmd_machine_key_revoke()
    assert state["client"].calls == [("revoke", PID, "kid123")]
    assert "失効" in capsys.readouterr().out


def test_revoke_missing_key_id_exits_2(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    monkeypatch.setenv("BEACON_MACHINE_KEY_ID", "")
    with pytest.raises(SystemExit) as ei:
        cm.cmd_machine_key_revoke()
    assert ei.value.code == 2


def test_revoke_404_single_line(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"].raise_msg = "API error 404: not found"
    monkeypatch.setenv("BEACON_MACHINE_KEY_ID", "nope")
    with pytest.raises(SystemExit) as ei:
        cm.cmd_machine_key_revoke()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "見つかりません" in err
    assert "Traceback" not in err


def test_owner_gate_403_message(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"].raise_msg = "API error 403: Access denied"
    with pytest.raises(SystemExit) as ei:
        cm.cmd_machine_key_list()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "owner" in err
