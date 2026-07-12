"""CLI wiring of the qualitative gate in cmd_bus_send (ms-93 e-3340).

Pins that an armed autonomous reply (in_reply_to + granted budget) that the
qual gate classifies as HOLD is (a) not sent, (b) refunds the consumed budget
slot, and (c) exits non-zero — mirroring channel/bus.mjs's reply-tool wiring so
Codex armed (which replies via `beacon bus send`) gets the same safety.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import dm_qualgate  # noqa: E402


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _clear_env(monkeypatch):
    for k in ("BEACON_BUS_BUDGET_N", "BEACON_BUS_CHANNEL", "BEACON_BUS_PAYLOAD",
              "BEACON_BUS_SENDER", "BEACON_BUS_DELIVERY", "BEACON_JSON",
              "BEACON_BUS_IN_REPLY_TO", "BEACON_QUALGATE_OFF", "BEACON_BUS_ACTION"):
        monkeypatch.delenv(k, raising=False)


class _RecordingClient:
    """Client that fails the test if a send is attempted (gate must stop it)."""
    def __init__(self):
        self.sent = False

    def issue_bus_envelope(self, *a, **kw):
        raise RuntimeError("API error 404: legacy")  # force legacy path, no envelope

    def post_bus_event(self, *a, **kw):
        self.sent = True
        raise AssertionError("post_bus_event must NOT be called when gate holds")


def test_cli_qual_gate_holds_and_refunds(project_dir, monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()

    # Force the gate to HOLD regardless of routing (the classifier/route mocking
    # would otherwise need directory data). We pin the *wiring*: hold → refund +
    # exit + no send. The classify↔JS parity is pinned in test_dm_qualgate_parity.
    monkeypatch.setattr(
        dm_qualgate, "evaluate_outbound_qual_gate",
        lambda cat, armed: {"hold": True, "category": "external",
                            "reason": "external_needs_human"},
    )
    client = _RecordingClient()
    monkeypatch.setattr(commands, "_get_api_client",
                        lambda: (client, {"project_id": "p"}))
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")

    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_send()
    assert exc.value.code == 1
    assert client.sent is False, "held reply must not be sent"

    # Budget was consumed (0→1) then refunded on hold → back to 0.
    budget = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert budget["used"] == 0, "held reply must refund the consumed slot"

    err = capsys.readouterr().err
    assert "reply held" in err


def test_cli_qual_gate_off_escape_hatch(project_dir, monkeypatch, capsys):
    # BEACON_QUALGATE_OFF=1 skips the gate entirely (parity with the JS side).
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_QUALGATE_OFF", "1")

    called = {"gate": False}

    def _should_not_run(cat, armed):
        called["gate"] = True
        return {"hold": True, "category": "external", "reason": "x"}
    monkeypatch.setattr(dm_qualgate, "evaluate_outbound_qual_gate", _should_not_run)

    # A client whose send raises so we don't need real cloud — we only assert
    # the gate was skipped (called stays False) before the send is attempted.
    class _Boom:
        def issue_bus_envelope(self, *a, **kw):
            raise RuntimeError("API error 404: legacy")

        def post_bus_event(self, *a, **kw):
            raise RuntimeError("cloud down")
    monkeypatch.setattr(commands, "_get_api_client",
                        lambda: (_Boom(), {"project_id": "p"}))
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")

    with pytest.raises(RuntimeError):
        commands.cmd_bus_send()
    assert called["gate"] is False, "BEACON_QUALGATE_OFF=1 must skip the gate"
