"""動詞パスの抽出が 1 箇所に在り、3 階層目を潰さないことの試験 (ms-166 e-6792)。

何が起きていたか
----------------
``scripts/check-cli-help-drift.py`` には動詞パスの抽出器が **2 つ併存** していた。

* ``_extract_verb``  — ``parts_in[1:3]`` で **2 トークン上限**。コメントも
  「at most two verb tokens (subcmd + subsubcmd)」と明記。そのため
  ``beacon session fork cleanup <path>`` / ``beacon session fork <ms-id>`` /
  ``beacon session fork list`` が **同じ ``session fork`` に潰れ**、3 階層以上の
  動詞は 2 階層目までしか存在確認されなかった。
* ``_registry_path`` — 上限なし (旗の検査用、PR #771 で新設)。

「動詞パスとは何か」に対する答えが 1 ファイル内に 2 つあるのは、このリポジトリが
繰り返し潰してきた drift の形そのもの (PR #770 の独立保守性レビューが指摘)。

統一した効果 (実測)
-------------------
上限を外した途端、**2 トークン上限が隠していた実在の drift を 1 件検出**した:

    missing from README ## CLI Commands tables:
        beacon sales target list

README は 2 つの動詞を 1 行に ``\\|`` で畳んでいた
(``beacon sales target <user> <amount> \\| list``) が、help 台帳では別々の 2 行。
抽出器は README 行の動詞を ``sales target`` (= ``<user>`` で停止) と読むので、
``sales target list`` には README 行が無い状態だった。2 行に分割して解消。
"""

from __future__ import annotations

import importlib.util
import os
import sys

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                       "check-cli-help-drift.py")


def _mod():
    """ハイフン入りファイル名なので spec から読み込む。"""
    spec = importlib.util.spec_from_file_location("_cli_drift", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_cli_drift"] = m
    spec.loader.exec_module(m)
    return m


def test_a_three_level_verb_is_not_collapsed_to_two():
    """これが欠陥の本体。3 階層目まで取れること。"""
    m = _mod()
    assert m.verb_path("beacon session fork cleanup <path>") == [
        "session", "fork", "cleanup"]
    # 同じ 2 階層目を共有する別の動詞が **区別される** こと (潰れると全部同じになる)
    assert m.verb_path("beacon session fork list") == ["session", "fork", "list"]
    assert m.verb_path("beacon session fork <ms-id>") == ["session", "fork"]
    assert len({tuple(m.verb_path(t)) for t in (
        "beacon session fork cleanup <path>",
        "beacon session fork list",
        "beacon session fork <ms-id>")}) == 3, "3 つの動詞が潰れて区別できていない"


def test_the_verb_path_rule_lives_in_exactly_one_place():
    """抽出の規則を 2 箇所に書かないこと。

    ``_extract_verb`` (文字列版) と ``_registry_path`` (list 版) は形が違うだけで、
    規則は ``verb_path`` が 1 箇所で持つ。両方が自前で走査を書いていたのが欠陥。
    """
    import ast
    tree = ast.parse(open(_SCRIPT, encoding="utf-8").read())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _loops(name):
        return [x for x in ast.walk(fns[name]) if isinstance(x, ast.For)]

    # **構文木で見る。** 散文のコメントに "parts_in[1:3]" のような字面が出るだけで
    # 落ちる部分一致検査は、厳しすぎる偽陽性になる (実際この試験の初版がそれで落ちた
    # — verb_path の docstring が旧実装の字面に言及しているため)。見るのは
    # 「走査のループが何箇所にあるか」。
    assert len(_loops("verb_path")) == 1, "verb_path に走査が 1 つだけ在ること"
    for name in ("_extract_verb", "_registry_path"):
        assert not _loops(name), (
            name + " が自前で走査しています — 規則は verb_path に 1 つだけ置く")
        called = {x.func.id for x in ast.walk(fns[name])
                  if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)}
        assert "verb_path" in called, name + " が verb_path を通っていません"


def test_prose_shaped_rows_are_still_not_mistaken_for_verbs():
    """トークンの検査は残す。

    台帳には散文混じりの行 (``beacon log [message]``) があるので、置き場所や
    大文字始まりで止まらないと、散文を動詞として数えてしまう。
    """
    m = _mod()
    assert m.verb_path("beacon log [message]") == ["log"]
    assert m.verb_path("beacon status --json") == ["status"]
    assert m.verb_path("beacon Foo bar") == [], "大文字始まりを動詞として拾っている"
    assert m.verb_path("beacon") == [], "起動行は空のパス"
    assert m.verb_path("not-beacon x") is None


def test_the_registry_path_still_drops_the_bare_launch_line():
    """旗の検査は「どの動詞の旗か」が要るので、起動行 (動詞なし) は対象外。

    verb_path は ``[]`` を返すが、_registry_path は None に倒す。この差分だけが
    2 つの口の違いで、それ以外の規則は共有する。
    """
    m = _mod()
    assert m.verb_path("beacon") == []
    assert m._registry_path("beacon") is None
    assert m._registry_path("beacon stop scoped <target>") == ["stop", "scoped"]


def test_the_guard_itself_is_green_on_this_tree():
    """統一した抽出器でこのリポジトリが緑であること。

    上限を外した結果 README の 1 行が drift として出たので、そこも直してある
    (``sales target`` と ``sales target list`` を別行に分割)。この試験はその状態を
    固定する — 片方が戻ったら赤くなる。
    """
    m = _mod()
    report = m.collect_drift() if hasattr(m, "collect_drift") else None
    if report is None:
        import subprocess
        r = subprocess.run([sys.executable, _SCRIPT], capture_output=True, text=True)
        assert r.returncode == 0, (
            "cli-drift ガードが赤です:\n" + r.stdout + r.stderr)
    else:
        assert report.get("ok"), report
