"""Whole-target 思想 artifact collection (ms-119 / e-4196).

A target-close philosophy review had no artifact source: --pr / --diff-ref are
change-scoped, so a whole-MS close reviewed against nothing (照合対象が空). The
fix collects the target's OWN recorded commit ledger as the mechanically-gathered
"what this target changed" — implementer-INDEPENDENT (git history + project
ledger), so it keeps the same independence contract as a diff.

These tests pin:
  * the pure shaper (review_spine.whole_target_artifact) shape;
  * the collector (commands._collect_whole_target_artifact) walking recorded
    commits, dedup, per-commit error tolerance, byte-budget truncation, and the
    empty-ledger gap;
  * the CLI else-branch now advertising --target.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import review_spine  # noqa: E402


# --- pure shaper -----------------------------------------------------------

def test_whole_target_artifact_shape():
    a = review_spine.whole_target_artifact(
        [{"hash": "aaaaaaa", "subject": "feat A", "diff": "+a"}],
        target_id="ms-119",
    )
    assert a["kind"] == "whole-target"
    assert a["ref"] == "ms-119"
    assert a["truncated"] is False
    assert a["commits"][0]["diff"] == "+a"


def test_whole_target_artifact_truncated_flag():
    a = review_spine.whole_target_artifact([], target_id="ms-9", truncated=True)
    assert a["truncated"] is True
    assert a["commits"] == []


def test_assemble_accepts_whole_target_mode():
    # ms-119 / e-4196 self-review skill_gap: a whole-target artifact must not be
    # labelled mode="diff". The kernel accepts a dedicated "whole-target" mode so
    # the judge isn't told "diff" while handed a commit series.
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_PHILOSOPHY,
        origin_id="specDocId", origin_content="## 設計方針 ...",
        diff_text="", mode="whole-target", target_ref="ms-119 (whole-target)",
        artifact=review_spine.whole_target_artifact(
            [{"hash": "aaaaaaa", "subject": "s", "diff": "+a"}], target_id="ms-119"),
    )
    assert b["mode"] == "whole-target"
    assert b["artifact"]["kind"] == "whole-target"


# --- collector -------------------------------------------------------------

def _fake_project():
    """A milestone whose recorded ledger carries commits (some nested under a
    task), a duplicate 7-char hash, and one entry with no hash."""
    return {
        "milestones": [
            {
                "id": "ms-T",
                "status": "in_progress",
                "title": "T",
                "entries": [
                    {"id": "e-1", "type": "commit", "description": "feat A",
                     "meta": {"hash": "aaaaaaa1111"}},
                    {"id": "e-2", "type": "task", "description": "task",
                     "entries": [
                         {"id": "e-3", "type": "commit", "description": "fix B",
                          "meta": {"hash": "bbbbbbb2222"}},
                         # duplicate 7-char prefix of e-1 → deduped
                         {"id": "e-4", "type": "commit", "description": "dup A",
                          "meta": {"hash": "aaaaaaa9999"}},
                     ]},
                    # recorded but unfetchable → carried with an error, not aborted
                    {"id": "e-5", "type": "commit", "description": "gone C",
                     "meta": {"hash": "ccccccc3333"}},
                    # no hash → skipped
                    {"id": "e-6", "type": "commit", "description": "no hash",
                     "meta": {}},
                ],
            }
        ]
    }


def _install_fakes(monkeypatch, data, diffs, errors=()):
    monkeypatch.setattr(commands, "load_project", lambda: data)

    def fake_run(argv, **kw):
        # argv == ["git", "show", "--format=", "-U15", <hash>]
        h = argv[-1]
        if h in errors:
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="fatal: bad object")
        return types.SimpleNamespace(returncode=0, stdout=diffs.get(h, ""),
                                     stderr="")

    monkeypatch.setattr(commands.subprocess, "run", fake_run)


def test_collect_walks_dedups_and_tolerates_missing(monkeypatch):
    data = _fake_project()
    diffs = {"aaaaaaa1111": "+A", "bbbbbbb2222": "+B"}
    _install_fakes(monkeypatch, data, diffs, errors={"ccccccc3333"})

    artifact, gaps = commands._collect_whole_target_artifact("ms-T")

    assert artifact["kind"] == "whole-target"
    hashes = [c["hash"] for c in artifact["commits"]]
    # e-1 (aaaaaaa), e-3 (bbbbbbb), e-5 (ccccccc) — dup e-4 removed, e-6 no hash
    assert hashes == ["aaaaaaa1111", "bbbbbbb2222", "ccccccc3333"]
    by_hash = {c["hash"]: c for c in artifact["commits"]}
    assert by_hash["aaaaaaa1111"]["diff"] == "+A"
    assert by_hash["aaaaaaa1111"]["subject"] == "feat A"
    # the unfetchable commit is carried with an error, not silently dropped
    assert "error" in by_hash["ccccccc3333"]
    assert artifact["truncated"] is False
    assert gaps == []


def test_collect_empty_ledger_surfaces_gap(monkeypatch):
    data = {"milestones": [{"id": "ms-E", "status": "todo", "title": "E",
                            "entries": []}]}
    _install_fakes(monkeypatch, data, {})

    artifact, gaps = commands._collect_whole_target_artifact("ms-E")

    assert artifact["commits"] == []
    assert len(gaps) == 1
    assert "照合対象が空" in gaps[0]


def test_collect_truncates_on_budget(monkeypatch):
    data = _fake_project()
    # big diffs so the first commit alone exceeds a tiny budget
    diffs = {"aaaaaaa1111": "x" * 50, "bbbbbbb2222": "y" * 50}
    _install_fakes(monkeypatch, data, diffs, errors={"ccccccc3333"})
    monkeypatch.setattr(commands, "_WHOLE_TARGET_DIFF_BUDGET", 10)

    artifact, gaps = commands._collect_whole_target_artifact("ms-T")

    assert artifact["truncated"] is True
    # first commit got its diff (budget checked before, spent after); the rest are
    # subject-only omitted
    first = artifact["commits"][0]
    assert first["diff"] == "x" * 50
    later = artifact["commits"][1]
    assert later.get("omitted") is True
    assert later["diff"] == ""
    assert any("打ち切り" in g for g in gaps)


# --- CLI else-branch advertises --target -----------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = os.path.join(REPO, "lib", "commands.py")


def test_cli_missing_artifact_source_names_target(monkeypatch):
    import subprocess
    env = dict(os.environ)
    # philosophy needs an origin doc; give a bogus one so origin resolution fails
    # BEFORE artifact collection would run — we only assert the else-branch text is
    # reachable, so instead drive a type whose origin is repo-file (ax) with no
    # --pr/--diff-ref/--target to hit the artifact else-branch cleanly.
    env.update({"BEACON_REVIEW_TYPE": "ax"})
    for k in ("BEACON_PR", "BEACON_DIFF_REF", "BEACON_TARGET_ID"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, COMMANDS, "review_context"],
                       capture_output=True, text=True, env=env, cwd=REPO)
    assert p.returncode == 1
    assert "--target" in p.stderr
    assert p.stdout.strip() == ""
