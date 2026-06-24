"""Structural pin tests for /beacon-review-apply Skill (ms-61 e-1892).

Why this file exists:

  The Skill at ``skills/beacon-review-apply.md`` is Claude-side prose
  that walks the user through MS 再分割 (= splitting a meta-review MS
  into multiple new MSes and moving tasks). Until ms-61 e-1892, the
  Skill spawned new MSes but never prompted to activate one of them —
  the parent MS / new MSes all stayed in ``todo`` status. The 2026-06-17
  ms-71 incident (= 6 new MSes ms-75〜80 land した直後、 全部 todo の
  まま 棚卸し残務 6 件が放置された) was the structural pathology.

  These tests pin the Step 4.5 (= 新規 MS の active 化確認) language so
  a future edit can't silently regress: dropping the prompt, dropping
  the ``beacon milestone start`` call, or skipping the warning when the
  user declines are all caught here.

  We do NOT exercise the Skill end-to-end via subprocess — the Skill
  body is prompt-style prose for Claude + a human, and there is no
  importable helper. Markdown structure assertions are the right shape
  of regression coverage for this layer.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO / "skills" / "beacon-review-apply.md"


# ---------------------------------------------------------------------------
# Baseline: file + frontmatter
# ---------------------------------------------------------------------------


def test_skill_file_exists():
    assert SKILL_PATH.exists(), (
        "skills/beacon-review-apply.md must exist — the Skill is invoked "
        "via the markdown filename, not via a Python entry-point."
    )


def test_skill_frontmatter_well_formed():
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "frontmatter must start at line 1"
    end = body.find("\n---\n", 4)
    assert end > 0, "frontmatter must close with --- on its own line"
    fm = body[4:end]
    assert "name: beacon-review-apply" in fm
    assert "description:" in fm
    # The trigger list MUST include the slash command itself, otherwise
    # `/beacon-review-apply` doesn't route to this Skill from Claude's
    # harness.
    assert "/beacon-review-apply" in fm


# ---------------------------------------------------------------------------
# ms-61 e-1892: Step 4.5 (= 新規 MS の active 化確認) structural pins
# ---------------------------------------------------------------------------


def test_skill_has_step_4_5_activation_section():
    """The whole point of e-1892 is the Step 4.5 prompt step.

    If a future edit drops this header, the manual ``beacon milestone
    start`` forgetting failure mode (ms-71 / 2026-06-17 incident) comes
    back. Locking the header text means the regression is loud.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "## Step 4.5:" in body, (
        "Step 4.5 (= 新規 MS の active 化確認) must be its own H2 step — "
        "embedding the prompt inside Step 4 or Step 5 hides it from "
        "future Skill readers"
    )
    # Self-reference to the driving task so anyone removing it knows
    # why it's there.
    assert "e-1892" in body, (
        "Step 4.5 must reference e-1892 — the task that introduced the "
        "forcing function. Drop the reference and the next refactorer "
        "won't know why this step exists."
    )


def test_skill_prompts_to_activate_parent_ms():
    """AC #1 of e-1892: the Skill MUST prompt the user before moving on.

    The exact prompt text is intentionally pinned in Japanese because
    the Skill is invoked in JP-context. If the prompt is removed or
    becomes a silent auto-action, this test fires.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Question to the user (= 「active にしますか」)
    assert "active 化しますか" in body or "active にしますか" in body, (
        "Step 4.5 must explicitly ask the user whether to activate an MS "
        "right after spawning new MSes — silent auto-start is too "
        "destructive (worktree + assignee + occupation claim)."
    )


def test_skill_invokes_beacon_milestone_start():
    """AC #1 of e-1892: the activation MUST go through ``beacon milestone start``.

    ms-81 e-1920 established ``milestone start`` as the canonical entry
    point that atomically flips status, self-adds assignee, creates the
    worktree, and lifts the occupation claim. Bypassing it (e.g. via
    ``beacon milestone update <id> --status in_progress``) skips the
    workspace + occupation work and resurrects the silent-branch-share
    bugs ms-65 / ms-81 closed.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "beacon milestone start" in body, (
        "Step 4.5 must call `beacon milestone start <ms-id>` — that is "
        "the ms-81 canonical activation path. Status-only flips skip "
        "the workspace/assignee/occupation work and reintroduce the "
        "ms-65 silent branch-share pathology."
    )


def test_skill_documents_user_decline_path():
    """If the user declines activation, the Skill MUST warn loudly.

    Without a visible warning, declining looks like a silent no-op and
    the user may forget that all new MSes are still ``todo`` (= the
    exact ms-71 failure mode). Pin the warning copy so a future edit
    can't quietly drop it.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    # The decline branch is documented (y/n with default y)
    assert "default y" in body, (
        "Step 4.5 must document the default answer so the user knows "
        "Enter activates the recommended MS"
    )
    # When the user declines, the warning fires
    assert "todo のまま" in body, (
        "Step 4.5 decline branch must explicitly warn that all new MSes "
        "stay in todo status — the original ms-71 incident was caused "
        "by a silent todo-residue state"
    )


def test_skill_references_ms71_incident_context():
    """The motivation MUST stay legible.

    e-1892 was driven by a concrete incident (ms-71 / 2026-06-17 / 6 new
    MSes ms-75〜80 left in todo). If the next refactorer can't see the
    incident reference, they won't understand why the step exists and
    will be tempted to remove it as "noise".
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "ms-71" in body or "2026-06-17" in body, (
        "Step 4.5 must reference the ms-71 / 2026-06-17 incident that "
        "drove this forcing function — without it the next reader can't "
        "tell why the prompt is necessary"
    )


def test_skill_mentions_supplementary_trigger_path():
    """The session-start side `meta-review-parent-stale` trigger is the
    complementary detection path (= AC #2 of e-1892).

    Even if the trigger ships in a follow-up PR, the Skill should name
    the route so we don't lose the design intent across handoffs.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "meta-review-parent-stale" in body, (
        "Step 4.5 must name the complementary `meta-review-parent-stale` "
        "trigger so the AC #2 (= session-start side warn route) "
        "intent survives even if the trigger ships in a separate PR"
    )


def test_skill_preserves_existing_observe_step():
    """The original Step 5 (= レビューMS を observe 化) must remain.

    e-1892 inserts Step 4.5 between Step 4 and Step 5. If a refactor
    accidentally drops Step 5 (= ``beacon milestone observe``), the
    review MS would stay in_progress forever — orthogonal regression
    that this test guards against.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "## Step 5: レビューMS を observe 化" in body, (
        "Step 5 (= レビューMS を observe 化) must remain unchanged — "
        "e-1892 inserts Step 4.5 BEFORE Step 5, not in place of it"
    )
    assert "beacon milestone observe" in body, (
        "Step 5 must still call `beacon milestone observe` to retire "
        "the meta-review MS as a methodology reference"
    )
