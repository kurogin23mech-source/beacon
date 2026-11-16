"""doc 原典のレビュー (= 思想) が **実行できる** ことの固定 (ms-119)。

**何が起きていた** (独立思想レビュー high, 2026-11-11): SPEC が「思想レビューを
Advisory クラスの第 2 instance に」と宣言している機構が、**どの経路からも
UnboundLocalError で落ちていた**。``cmd_review_context`` は ``origin_provenance``
を ``origin_kind == "repo-file"`` の分岐の中だけで束縛し、下の
``assemble_review_context`` 呼び出しで無条件に読んでいた。doc 原典 (思想の SPEC /
vision) は別の分岐を通るので、束縛されないまま読まれる。

回帰は 2026-11-05 の 23ef1191 (PR #836) で入り、**6 日間 main で動かないまま**
ms-119 のコミットが 7 本積まれた。その間、完了節目で思想レビューが走ったことは
一度も無く、**走らなかったことを記録する機構も無かった**。

**なぜ既存の試験が捕まえなかったか**: ``tests/test_review_whole_target_artifact.py``
は philosophy の ``--target`` を **エラー経路** (``--pr`` との衝突) でしか触らず、
``tests/test_review_origin_provenance_e7345.py`` は repo-file / decision-verification
の経路しか見ていなかった。**doc 原典で assemble まで到達する経路に試験が 1 つも
無かった** —— 宣言された instance が動かないことを、機械が誰にも告げなかった。

この module が固定する契約: doc 原典のレビューが ``--target`` / ``--pr`` /
``--diff-ref`` のどの入口からも bundle を組み立てられること。``origin_provenance``
は doc 原典では **``None`` が正しい** (解決元のファイルパスが存在しない。
``review_spine.assemble_review_context`` の既定も None で docstring も optional)。
"""

import io
import json
from contextlib import redirect_stdout

import pytest

import commands
import review_spine as rs

SPEC_DOC = "test-philosophy-origin-doc"


@pytest.fixture
def doc_origin(monkeypatch, tmp_path):
    """doc 原典を 1 件だけ持つ store を立てる。"""
    class _Store:
        def get_document(self, doc_id):
            if doc_id != SPEC_DOC:
                return None
            return {"content": "# テスト用 SPEC\n\n## 設計方針\n\n1. 構造で閉じる。\n"}

    monkeypatch.setattr(commands, "get_store", lambda: _Store())
    monkeypatch.setattr(commands, "_collect_change_diff",
                        lambda *a, **k: "diff --git a/x b/x\n+y\n")
    monkeypatch.setattr(
        commands, "_pr_diff_collection",
        lambda pr, pr_repo, gaps: (["git", "diff"], "o/r#1", "o/r", "f" * 40))
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "philosophy")
    monkeypatch.setenv("BEACON_ORIGIN_DOC", SPEC_DOC)
    for k in ("BEACON_PR", "BEACON_PR_REPO", "BEACON_DIFF_REF",
              "BEACON_TARGET_ID", "BEACON_MODE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _run():
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_review_context()
    raw = buf.getvalue().strip()
    assert raw, "bundle が出ていない"
    return json.loads(raw.splitlines()[-1])


def test_philosophy_is_a_doc_origin_type():
    """前提: 思想レビューの原典は doc (repo-file ではない)。

    これが崩れたらこの module の測る対象が変わるので、前提として固定する。
    """
    desc = rs.load_review_types().get("philosophy") or {}
    assert (desc.get("origin") or {}).get("kind") == "doc", desc.get("origin")


def test_doc_origin_bundle_builds_from_diff_ref(doc_origin):
    """**本体**: doc 原典 + --diff-ref で bundle が組める。

    修正前はここで UnboundLocalError だった。
    """
    doc_origin.setenv("BEACON_DIFF_REF", "a...b")
    d = _run()
    assert (d.get("origin") or {}).get("id") == SPEC_DOC
    assert d.get("mode") == "diff"


def test_doc_origin_bundle_builds_from_pr(doc_origin):
    """同じ入口違い (--pr)。Skill が文書化している形。"""
    doc_origin.setenv("BEACON_PR", "1")
    doc_origin.setenv("BEACON_PR_REPO", "o/r")
    d = _run()
    assert (d.get("origin") or {}).get("id") == SPEC_DOC


def test_doc_origin_provenance_is_none_not_missing(doc_origin):
    """doc 原典に解決元パスは無いので、provenance は **None が正しい**。

    でっち上げた値を載せると、読み手は「どこかのファイルから解決した」と誤読する。
    """
    doc_origin.setenv("BEACON_DIFF_REF", "a...b")
    d = _run()
    origin = d.get("origin") or {}
    assert origin.get("resolved_from") in (None, "", "doc"), origin
    assert "path" not in origin or not origin.get("path"), origin


def test_a_doc_origin_type_still_refuses_a_missing_origin_doc(doc_origin):
    """原典を渡さなければ **止まる** (黙って原典なしで判定しない)。

    SPEC § 方針5 の「原典の欠落は finding」に対応する入口側の拒否。
    """
    doc_origin.delenv("BEACON_ORIGIN_DOC", raising=False)
    doc_origin.setenv("BEACON_DIFF_REF", "a...b")
    with pytest.raises(SystemExit) as ei:
        with redirect_stdout(io.StringIO()):
            commands.cmd_review_context()
    assert ei.value.code == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
