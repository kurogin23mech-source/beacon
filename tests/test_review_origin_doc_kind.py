"""思想 (philosophy) レビューの原典解決が、provenance を落として全体を落とした件。

何が起きていたか
----------------
``beacon review context --type philosophy --pr 6 --origin-doc <spec>`` が
``UnboundLocalError: local variable 'origin_provenance' referenced before
assignment`` で落ちていた (2026-11-05 実測)。原典 (= judge に渡す照合基準) の
解決が if/elif で呼び出し元のローカル変数に代入する形だったため、e-7345 で
``origin_provenance`` (= その原典をどの作業ツリーから読んだか) を足したとき、
repo-file の枝にだけ入り **doc の枝は名前を束縛しないまま** になった。

repo-file を原典に持つ ax / maintainability は通るので、壊れていたのは
思想レビューだけ。しかも「レビュー type が 1 つ丸ごと到達不能」という形なので、
走らせようとした人が traceback を読むまで誰にも分からない。前セッションの
引き継ぎが「次セッション最優先: PR #6 に思想レビューをかける」だったため、
最優先の作業がそこで止まった。

この file が留めるもの
----------------------
1. 原典の解決は **3 つ組を return する 1 つの関数** であること (枝が呼び出し元の
   ローカルに代入する形に戻ると、次に 4 つ目の値が増えたとき同じ事故が再発する)
2. doc 原典は provenance を持たない (= ``None``) こと。チェックアウトが無いので
   「どの作業ツリーか」を言えないのが正しい状態で、嘘の出所を載せない
3. 両方の原典 kind が、assemble に渡せる 3 つ組を実際に返すこと
"""

from __future__ import annotations

import ast
import inspect

import pytest

import commands
import review_spine


# --- 1. 解決は 3 つ組を return する 1 関数 ------------------------------------

def test_single_type_path_takes_the_triple_from_one_call():
    """``cmd_review_context`` は原典 3 値を 1 回の呼び出しで受け取る。

    枝ごとに代入する形 (= 事故の原因) に戻っていないことを、構文木で確かめる。
    """
    tree = ast.parse(inspect.getsource(commands.cmd_review_context))
    names = {"origin_id", "origin_content", "origin_provenance"}
    triple_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Tuple):
                bound = {e.id for e in tgt.elts if isinstance(e, ast.Name)}
                if bound == names:
                    assert isinstance(node.value, ast.Call), (
                        "原典 3 値は関数呼び出しの戻り値で受け取る")
                    triple_calls += 1
            elif isinstance(tgt, ast.Name) and tgt.id in names:
                pytest.fail(
                    f"{tgt.id} を枝の中で個別に代入している。原典の解決は 3 つ組を "
                    f"return する 1 関数に寄せる (= 枝が 1 つ忘れても気づけないため)")
    assert triple_calls == 1, "原典 3 値を受け取る箇所は 1 つ"


def test_resolver_returns_three_values_on_every_path():
    """解決関数の全 return が 3 要素。1 つでも欠けると assemble 側で落ちる。"""
    tree = ast.parse(inspect.getsource(commands._resolve_review_origin))
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert returns, "原典の解決は値を return する"
    for r in returns:
        assert isinstance(r.value, ast.Tuple) and len(r.value.elts) == 3, (
            "return は (origin_id, origin_content, provenance) の 3 つ組")


# --- 2. doc 原典は provenance を持たない -------------------------------------

class _FakeStore:
    def __init__(self, doc):
        self._doc = doc

    def get_document(self, doc_id):
        return self._doc


def test_doc_origin_resolves_with_no_provenance(monkeypatch):
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"content": "# SPEC\n方針1 ..."}))
    gaps = []
    origin_id, content, provenance = commands._resolve_review_origin(
        "philosophy", {"id": "philosophy", "origin": {"kind": "doc"}},
        "wbx8BegbDZgJ1NfaLcOA", gaps)
    assert origin_id == "wbx8BegbDZgJ1NfaLcOA"
    assert "方針1" in content
    # チェックアウトが無いので出所は言えない。ここで何かを載せると、judge は
    # 「どの版の原典で判定したか」を誤って信じる。
    assert provenance is None
    assert gaps == []


def test_doc_origin_empty_body_is_a_gap_not_a_crash(monkeypatch):
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"content": "   "}))
    gaps = []
    _, content, provenance = commands._resolve_review_origin(
        "philosophy", {"origin": {"kind": "doc"}}, "d-1", gaps)
    assert content.strip() == ""
    assert provenance is None
    assert len(gaps) == 1 and "原典" in gaps[0]


def test_doc_origin_missing_doc_exits(monkeypatch):
    monkeypatch.setattr(commands, "get_store", lambda: _FakeStore(None))
    with pytest.raises(SystemExit):
        commands._resolve_review_origin(
            "philosophy", {"origin": {"kind": "doc"}}, "nope", [])


def test_doc_origin_without_origin_doc_exits():
    with pytest.raises(SystemExit):
        commands._resolve_review_origin(
            "philosophy", {"origin": {"kind": "doc"}}, "", [])


def test_unknown_origin_kind_exits():
    with pytest.raises(SystemExit):
        commands._resolve_review_origin("weird", {"origin": {"kind": "???"}}, "", [])


# --- 3. 解決した 3 つ組が assemble をそのまま通る -----------------------------

def test_doc_origin_triple_assembles(monkeypatch):
    """思想レビューの bundle が実際に組めること (= 到達不能に戻っていない)。"""
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"content": "# ms-10 SPEC"}))
    gaps = []
    origin_id, content, provenance = commands._resolve_review_origin(
        "philosophy", {"origin": {"kind": "doc"}}, "d-spec", gaps)
    bundle = review_spine.assemble_review_context(
        "philosophy",
        origin_id=origin_id,
        origin_content=content,
        diff_text="@@ -1 +1 @@\n-a\n+b",
        mode="diff",
        target_ref="PR #6",
        gaps=gaps,
        origin_provenance=provenance,
    )
    assert bundle["review_type"] == "philosophy"
    assert bundle["origin"]["id"] == "d-spec"
    # provenance が無い doc 原典は 2 key の形を保つ (review_spine._origin_slot の契約)
    assert set(bundle["origin"].keys()) == {"id", "content"}
