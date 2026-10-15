"""採否を渡さない限りレビューの関門を開けない (ms-166 e-6997)。

経緯
----
独立思想レビュー (high) が、この MS の **旗艦** について次を実測した:

    溶接は正しい漏斗に在るが、中身が空でも通る。``--adjudications`` は任意旗で、
    空なら判断記録を書かずに早期 return する一方、review-due の印は消え
    「approve/merge の gate を通過できます」と exit 0 で言う。

こちらでも使い捨てプロジェクトで再現した: 採否を一切渡さずに関門が開き、残るのは
stderr の ℹ 1 行だけだった。つまり捕獲は「AI が旗を渡すこと」に依存し、依存先は
Skill の散文 —— SPEC が消すと宣言した「お願い依存」そのもの。**実践として毎回
渡していても、それは構造の証明にならない。**

この module が固定する契約
--------------------------
* 採否 (``--adjudication-summary`` / ``--adjudications``) か、findings が 0 件だった
  ことの明示 (``--no-findings``) の **どちらかが無ければ関門は開かない**。
* 両方を同時に渡すのは拒否する (どちらが本当かを機械が決められない)。
* ``--no-findings`` のときも判断記録を **1 行残す** —— 以前は「0 件だった」と
  「書き忘れた」が記録上まったく同じ (どちらも行ゼロ) で区別できなかった。
* 関門は ``--pr`` / ``--target`` の **両方** に効く (片方だけに置くと、同じ状態に
  到達する兄弟の経路が素通りする = この MS が何度も直している型)。
"""
import io
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
BEACON = os.path.join(ROOT, "bin", "beacon")
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _project(tmp_path, *, pr="4242", repo="o/r", target=""):
    """使い捨てプロジェクト。**この repo の .beacon は絶対に触らない** (本番)。"""
    proj = tmp_path / "proj"
    (proj / ".beacon" / "triggers").mkdir(parents=True)
    (proj / ".beacon" / "project.json").write_text(
        json.dumps({"name": "p", "summary": "", "milestones": [
            {"id": "ms-9", "title": "t", "status": "in_progress", "entries": []}]}),
        encoding="utf-8")
    trig = proj / ".beacon" / "triggers"
    if target:
        (trig / f"review-due-{target}.json").write_text(
            json.dumps({"name": f"review-due-{target}", "kind": "review-due",
                        "target_id": target, "bindings": ["ax"]}),
            encoding="utf-8")
    else:
        (trig / f"ax-review-due-{pr}@{repo.replace('/', '~')}.json").write_text(
            json.dumps({"name": f"ax-review-due-{pr}", "kind": "ax-review-due",
                        "pr_number": pr, "review": "ax"}), encoding="utf-8")
    return proj


def _run(proj, *args):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("BEACON_"):
            env.pop(k)
    return subprocess.run([BEACON, "review", *args], cwd=str(proj), env=env,
                          capture_output=True, text=True)


def _triggers(proj):
    d = proj / ".beacon" / "triggers"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# --------------------------------------------------------------------------
# default-deny — 採否が無ければ開かない
# --------------------------------------------------------------------------

@pytest.mark.parametrize("selector", [("--pr", "4242", "--repo", "o/r"),
                                      ("--target", "ms-9")])
def test_done_refuses_to_open_without_an_adjudication(tmp_path, selector):
    """採否も --no-findings も無い呼び出しは、**関門を開けずに** 非 0 で終わる。"""
    target = selector[1] if selector[0] == "--target" else ""
    proj = _project(tmp_path, target=target)
    before = _triggers(proj)
    assert before, "前提: レビュー未実施の印が在る"

    r = _run(proj, "done", "--type", "ax", *selector)

    assert r.returncode != 0, f"採否なしで成功している: {r.stdout}"
    assert "採否" in r.stderr, r.stderr
    assert "--no-findings" in r.stderr, "0 件だった場合の出口を案内していない"
    assert _triggers(proj) == before, (
        "拒否したのに印を消している (= 関門が開いた)")


@pytest.mark.parametrize("selector", [("--pr", "4242", "--repo", "o/r"),
                                      ("--target", "ms-9")])
def test_no_findings_must_be_explicit_to_open_the_gate(tmp_path, selector):
    """``--no-findings`` を明示すれば開く (0 件は正当な結果)。"""
    target = selector[1] if selector[0] == "--target" else ""
    proj = _project(tmp_path, target=target)

    r = _run(proj, "done", "--type", "ax", *selector, "--no-findings")

    assert r.returncode == 0, r.stderr
    assert "review-due 解消" in r.stdout or "実施を記録" in r.stdout, r.stdout


