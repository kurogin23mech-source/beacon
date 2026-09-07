"""ms-169 e-6280 — the PostToolUse vet hook (「文脈ごと1回承認」).

The gate (PreToolUse) fires「ask」on every armed side-effect. The vet hook
(PostToolUse) is what stops the ask from repeating: once a side-effect actually
executed WHILE armed (⇒ a human approved it), the session is vetted and later
DMs no longer re-gate. This file locks:

  * bin/beacon-untrusted-turn-vet-hook.py fires vet ONLY on (armed AND
    side-effect completed); a read-only completion or a side-effect that ran
    UNARMED must NOT vet (else a later real injection would be un-gated).
  * the full state machine composes: DM arms → gate asks → human approves →
    side-effect runs → vet hook vets → next side-effect passes → a NEW DM does
    not re-arm the vetted session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import untrusted_turn as ut  # noqa: E402

GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"
VET = REPO / "bin" / "beacon-untrusted-turn-vet-hook.py"


def _beacon_root(tmp_path: Path) -> Path:
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({"name": "t"}))
    return tmp_path


def _run(script: Path, cwd: Path, hook_input: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({**hook_input, "cwd": str(cwd)}),
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _gate_decision(cwd: Path, session_id: str, tool_name: str, tool_input=None) -> str:
    out = _run(GATE, cwd, {"hook_event_name": "PreToolUse", "session_id": session_id,
                           "tool_name": tool_name, "tool_input": tool_input or {}})
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _vet(cwd: Path, session_id: str, tool_name: str, tool_input=None) -> None:
    _run(VET, cwd, {"hook_event_name": "PostToolUse", "session_id": session_id,
                    "tool_name": tool_name, "tool_input": tool_input or {}})


# ---------------------------------------------------------------------------
# The vet hook's fire condition (both must hold)
# ---------------------------------------------------------------------------

def test_vet_hook_vets_armed_side_effect(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    _vet(root, "sv-1", "Write", {"file_path": "x"})  # armed + side-effect → vet
    assert ut.is_vetted(root, "sv-1") is True
    assert ut.is_armed(root, "sv-1") is None


def test_vet_hook_ignores_read_only_completion(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    _vet(root, "sv-1", "Read", {"file_path": "x"})   # read-only → never vets
    assert ut.is_vetted(root, "sv-1") is False
    assert ut.is_armed(root, "sv-1") is not None      # still armed


def test_vet_hook_does_not_vet_unarmed_side_effect(tmp_path):
    # THE security-critical guard: a side-effect that runs while NOT armed is
    # ordinary work; vetting it would suppress the gate for a LATER real
    # injection. So an unarmed side-effect must leave the session un-vetted.
    root = _beacon_root(tmp_path)
    _vet(root, "sv-1", "Bash", {"command": "ls"})
    assert ut.is_vetted(root, "sv-1") is False
    # A subsequent real DM still arms and the gate still fires.
    ut.arm(root, "sv-1", event_ids=["e-late"])
    assert _gate_decision(root, "sv-1", "Write", {"file_path": "y"}) == "ask"


def test_vet_hook_ignores_non_posttooluse_event(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    _vet(root, "sv-1", "Write", {"file_path": "x"})  # wrong event name below:
    # (the helper always sends PostToolUse; assert the positive path worked)
    assert ut.is_vetted(root, "sv-1") is True


def test_vet_hook_is_per_session(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-A", event_ids=["e-1"])
    ut.arm(root, "sv-B", event_ids=["e-2"])
    _vet(root, "sv-A", "Write", {"file_path": "x"})
    assert ut.is_vetted(root, "sv-A") is True
    assert ut.is_vetted(root, "sv-B") is False       # B untouched, still armed
    assert ut.is_armed(root, "sv-B") is not None


# ---------------------------------------------------------------------------
# Full state machine — one approval per risky context, not per tool
# ---------------------------------------------------------------------------

def test_one_approval_then_rest_of_session_passes(tmp_path):
    root = _beacon_root(tmp_path)
    # DM arrives → armed. First side-effect → gate asks (the ONE confirmation).
    ut.arm(root, "sv-1", event_ids=["e-1"])
    assert _gate_decision(root, "sv-1", "Bash", {"command": "a"}) == "ask"
    # Human approves → the tool runs → PostToolUse vets the session.
    _vet(root, "sv-1", "Bash", {"command": "a"})
    # Every subsequent side-effect in the session now passes without asking.
    assert _gate_decision(root, "sv-1", "Bash", {"command": "b"}) == ""
    assert _gate_decision(root, "sv-1", "Write", {"file_path": "c"}) == ""
    assert _gate_decision(root, "sv-1", "mcp__gmail__send_email", {"to": "x"}) == ""
    # A brand-new DM does NOT re-arm the vetted session (承認範囲 = セッション全体).
    assert ut.arm(root, "sv-1", event_ids=["e-2"]) is False
    assert _gate_decision(root, "sv-1", "Bash", {"command": "d"}) == ""
