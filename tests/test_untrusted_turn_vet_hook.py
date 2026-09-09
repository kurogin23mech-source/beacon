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


def _vet(cwd: Path, session_id: str, tool_name: str, tool_input=None,
         event: str = "PostToolUse") -> None:
    _run(VET, cwd, {"hook_event_name": event, "session_id": session_id,
                    "tool_name": tool_name, "tool_input": tool_input or {}})


# ---------------------------------------------------------------------------
# The vet hook's fire condition (all three must hold: armed + side-effect + asked)
# ---------------------------------------------------------------------------

def test_vet_hook_clears_armed_asked_context(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    ut.record_asked(root, "sv-1", "Write")           # the gate asked (approved)
    _vet(root, "sv-1", "Write", {"file_path": "x"})  # armed + asked + side-effect
    assert ut.is_armed(root, "sv-1") is None          # context cleared → passes


def test_vet_hook_does_not_vet_without_gate_ask(tmp_path):
    # THE security guard (independent review consensus): a side-effect that ran
    # while armed but WITHOUT the gate emitting an ask (fail-safe / timeout / not
    # installed / auto-accept mode) is NOT proof of human approval. It must not
    # silently clear the context and drop the gate.
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])          # armed, but gate never asked
    _vet(root, "sv-1", "Write", {"file_path": "x"})
    assert ut.is_armed(root, "sv-1") is not None      # still armed → still gates


def test_vet_hook_ignores_read_only_completion(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    ut.record_asked(root, "sv-1", "Read")
    _vet(root, "sv-1", "Read", {"file_path": "x"})   # read-only → never clears
    assert ut.is_armed(root, "sv-1") is not None      # still armed


def test_vet_hook_does_not_vet_unarmed_side_effect(tmp_path):
    # A side-effect that runs while NOT armed is ordinary work; clearing/vetting
    # it would suppress the gate for a LATER real injection.
    root = _beacon_root(tmp_path)
    _vet(root, "sv-1", "Bash", {"command": "ls"})
    assert ut.is_armed(root, "sv-1") is None          # nothing to clear
    # A subsequent real DM still arms and the gate still fires.
    ut.arm(root, "sv-1", event_ids=["e-late"])
    assert _gate_decision(root, "sv-1", "Write", {"file_path": "y"}) == "ask"


def test_vet_hook_ignores_non_posttooluse_event(tmp_path):
    # A PreToolUse event delivered to the vet hook must NOT clear (event filter).
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    ut.record_asked(root, "sv-1", "Write")
    _vet(root, "sv-1", "Write", {"file_path": "x"}, event="PreToolUse")
    assert ut.is_armed(root, "sv-1") is not None       # wrong event → ignored


def test_vet_hook_is_per_session(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-A", event_ids=["e-1"])
    ut.record_asked(root, "sv-A", "Write")
    ut.arm(root, "sv-B", event_ids=["e-2"])
    _vet(root, "sv-A", "Write", {"file_path": "x"})
    assert ut.is_armed(root, "sv-A") is None          # A's context cleared
    assert ut.is_armed(root, "sv-B") is not None       # B untouched, still armed


# ---------------------------------------------------------------------------
# Full state machine — one approval per risky CONTEXT, not per tool, and a NEW
# untrusted DM re-arms (per-context, corrected from per-session in the #738
# review). Exercises the REAL flow: the gate stamps asked_at when it asks, so
# the subsequent vet finds the marker and clears that context.
# ---------------------------------------------------------------------------

def test_one_approval_clears_context_but_new_dm_rearms(tmp_path):
    root = _beacon_root(tmp_path)
    # DM arrives → armed. First side-effect → gate asks (the ONE confirmation).
    ut.arm(root, "sv-1", event_ids=["e-1"])
    assert _gate_decision(root, "sv-1", "Bash", {"command": "a"}) == "ask"
    assert ut.is_armed(root, "sv-1")["asked_at"]  # the gate stamped the marker
    # Human approves → the tool runs → PostToolUse clears THIS context.
    _vet(root, "sv-1", "Bash", {"command": "a"})
    # Every subsequent side-effect in the SAME context passes without asking.
    assert _gate_decision(root, "sv-1", "Bash", {"command": "b"}) == ""
    assert _gate_decision(root, "sv-1", "Write", {"file_path": "c"}) == ""
    # A brand-new untrusted DM RE-ARMS (per-context) → the gate asks again for it.
    assert ut.arm(root, "sv-1", event_ids=["e-2"]) == ut.ARM_OK
    assert _gate_decision(root, "sv-1", "Bash", {"command": "d"}) == "ask"
