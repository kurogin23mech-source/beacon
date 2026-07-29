"""ms-128 Wave7 — Trek Skill 動線の structural (string-based) forcing functions.

Two acceptance criteria are pinned against the Skill markdown bodies. The
Skill body is prose the AI interprets, so what we guard is the structural
presence of the new動線 — wording can drift inside a section without
breaking these, but removing the動線 itself is the regression we catch.

  * AC9 (e-4281) — ``beacon-trek-execute.md``: the executor must not throw a
    decision at the terminal human (AskUserQuestion / modal prompt). Judgment
    routes to leader/peer DM; deploy/release escalates non-blocking.
  * AC6 (e-4370) — ``beacon-trek-review.md``: leader stamps ``user_review``
    (not ``done``) only after imposing 思想 + 目的達成 review at the
    leader_review→user_review terminalization boundary, and the completion
    handoff is fired by the leader alone.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"
TREK_EXECUTE = SKILLS / "beacon-trek-execute.md"
TREK_REVIEW = SKILLS / "beacon-trek-review.md"
TREK_PULSE = SKILLS / "beacon-trek-pulse.md"


# ---------------------------------------------------------------------------
# AC9 (e-4281) — executor は端末の人間に判断を投げない
# ---------------------------------------------------------------------------


def test_execute_skill_forbids_askuserquestion():
    """The execute Skill must explicitly forbid AskUserQuestion within Trek
    scope — the terminal-human modal is what killed the session for 4h+ in
    the 2026-07-27 dogfood (tk-9d4b53ed)."""
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    assert "AskUserQuestion" in body, (
        "beacon-trek-execute.md must name AskUserQuestion so the prohibition "
        "is unambiguous (e-4281 / AC9)."
    )
    assert "e-4281" in body


def test_execute_skill_has_judgment_priority_section():
    """The pulse Skill cross-references '判断境界の優先順' in the execute
    Skill body; that section must exist so the reference resolves and the
    routing order (self / dm-peer / dm-leader / continue) is documented."""
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    assert "判断境界の優先順" in body
    assert "dm-peer" in body and "dm-leader" in body


def test_pulse_skill_reference_to_execute_section_resolves():
    """Guard the dangling-reference regression: the pulse Skill points readers
    at the execute Skill's 判断境界 section — both must carry the anchor."""
    pulse = TREK_PULSE.read_text(encoding="utf-8")
    execute = TREK_EXECUTE.read_text(encoding="utf-8")
    if "判断境界の優先順" in pulse:
        assert "判断境界の優先順" in execute, (
            "pulse Skill references the execute Skill's 判断境界の優先順 "
            "section, but it is missing from beacon-trek-execute.md."
        )


def test_execute_skill_terminal_choice_uses_5state_tokens():
    """Regression guard: the Step 9 forced-choice state-update must use the
    live 5-state tokens, not the retired ``done``/``waiting-review`` pair
    (``set_task_state`` rejects/migrates those)."""
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    assert "done|waiting-review" not in body, (
        "Step 9 still shows the stale 'done|waiting-review' terminal tokens; "
        "executor terminalizes to leader_review|user_review (done is Trek外)."
    )


# ---------------------------------------------------------------------------
# AC6 (e-4370) — 完遂 handoff + 思想/目的達成 review at terminalization boundary
# ---------------------------------------------------------------------------


def test_review_skill_approve_stamps_user_review_not_done():
    """Option A (approve) must stamp ``user_review`` — done is Trek外 (方針5).
    The whole terminalization boundary lives on leader_review→user_review."""
    body = TREK_REVIEW.read_text(encoding="utf-8")
    assert "task-state <trek_id> <task_id> user_review" in body, (
        "Option A must stamp user_review; done must not be written by Trek."
    )
    assert "task-state <trek_id> <task_id> done" not in body, (
        "Trek must not stamp done (方針5 / e-4366); read-time migrate only."
    )


def test_review_skill_imposes_philosophy_and_attainment_review():
    """leader_review→user_review must impose 思想 + 目的達成 review before the
    stamp (AX/保守性 are the executor's PR-time job; this is the配置手前境界)."""
    body = TREK_REVIEW.read_text(encoding="utf-8")
    assert "--type philosophy" in body, "思想レビュー動線が欠落 (e-4370)"
    assert "--type attainment" in body, "目的達成レビュー動線が欠落 (e-4370)"
    assert "ターミナル化境界" in body


def test_review_skill_completion_handoff_is_leader_sole():
    """The completion handoff (全 Target user_review → user 通知) is the
    leader's alone, and the executor must not judge completion — this is the
    race guard from 方針9."""
    body = TREK_REVIEW.read_text(encoding="utf-8")
    assert "summary-sent" in body, (
        "completion handoff must stamp summary-sent so leader-digest stops."
    )
    assert "リーダー単独" in body
    assert "executor は完遂を判定しない" in body


def test_review_skill_state_set_has_no_done():
    """The state-vocabulary section must reflect the 5-state set without done
    (done is Trek外, migrated at read time)."""
    body = TREK_REVIEW.read_text(encoding="utf-8")
    assert "block / todo / working / leader_review / user_review" in body
