"""ms-128 方針6 / e-4374 (AC7): leader_review verdict set branches on halt_reason.

A leader_review queue conflates two kinds: a normally-completed target (the
executor stamped it) and a halt-rescued target (the server force-transitioned
it with a halt_reason tag — it merely *stopped*, it is not done). The leader's
verdicts must differ: ``approve`` is meaningless for something that only
stalled. ``leader_review_verdict_set`` picks the right set; these pin it.
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


def _now():
    return datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)


def _doc_with(state, *, halt_reason=None):
    entry = {"state": state}
    if halt_reason is not None:
        entry["halt_reason"] = halt_reason
    return {"task_states": {"e-1": entry}}


def test_completion_verdicts_when_no_halt_reason():
    vs = trek.leader_review_verdict_set(_doc_with("leader_review"), "e-1")
    assert vs["mode"] == "completion"
    assert vs["halt_reason"] is None
    names = {v["verdict"] for v in vs["verdicts"]}
    assert names == {"approve", "re-work", "forward-to-user"}
    # approve terminalizes; re-work returns to working.
    to = {v["verdict"]: v["to_state"] for v in vs["verdicts"]}
    assert to["approve"] == "user_review"
    assert to["re-work"] == "working"
    assert to["forward-to-user"] == "user_review"


def test_halt_rescue_verdicts_when_halt_reason_present():
    vs = trek.leader_review_verdict_set(
        _doc_with("leader_review", halt_reason=trek.HALT_REASON_STALL), "e-1")
    assert vs["mode"] == "halt_rescue"
    assert vs["halt_reason"] == trek.HALT_REASON_STALL
    names = {v["verdict"] for v in vs["verdicts"]}
    assert names == {"reassign", "dm-nudge", "re-open"}
    # every halt-rescue verdict returns the target to working.
    assert all(v["to_state"] == "working" for v in vs["verdicts"])


def test_approve_never_offered_for_halt_rescue():
    """The structural guard: a stalled target can never be 'approved' as done."""
    vs = trek.leader_review_verdict_set(
        _doc_with("leader_review", halt_reason=trek.HALT_REASON_LIVENESS), "e-1")
    assert "approve" not in {v["verdict"] for v in vs["verdicts"]}


def test_rescue_verdicts_never_offered_for_completion():
    vs = trek.leader_review_verdict_set(_doc_with("leader_review"), "e-1")
    names = {v["verdict"] for v in vs["verdicts"]}
    assert not (names & {"reassign", "dm-nudge", "re-open"})


def test_not_in_review_when_state_is_working():
    vs = trek.leader_review_verdict_set(_doc_with("working"), "e-1")
    assert vs["mode"] == "not_in_review"
    assert vs["verdicts"] == []


def test_unknown_target_is_not_in_review():
    vs = trek.leader_review_verdict_set({"task_states": {}}, "e-nope")
    assert vs["mode"] == "not_in_review"


def test_forced_halt_then_verdict_set_is_halt_rescue():
    """End-to-end: sweep forces working→leader_review with the tag, and the
    verdict set then reads that tag → halt_rescue (not completion)."""
    doc = {"task_states": {"e-1": {"state": "working"}}}
    trek.force_halt_to_leader_review(
        doc, "e-1", halt_reason=trek.HALT_REASON_STALL, now=_now())
    assert doc["task_states"]["e-1"]["state"] == "leader_review"
    vs = trek.leader_review_verdict_set(doc, "e-1")
    assert vs["mode"] == "halt_rescue"


def test_re_open_clears_halt_reason_so_next_review_is_not_stuck():
    """A halt-rescue re-open (leader_review→working via set_task_state) replaces
    the entry, dropping the halt_reason tag. When the target later completes and
    re-enters leader_review, it is a normal completion, not a phantom halt."""
    doc = {"task_states": {"e-1": {"state": "working"}}}
    trek.force_halt_to_leader_review(
        doc, "e-1", halt_reason=trek.HALT_REASON_STALL, now=_now())
    # re-open → working
    trek.set_task_state(doc, task_id="e-1", state="working", note="re-open")
    assert doc["task_states"]["e-1"].get("halt_reason") in (None, "")
    # later completion → leader_review with no halt tag
    trek.set_task_state(doc, task_id="e-1", state="leader_review", note="done")
    vs = trek.leader_review_verdict_set(doc, "e-1")
    assert vs["mode"] == "completion"


# ---------------------------------------------------------------------------
# AX/maintainability review PR#545 — pin the skill to the lib so the verdict
# lists have a single source of truth (not a hand-copied prose mirror).
# ---------------------------------------------------------------------------

import pathlib  # noqa: E402

_SKILL = (pathlib.Path(__file__).resolve().parent.parent
          / "skills" / "beacon-trek-review.md")


def test_skill_calls_the_verdict_verb_not_hand_derivation():
    """The skill must fetch the verdict set from the single-source CLI verb,
    not re-derive the branch by hand (the drift risk both reviewers flagged)."""
    body = _SKILL.read_text(encoding="utf-8")
    assert "beacon trek review-verdicts" in body, (
        "beacon-trek-review.md must call `beacon trek review-verdicts` so the "
        "verdict lists come from lib.leader_review_verdict_set, not prose."
    )


def test_skill_mentions_only_real_lib_verdicts():
    """Every verdict token named in the skill's option prose must be a real
    lib verdict — a phantom/renamed verdict in the skill would misfire."""
    body = _SKILL.read_text(encoding="utf-8")
    lib_verdicts = {v["verdict"] for v in
                    trek.COMPLETION_VERDICTS + trek.HALT_RESCUE_VERDICTS}
    # Guard against near-miss spellings: the skill must not name a verdict the
    # lib doesn't have.
    for phantom in ("forward-user", "reopen", "re_open", "dm_nudge"):
        # these are near-miss spellings of real verdicts; none may appear.
        assert phantom not in body, (
            f"skill names '{phantom}' which is not a lib verdict "
            f"(real set: {sorted(lib_verdicts)})"
        )
    # And the canonical names must all be present somewhere in the option prose.
    for v in lib_verdicts:
        assert v in body, f"lib verdict '{v}' is not documented in the skill"
