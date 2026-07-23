"""ms-119 / e-4009 — review types are a data-driven registry, not a whitelist.

The independent 目的達成 review flagged AC5 as *partial*: the review kernel was a
plug-in in name only — ``assemble_review_context`` / ``cmd_review_context`` hard-
coded ``ax`` / ``philosophy`` and raised on anything else, so a new judge-run
review type needed a code edit. This pins the fix: types are discovered from
``skills/<type>/review-type.json`` descriptors, so a NEW type is added by dropping
a descriptor (+ 原典 + SKILL) with no edit to review_spine.py / commands.py.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402


# --- the shipped types come from on-disk descriptors, not code constants ---

def test_builtin_types_discovered_from_disk_descriptors():
    reg = review_spine.load_review_types()
    assert "ax" in reg
    assert "philosophy" in reg
    # each carries an origin descriptor that says how its 原典 is resolved
    assert reg["ax"]["origin"]["kind"] == "repo-file"
    assert reg["ax"]["origin"]["ref"] == "skills/ax-review/principles.md"
    assert reg["philosophy"]["origin"]["kind"] == "doc"


def test_ax_and_philosophy_descriptor_files_exist():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in ("ax-review", "philosophy-review"):
        p = os.path.join(repo, "skills", d, "review-type.json")
        assert os.path.isfile(p), f"{p} must exist (types are data, not code)"
        desc = json.load(open(p, encoding="utf-8"))
        assert desc["judge_run"] is True


def test_judge_run_filter_excludes_human_gated():
    jr = review_spine.judge_run_review_types()
    assert set(jr) >= {"ax", "philosophy"}
    # attainment is human-gated (never a subagent judge), so it must not appear
    assert review_spine.REVIEW_ATTAINMENT not in jr


# --- a NEW type registers from a descriptor with no code change ---

def test_new_type_registers_from_descriptor_no_code_edit(tmp_path):
    skills = tmp_path / "skills"
    (skills / "security-review").mkdir(parents=True)
    (skills / "security-review" / "review-type.json").write_text(json.dumps({
        "id": "security",
        "judge_run": True,
        "label": "Security drift",
        "origin": {"kind": "repo-file", "ref": "skills/security-review/principles.md"},
    }), encoding="utf-8")
    reg = review_spine.load_review_types(skills_dir=str(skills))
    assert "security" in reg
    assert reg["security"]["origin"]["kind"] == "repo-file"
    # and it is a judge-run type
    assert "security" in review_spine.judge_run_review_types(skills_dir=str(skills))


def test_malformed_descriptor_is_skipped_not_crashing(tmp_path):
    skills = tmp_path / "skills"
    (skills / "broken").mkdir(parents=True)
    (skills / "broken" / "review-type.json").write_text("{not json", encoding="utf-8")
    (skills / "noid").mkdir(parents=True)
    (skills / "noid" / "review-type.json").write_text("{}", encoding="utf-8")
    reg = review_spine.load_review_types(skills_dir=str(skills))
    # built-in fallback still present, bad files silently skipped
    assert "ax" in reg and "philosophy" in reg
    assert "broken" not in reg and "" not in reg


# --- assemble_review_context is no longer a hardcoded pair ---

def test_assemble_accepts_any_registered_judge_type():
    """A synthetic type passed via known_judge_types is accepted — proving the
    old ``review_type not in (AX, PHILOSOPHY)`` whitelist is gone."""
    b = review_spine.assemble_review_context(
        "security",
        origin_id="skills/security-review/principles.md",
        origin_content="rule 1 ...",
        diff_text="+x",
        mode="diff",
        target_ref="PR #1",
        known_judge_types={"security", "ax", "philosophy"},
    )
    assert b["review_type"] == "security"


def test_assemble_rejects_type_absent_from_known_set():
    with pytest.raises(ValueError) as e:
        review_spine.assemble_review_context(
            "security",
            origin_id="x", origin_content="y", diff_text="z",
            mode="diff", target_ref="ms-1",
            known_judge_types={"ax", "philosophy"},
        )
    # the message points at the descriptor path, not a code edit
    assert "review-type.json" in str(e.value)
