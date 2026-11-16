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
    # the folded duplicate (e-4, same 7-char prefix) is recorded, not silently lost
    assert by_hash["aaaaaaa1111"]["deduped"] == ["aaaaaaa9999"]
    # the unfetchable commit is carried with an error, not silently dropped
    assert "error" in by_hash["ccccccc3333"]
    assert artifact["truncated"] is False
    # a fetch failure MUST surface as a gap so "gaps empty = complete input" holds
    assert len(gaps) == 1 and "取得できません" in gaps[0]


def test_collect_unknown_target_raises_not_exits(monkeypatch):
    # ms-119 / e-4196 maint review: the collector is reusable, so a bad target id
    # raises ValueError (the CLI layer converts it to an exit) — it never sys.exits.
    data = {"milestones": []}
    _install_fakes(monkeypatch, data, {})
    with pytest.raises(ValueError):
        commands._collect_whole_target_artifact("ms-nope")


def test_collect_merge_and_empty_diff_are_marked(monkeypatch):
    # ms-119 / e-4196 AX review: a successful-but-empty diff must be tagged so a
    # judge doesn't read "" as "changed nothing" (the merge-commit / empty-commit
    # completeness hole). The collector uses -m --first-parent so merges DO produce a
    # diff; a genuinely empty result carries empty_reason.
    data = {"milestones": [{"id": "ms-M", "status": "in_progress", "title": "M",
             "entries": [
                 {"id": "e-1", "type": "commit", "description": "empty one",
                  "meta": {"hash": "deadbee0000"}},
             ]}]}
    _install_fakes(monkeypatch, data, {"deadbee0000": ""})  # git show returns empty
    artifact, gaps = commands._collect_whole_target_artifact("ms-M")
    c = artifact["commits"][0]
    assert c["diff"] == ""
    assert "empty_reason" in c


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


def _run_ctx(env_extra):
    import subprocess
    env = dict(os.environ)
    for k in ("BEACON_PR", "BEACON_DIFF_REF", "BEACON_TARGET_ID", "BEACON_MODE",
              "BEACON_ORIGIN_DOC"):
        env.pop(k, None)
    env.update(env_extra)
    return subprocess.run([sys.executable, COMMANDS, "review_context"],
                          capture_output=True, text=True, env=env, cwd=REPO)


def test_cli_missing_artifact_source_names_target():
    # ax's 原典 is a repo-file, so with no --pr/--diff-ref/--target it reaches the
    # artifact else-branch cleanly; assert that branch's error advertises --target.
    p = _run_ctx({"BEACON_REVIEW_TYPE": "ax"})
    assert p.returncode == 1
    assert "--target" in p.stderr
    assert p.stdout.strip() == ""


def test_cli_target_with_pr_is_rejected():
    # ms-119 / e-4196 AX review: --target (whole-target) + --pr (change-scoped) must
    # not silently let --pr win and drop the whole-target scope.
    p = _run_ctx({"BEACON_REVIEW_TYPE": "philosophy", "BEACON_TARGET_ID": "ms-1",
                  "BEACON_PR": "1"})
    assert p.returncode == 1
    assert "同時指定できません" in p.stderr
    assert p.stdout.strip() == ""


def test_cli_pr_open_type_rejects_whole_target():
    # ms-119 / e-4196 AX review: a pr-open review type (ax) has no whole-target
    # question in its instrument, so --target is routed away, not handed a mode the
    # judge instrument doesn't know.
    p = _run_ctx({"BEACON_REVIEW_TYPE": "ax", "BEACON_TARGET_ID": "ms-1"})
    assert p.returncode == 1
    assert "philosophy" in p.stderr
    assert p.stdout.strip() == ""


# --- 作業記録を judge に運ばないこと (ms-119 e-7352 の 6 番目の経路) ----------
#
# 変更スコープの収集 (_collect_change_diff) は .beacon/ を落とすが、target 全体の
# 収集はこの口を通らない (1 commit ずつ `git show`、rc≠0 を許容するのが仕様)。
# 通らないなら同じ落とし方をここでも行う必要がある。でないと「target 全体の
# 思想レビュー」だけが、実装者の意図説明 (project.json の behavior / summary /
# motivation) を受け取る judge になる。

def _beacon_section(path, body="@@ -1 +1 @@\n-old\n+new\n"):
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n{body}")


def test_bookkeeping_is_dropped_from_each_commit_and_disclosed(monkeypatch):
    data = _fake_project()
    code = _beacon_section("engine/run.py", "@@ -1 +1 @@\n-x\n+y\n")
    ledger = _beacon_section(".beacon/project.json",
                             '@@ -1 +1 @@\n-{"summary": "前回"}\n+{"summary": "今回"}\n')
    diffs = {
        "aaaaaaa1111": code + ledger,   # 混在 → コードだけ残る
        "bbbbbbb2222": ledger,          # 作業記録だけ → 空になる
    }
    _install_fakes(monkeypatch, data, diffs, errors={"ccccccc3333"})

    artifact, gaps = commands._collect_whole_target_artifact("ms-T")
    by_hash = {c["hash"]: c for c in artifact["commits"]}

    mixed = by_hash["aaaaaaa1111"]["diff"]
    assert "engine/run.py" in mixed
    assert ".beacon/project.json" not in mixed, "作業記録が judge に渡っている"
    assert "前回" not in mixed and "今回" not in mixed

    only = by_hash["bbbbbbb2222"]
    assert only["diff"].strip() == ""
    # 「変更が無かった」と「作業記録しか無かった」を区別する。混ぜると judge は
    # 「この commit は何も変えていない」と読む。
    assert "bookkeeping-only" in only["empty_reason"]

    # 黙って削らない — 落としたことが gaps に出る
    assert any(".beacon" in g for g in gaps), gaps


def test_no_disclosure_when_the_target_has_no_bookkeeping(monkeypatch):
    """落とす物が無いときに gap を作らない (常に出る警告は読まれなくなる)。"""
    data = _fake_project()
    code = _beacon_section("engine/run.py", "@@ -1 +1 @@\n-x\n+y\n")
    _install_fakes(monkeypatch, data, {"aaaaaaa1111": code,
                                       "bbbbbbb2222": code},
                   errors={"ccccccc3333"})

    _artifact, gaps = commands._collect_whole_target_artifact("ms-T")
    assert not any(".beacon" in g for g in gaps), gaps
