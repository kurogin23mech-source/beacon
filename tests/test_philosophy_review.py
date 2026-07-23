"""Regression anchor for the 思想 (philosophy) review instance (ms-119 e-3913).

The philosophy review reuses the AX review-kernel (原典 + cold judge + 構造化
findings) with the 原典 swapped to the target's SPEC / vision. These checks pin:
  - the skill exists with the cold-judge harness + drift-findings schema, and
  - the firing spine (e-3911) actually names this skill when it fires the
    philosophy binding — so the trigger and the skill cannot drift apart.
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
SKILL = os.path.join(HERE, "..", "skills", "philosophy-review", "SKILL.md")

sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import commands  # noqa: E402


def _skill_text():
    with open(SKILL, "r", encoding="utf-8") as f:
        return f.read()


def test_skill_exists_with_frontmatter():
    t = _skill_text()
    assert "name: philosophy-review" in t


def test_skill_declares_spec_as_origin_not_implementation():
    # The defining move of the philosophy instance: 原典 = SPEC / vision, and the
    # judge must not accept the implementation's self-justification.
    t = _skill_text()
    assert "原典" in t and "SPEC" in t
    assert "自己弁護" in t or "意図説明" in t  # cold-judge posture


def test_skill_declares_drift_findings_schema():
    # The reused review-kernel's structured output — drift as promised/implemented
    # pairs, not bare assertions.
    t = _skill_text()
    for key in ("spec_ref", "promised", "implemented", "drift", "severity",
                "skill_gaps"):
        assert key in t, f"missing schema key: {key}"


def test_skill_is_advisory_not_verdict():
    # Philosophy drift is discoverable → AI-complete advisory; it must NOT
    # confirm the 目的達成 verdict (that is owned / human, e-3912).
    t = _skill_text()
    assert "非 blocking" in t or "助言" in t


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": [{"id": "ms-5", "title": "T", "status": "in_progress",
                        "entries": []}],
        "operations": [],
    }, ensure_ascii=False))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(commands, "_resolve_session_id", lambda: "sv-t")
    # SPEC-bearing target → the philosophy binding fires.
    monkeypatch.setattr(commands, "_spec_exists_for_ms", lambda _id: True)
    return tmp_path


def test_firing_spine_names_the_skill(proj, monkeypatch):
    # A SPEC-bearing completion via the gate fires a philosophy review-due
    # trigger whose message points at /philosophy-review.
    for k in ("BEACON_MS_ID", "BEACON_REASON", "BEACON_REVIEW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了主張")
    monkeypatch.setenv("BEACON_REVIEW", "1")
    commands.cmd_milestone_done()
    trig = json.loads(
        (proj / ".beacon" / "triggers" / "review-due-ms-5.json").read_text())
    assert "philosophy" in trig["bindings"]
    assert "/philosophy-review" in trig["message"]
