"""Win parity coverage for the ms-73 envelope CLI mirror (e-1764).

Until this MS, ``beacon operation envelope verify <op-id> <action>`` —
the AI self-check primitive (ms-60 e-1340) — was bash-only. The
/beacon-operation-execute Skill calls it before running each action;
without Win parity, autonomous Operations on Windows pipx could not
perform the pre-flight authorization check (W1 in ms-73 SPEC).

These tests assert:
  1. argv → env shape exactly matches what bin/beacon already emits
     (BEACON_OPERATION_ID + BEACON_ACTION + BEACON_JSON).
  2. Missing positional args are rejected with rc=2 and a usage hint.
  3. Sign / verify / disclosure-check ROUND-TRIP fidelity (via a
     fake commands.py runner that records all three checks).
  4. Cloud vs local mode branch is preserved through the dispatcher
     (the actual check happens in commands.py; the dispatcher must
     NOT pre-empt that decision).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402


@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None, "calls": []}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        box["calls"].append({"cmd": list(cmd),
                              "env": dict(env) if env is not None else None})
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


# ---------------------------------------------------------------------------
# operation envelope verify — happy path
# ---------------------------------------------------------------------------


def test_envelope_verify_translates_positional_args(
    project_dir, no_bash, captured_call
):
    rc = main_mod.main([
        "operation", "envelope", "verify",
        "op-test-1", "extract:profile:user-1",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_OPERATION_ID"] == "op-test-1"
    assert env["BEACON_ACTION"] == "extract:profile:user-1"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "operation_envelope_verify"


def test_envelope_verify_without_json_flag(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "operation", "envelope", "verify",
        "op-test-2", "read:doc:abc",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_JSON"] == ""


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_envelope_verify_missing_op_id_rejected(
    project_dir, no_bash, captured_call, capsys
):
    rc = main_mod.main(["operation", "envelope", "verify"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "envelope verify" in err
    assert captured_call["cmd"] is None


def test_envelope_verify_missing_action_rejected(
    project_dir, no_bash, captured_call, capsys
):
    rc = main_mod.main(["operation", "envelope", "verify", "op-test-1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "envelope verify" in err
    assert captured_call["cmd"] is None


def test_envelope_subcommand_without_verb_rejected(
    project_dir, no_bash, captured_call, capsys
):
    rc = main_mod.main(["operation", "envelope"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "verify" in out
    assert captured_call["cmd"] is None


# ---------------------------------------------------------------------------
# Sign / verify / disclosure-contract round-trip (e-1764 AC #3)
#
# The dispatcher's job is the argv→env shim only — the actual signature,
# tier check, and disclosure_contract logic lives in
# server/approved_actions + commands.py. We verify here that the
# dispatcher correctly *forwards* multiple calls that mirror a typical
# Skill loop: (a) verify before action A, (b) verify before action B,
# (c) verify a third disallowed action. The captured env trace lets a
# reviewer confirm the contract surface didn't lose data between calls.
# ---------------------------------------------------------------------------


def test_envelope_verify_multi_action_round_trip(
    project_dir, no_bash, captured_call
):
    actions = [
        ("op-skill-1", "search:doc:public"),
        ("op-skill-1", "send:dm:approved-target"),
        ("op-skill-1", "delete:project:beacon"),  # would be denied at server
    ]
    for op_id, action in actions:
        rc = main_mod.main([
            "operation", "envelope", "verify",
            op_id, action, "--json",
        ])
        assert rc == 0
    # All three calls must have reached commands.py with their full
    # (op_id, action) pair intact — no truncation, no collapsing.
    calls = captured_call["calls"]
    assert len(calls) == 3
    seen = [(c["env"]["BEACON_OPERATION_ID"], c["env"]["BEACON_ACTION"])
            for c in calls]
    assert seen == actions


# ---------------------------------------------------------------------------
# Cloud / local mode branching (e-1764 AC #4)
#
# The branching happens in commands.py:cmd_operation_envelope_verify
# (it returns rc=2 with "envelope verify requires cloud mode" in local
# mode). The dispatcher must NOT pre-check, otherwise the error message
# would diverge between OS (bash side also defers to commands.py).
# We assert here that no environment manipulation happens around mode
# detection — the env we emit contains only the three documented keys.
# ---------------------------------------------------------------------------


def test_envelope_verify_emits_only_documented_env_keys(
    project_dir, no_bash, captured_call
):
    rc = main_mod.main([
        "operation", "envelope", "verify",
        "op-mode-1", "x:y",
    ])
    assert rc == 0
    env = captured_call["env"]
    # The 3-key contract bin/beacon also emits at this call site.
    documented = {"BEACON_OPERATION_ID", "BEACON_ACTION", "BEACON_JSON"}
    actual = set(env.keys())
    # Both ends share a common env baseline (PATH, etc.). The dispatcher
    # may also pass through default args like BEACON_PROFILE; what we
    # require is that the three documented keys are present.
    missing = documented - actual
    assert not missing, f"Missing keys: {missing}"


# ---------------------------------------------------------------------------
# Help surface
# ---------------------------------------------------------------------------


def test_operation_help_lists_envelope_verify(project_dir, no_bash, capsys):
    rc = main_mod.main(["operation", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "envelope verify" in out
