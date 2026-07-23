"""ms-119 finding fixes — approval audit trail (e-4006 ①b) + model-independence
wire-up (e-3988 ②).

The 思想 review found the value of this MS is "an AI that approves cannot HIDE
it", but the approval record didn't capture which signal opened the gate — a
self-approval looked identical to a human's. And judge_model_independence() had
no caller (enforcement was skill prose). These tests pin both fixes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import review_spine  # noqa: E402
import transition_approval as ta  # noqa: E402


class _Env:
    def __init__(self, env):
        self._w = env
        self._p = {}

    def __enter__(self):
        for k, v in self._w.items():
            self._p[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._p.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- e-4006 ①b: append_verdict records the gate ---

def _pending_entry():
    return {
        "id": "e-1", "type": "target-transition-approval", "status": "pending",
        "meta": {"target_id": "ms-1", "old_state": "in_progress", "new_state": "done",
                 "new_state": "done", "approval_status": "pending",
                 "approval_rationale": None, "approval_history": []},
    }


def test_append_verdict_records_gate():
    e = _pending_entry()
    gate = {"signal": "user-override", "session_kind": "unset", "evidence_source": ""}
    ta.append_verdict(e, status="approved", rationale="ok", actor="m/a",
                      at="2026-07-23T00:00:00", gate=gate)
    assert e["meta"]["approval_gate"] == gate
    assert e["meta"]["approval_history"][-1]["gate"] == gate


def test_append_verdict_without_gate_is_backward_compatible():
    e = _pending_entry()
    ta.append_verdict(e, status="approved", rationale="", actor="", at="t")
    # no gate key leaks when none supplied (old callers unaffected)
    assert "approval_gate" not in e["meta"]
    assert "gate" not in e["meta"]["approval_history"][-1]


# --- e-4006 ①b: _approval_gate_record reads the env signal ---

def test_gate_record_flags_ai_session_override():
    with _Env({"BEACON_TARGET_APPROVE_USER_OVERRIDE": "1",
               "BEACON_SESSION_KIND": None,
               "BEACON_EVIDENCE_SOURCE": None}):
        g = commands._approval_gate_record()
    assert g["signal"] == "user-override"
    assert g["session_kind"] == "unset"  # the smoking gun: override from a non-human session


def test_gate_record_human_session():
    with _Env({"BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
               "BEACON_SESSION_KIND": "human",
               "BEACON_EVIDENCE_SOURCE": "independent-judge:fable"}):
        g = commands._approval_gate_record()
    assert g["signal"] == "human-session"
    assert g["evidence_source"] == "independent-judge:fable"


# --- e-3988 ②: model-independence check runs in the kernel ---

def test_model_independence_gap_when_same_model():
    with _Env({"BEACON_JUDGE_MODEL": "opus", "BEACON_IMPLEMENTER_MODEL": "opus"}):
        g = commands._model_independence_gap(review_spine)
    assert g is not None and "モデル独立性" in g


def test_no_gap_when_models_differ():
    with _Env({"BEACON_JUDGE_MODEL": "fable", "BEACON_IMPLEMENTER_MODEL": "opus"}):
        assert commands._model_independence_gap(review_spine) is None


def test_no_gap_when_judge_model_absent():
    with _Env({"BEACON_JUDGE_MODEL": None, "BEACON_IMPLEMENTER_MODEL": "opus"}):
        assert commands._model_independence_gap(review_spine) is None
