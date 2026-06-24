"""Structural pin tests for the GitHub owner / repo hallucination guard
(= ms-95 / e-2370).

Background (= 2026-06-24 dogfood):
  When the AI generates install prompts (= /beacon-init / /beacon-onboard
  Skill body, or the join.html setup prompt template), there is a risk
  that the AI will *hallucinate* the GitHub owner / repository name
  (= fabricate a plausible-looking owner string from cwd directory name,
  inviter email, or training-data familiarity) and embed a wrong
  ``git clone`` URL into the install instructions.  The user, looking at
  a long install prompt, may not notice the wrong owner and end up
  cloning a stranger's repo or hitting a 404.

Structural fix (= e-2370):
  Each install-prompt surface must contain explicit text instructing the
  AI to:
    1. derive GitHub owner / repo from ``gh repo view`` or
       ``git remote get-url origin`` (= structured source, never guess);
    2. fall back to an explicit placeholder
       (= ``<your-github-owner>`` / ``<your-repo-name>``) if both fail;
    3. NEVER infer the owner from cwd / inviter email / project name.

These tests fail loudly the moment any of the three surfaces drops the
guard — preventing a silent regression of the dogfood incident.

Why structural pin tests (= grep for keywords) instead of behavioural
end-to-end:
  The install-prompt surfaces are Claude-side prose (= Skill markdown
  bodies) and a JS template string (= ``buildSetupPrompt`` in
  join.html).  They are *consumed* by an AI at runtime; there is no
  Python entry-point to invoke.  Structural assertions are the only way
  to lock the guard in place without an interactive harness.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Surface paths
# ---------------------------------------------------------------------------

INIT_SKILL = REPO / "skills" / "beacon-init.md"
ONBOARD_SKILL = REPO / "skills" / "beacon-onboard.md"
JOIN_HTML = REPO / "server" / "static" / "join.html"


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_has_all(text: str, markers: list[str], where: str) -> None:
    """Assert every marker appears in text; report which are missing."""
    missing = [m for m in markers if m not in text]
    assert not missing, (
        f"{where}: missing required hallucination-guard markers: {missing!r}. "
        f"These markers are the structural pin for e-2370 (= prevent install "
        f"prompt from fabricating a GitHub owner). If you intentionally moved "
        f"the wording, update this test to track the new wording."
    )


# ---------------------------------------------------------------------------
# beacon-init Skill
# ---------------------------------------------------------------------------


def test_beacon_init_skill_has_owner_guard():
    """The /beacon-init Skill body must instruct the AI never to guess
    the GitHub owner, to derive it from gh CLI / git remote, and to fall
    back to a placeholder.
    """
    assert INIT_SKILL.exists(), f"{INIT_SKILL} must exist"
    text = INIT_SKILL.read_text(encoding="utf-8")
    _assert_has_all(
        text,
        [
            # 1. The dedicated section header (= AI's eye must land here)
            "GitHub owner / repo の取り扱い",
            # 2. Reference back to the originating incident
            "e-2370",
            # 3. Structured source = gh CLI / git remote
            "gh repo view",
            "git remote get-url origin",
            # 4. Explicit placeholder fallback
            "<your-github-owner>",
            "<your-repo-name>",
            # 5. Explicit ban on inferring from cwd / project name
            "推測してはならない",
        ],
        where=str(INIT_SKILL.relative_to(REPO)),
    )


# ---------------------------------------------------------------------------
# beacon-onboard Skill
# ---------------------------------------------------------------------------


def test_beacon_onboard_skill_has_owner_guard():
    """The /beacon-onboard Skill body must carry the same guard.
    Onboarding flows for invited members are a common surface where the
    AI is tempted to fabricate a clone URL (= guessing from the inviter
    email's domain, etc).
    """
    assert ONBOARD_SKILL.exists(), f"{ONBOARD_SKILL} must exist"
    text = ONBOARD_SKILL.read_text(encoding="utf-8")
    _assert_has_all(
        text,
        [
            "GitHub owner / repo の取り扱い",
            "e-2370",
            "gh repo view",
            "git remote get-url origin",
            "<your-github-owner>",
            "<your-repo-name>",
            "推測してはならない",
        ],
        where=str(ONBOARD_SKILL.relative_to(REPO)),
    )


# ---------------------------------------------------------------------------
# join.html buildSetupPrompt (= invitation install prompt)
# ---------------------------------------------------------------------------


def test_join_html_setup_prompt_has_owner_guard():
    """The join.html ``buildSetupPrompt`` template must embed the
    hallucination guard directly into the prompt text that gets pasted
    into Claude Code by an invited user.  This is the highest-risk
    surface because the prompt is consumed by an AI on a *new* machine
    where neither Skill body is present yet.
    """
    assert JOIN_HTML.exists(), f"{JOIN_HTML} must exist"
    text = JOIN_HTML.read_text(encoding="utf-8")

    # The function must still exist; we don't want a refactor to silently
    # rename/remove it.
    assert "function buildSetupPrompt" in text, (
        "server/static/join.html must still define buildSetupPrompt(). "
        "If it was renamed, update this test to track the new entry point."
    )

    _assert_has_all(
        text,
        [
            # Hallucination guard markers inside the prompt template
            "e-2370",
            "gh repo view",
            "git remote get-url origin",
            "<your-github-owner>",
            "<your-repo-name>",
            # ban-on-inference signal (= JS template uses 推測 in the wording)
            "推測しない",
        ],
        where=str(JOIN_HTML.relative_to(REPO)),
    )


# ---------------------------------------------------------------------------
# Sanity: no surface accidentally hard-codes a sample owner
# ---------------------------------------------------------------------------


def test_install_surfaces_do_not_hardcode_sample_owner():
    """Guard against a future contributor pasting an example owner
    (= e.g. ``acme-corp`` / ``foo-org``) directly into the install
    prompt as a 'helpful' default.  Only the *project-level* Beacon
    self-references (= ``r-kida2/beacon`` / ``kurogin23mech-source/beacon``)
    are allowed because they point to the Beacon project itself, not the
    user's repo.

    The placeholder ``<your-github-owner>`` is the only acceptable
    stand-in inside install prompts.
    """
    for path in (INIT_SKILL, ONBOARD_SKILL, JOIN_HTML):
        text = path.read_text(encoding="utf-8")
        # Quick negative sanity — these strings are not allowed to appear
        # as fabricated install-prompt owners.
        forbidden = [
            "acme-corp/",
            "foo-org/",
            "example-user/",
            "your-username/",  # English variant slipped through past PR
        ]
        for f in forbidden:
            assert f not in text, (
                f"{path.relative_to(REPO)} contains forbidden sample owner "
                f"{f!r}. Use the canonical placeholder <your-github-owner> "
                f"instead (= e-2370 structural rule)."
            )
