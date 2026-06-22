"""Skill markdown structural tests for the leader stance integration (e-2166).

ms-92 / e-2166 wove the leader stance reminder into two places:

  * ``skills/beacon-trek.md`` — Step 1-d, the create subaction's
    read-and-acknowledge block shown immediately after the trek is
    created (= the moment the user becomes a leader). The Skill must
    reference CORE doc ``trek-leader-stance`` and surface the 4
    concrete leader actions so the user can't onboard as a leader
    without seeing them.
  * ``skills/beacon-trek-execute.md`` — Step 0, the leader stance
    reminder shown at the top of every execute invocation. Wraps the
    same 4 actions plus the CORE doc reference so each Skill
    invocation reads the stance once before any autonomous action.

The CORE doc itself (``trek-leader-stance``) is tested via beacon's
own doc indexing; we don't reach into the doc backend here.

These tests are deliberately string-based — the Skill body is prose
that AI interprets, so a structural change (= dropping the stance
section) is the regression we care about. Wording can drift inside
sections without breaking these tests.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TREK_SKILL = REPO / "skills" / "beacon-trek.md"
TREK_EXECUTE_SKILL = REPO / "skills" / "beacon-trek-execute.md"


# ---------------------------------------------------------------------------
# beacon-trek.md — Step 1-d leader stance read-and-acknowledge
# ---------------------------------------------------------------------------


def test_beacon_trek_skill_has_leader_stance_step():
    """Step 1-d must exist so trek-create surfaces the stance to the user."""
    body = TREK_SKILL.read_text(encoding="utf-8")
    assert "Step 1-d" in body or "### 1-d" in body, (
        "beacon-trek.md must have a Step 1-d (= leader stance reminder) "
        "right after trek-create lands. Removing it lets users onboard "
        "as a leader without seeing the role contract."
    )
    assert "e-2166" in body, "the section must self-reference its driving task"


def test_beacon_trek_skill_references_core_doc_trek_leader_stance():
    body = TREK_SKILL.read_text(encoding="utf-8")
    assert "trek-leader-stance" in body, (
        "Step 1-d must reference CORE doc trek-leader-stance so the user "
        "can drill down past the inline summary"
    )


def test_beacon_trek_skill_lists_concrete_leader_actions():
    """The 4 concrete actions from the CORE doc must be inline so a user
    reading just the Skill (= no doc drill-down) still grasps the role."""
    body = TREK_SKILL.read_text(encoding="utf-8")
    # Each of the 4 expected actions:
    assert "pulse" in body, "action #1 (state-observation via pulse query)"
    assert "stuck" in body or "12 分" in body, (
        "action #2 (12-min stuck → leader direct push)"
    )
    assert "leader_review" in body, (
        "action #3 (leader_review immediate 3-choice picker)"
    )
    assert "archive" in body, (
        "action #4 (Trek completion = leader-willed archive)"
    )


def test_beacon_trek_skill_states_leader_is_推進者_not_調停者():
    """The core stance metaphor (= 調停者ではなく推進者) must appear so
    the user reading the Skill can't mistake the role for arbitration."""
    body = TREK_SKILL.read_text(encoding="utf-8")
    assert "推進者" in body
    assert "調停者" in body


# ---------------------------------------------------------------------------
# beacon-trek-execute.md — Step 0 leader stance reminder
# ---------------------------------------------------------------------------


def test_beacon_trek_execute_skill_starts_with_leader_stance_step():
    """Step 0 must surface the stance before any autonomous action fires."""
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    # The reminder must come BEFORE Step 1 ("Trek の有効性確認")
    assert "## Step 0: leader 自覚 reminder" in body, (
        "beacon-trek-execute.md must open with the leader stance reminder "
        "as Step 0 (= forcing function on every Skill invocation)"
    )
    # The original Step 0 ("起動コンテキストの確定") was renamed to Step 0-context
    # so the reminder takes precedence in reading order.
    assert "Step 0-context" in body
    # Verify reading order: Step 0 reminder appears before Step 0-context.
    p_reminder = body.find("## Step 0: leader 自覚 reminder")
    p_context = body.find("## Step 0-context")
    p_step1 = body.find("## Step 1: Trek")
    assert p_reminder < p_context < p_step1, (
        "leader stance reminder must precede context resolution and Step 1"
    )


def test_beacon_trek_execute_skill_references_core_doc_trek_leader_stance():
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    assert "trek-leader-stance" in body


def test_beacon_trek_execute_skill_self_references_e_2166():
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    assert "e-2166" in body


def test_beacon_trek_execute_skill_lists_concrete_leader_actions():
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    assert "pulse" in body
    assert "leader_review" in body
    assert "archive" in body


def test_beacon_trek_execute_skill_disclaims_punitive_enforcement():
    """The CORE doc's 'no punitive enforcement' philosophy must be carried
    forward in the Skill so future edits don't bolt on N-minute strict
    DM enforcement that would violate the autonomous-loop framework
    (= CORE doc 5nfTSmCDVUzD4SLzIhI5 — TTL safety net is the real strict
    layer, narrative is the soft one).
    """
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    # The disclaimer wording can drift but the concepts must remain.
    assert ("構造的 enforcement" in body
            and ("punitive" in body or "philosophy 違反" in body)), (
        "the Skill must state that punitive structural enforcement is NOT "
        "the leader stance mechanism — the reminder is a forcing function "
        "for willingness, not a penalty engine"
    )


def test_beacon_trek_execute_skill_distinguishes_leader_from_executor():
    """Executor sessions should not be made to feel like leaders just
    because they invoke this Skill — the stance reminder must explicitly
    say 'if you are leader' rather than asserting the role on everyone.
    """
    body = TREK_EXECUTE_SKILL.read_text(encoding="utf-8")
    # Look for the explicit "if leader / else information only" path.
    assert "executor role" in body, (
        "the stance reminder must scope to leader sessions and degrade to "
        "information-only for executor sessions"
    )