@pytest.mark.parametrize("selector", [("--pr", "4242", "--repo", "o/r"),
                                      ("--target", "ms-9")])
def test_an_adjudication_summary_also_opens_the_gate(tmp_path, selector):
    """採否を渡す通常の形はそのまま通る (関門を厳しくして使えなくしていない)。"""
    target = selector[1] if selector[0] == "--target" else ""
    proj = _project(tmp_path, target=target)

    r = _run(proj, "done", "--type", "ax", *selector,
             "--adjudication-summary", "high 1 件を採用")

    assert r.returncode == 0, r.stderr


def test_done_refuses_both_no_findings_and_an_adjudication(tmp_path):
    """矛盾した指定は拒否する (どちらが本当かを機械が決められない)。"""
    proj = _project(tmp_path)
    before = _triggers(proj)

    r = _run(proj, "done", "--type", "ax", "--pr", "4242", "--repo", "o/r",
             "--no-findings", "--adjudication-summary", "x")

    assert r.returncode != 0
    assert "--no-findings" in r.stderr
    assert _triggers(proj) == before, "拒否したのに印を消している"


# --------------------------------------------------------------------------
# 0 件も記録に残る (= 「0 件だった」と「書き忘れた」を区別できる)
# --------------------------------------------------------------------------

def test_no_findings_is_recorded_as_a_decision(monkeypatch):
    """``--no-findings`` のとき判断記録を 1 行書くこと。

    以前は採否が空なら何も書かずに戻っていたので、0 件と書き忘れが記録上同じだった。
    cloud 経路なので API client を差し替えて payload を捕まえる (CLI の出力には
    現れない欠陥 = 実測しないと分からない形)。
    """
    import commands

    posted = []

    class _Client:
        def record_decision(self, project_id, payload):
            posted.append(payload)
            return {"decision_id": "dec-1"}

    import commands_shared
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (_Client(), {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "decided_by_for_review",
                        lambda: "autonomous-AI")

    commands._record_review_adjudication_decision(
        "ax", "4242", "", [], "o/r", no_findings=True)

    assert len(posted) == 1, f"0 件の明示が記録されていない: {posted}"
    assert posted[0]["kind"] == "review-adjudication"
    assert "0 件" in (posted[0]["decision"] or ""), posted[0]
    assert "review:ax" in (posted[0].get("evidence") or [])


def test_nothing_is_recorded_when_neither_is_given(monkeypatch):
    """最後の受け皿: 両方空のときは書かない (呼び出し側が既に拒否しているが、
    ここだけ先に緩めても勝手に書き始めない形を固定する)。"""
    import commands, commands_shared

    posted = []

    class _Client:
        def record_decision(self, project_id, payload):
            posted.append(payload)
            return {}

    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (_Client(), {"project_id": "p1"}))
    commands._record_review_adjudication_decision("ax", "4242", "", [], "o/r")
    assert posted == []


# --------------------------------------------------------------------------
# 文法の単一ソース
# --------------------------------------------------------------------------

def test_the_help_registry_advertises_the_requirement():
    """help の単一ソースが「採否 か --no-findings が必須」と読める形で載せること。

    旗を増やしたのに help が「任意」のままだと、--help だけ読む人が実行して
    エラーで学ぶ形になる (= この MS で直した型)。
    """
    import commands
    rows = [r for r in commands._help_registry()
            if r.get("command", "").startswith("beacon review done")]
    assert len(rows) == 1, rows
    cmd = rows[0]["command"]
    assert "--no-findings" in cmd, f"文法に --no-findings が無い: {cmd}"
    assert "--adjudication-summary" in cmd, cmd
    # 任意旗の列ではなく **文法** に出ていること (= 排他の必須として読める)。
    assert "|" in cmd.split("--adjudication-summary")[1].split(")")[0], cmd


def test_the_bash_front_accepts_no_findings():
    """bash フロントが旗を受けること (python 側だけ受ける非対称を作らない)。"""
    src = io.open(BEACON, encoding="utf-8").read()
    arm = src[src.index('        done)'):]
    arm = arm[:arm.index('            skip)')]
    assert "--no-findings)" in arm, "bash の review done が --no-findings を受けない"
    assert "BEACON_REVIEW_NO_FINDINGS=" in arm, "python 側へ渡していない"
