"""構文木から「どの関数がどれを呼んでいるか」を構造抽出する共有プリミティブ。

ms-166 の構造ガードは 3 回続けて同じ問いを立てた: **呼び出し X は関数 Y から
到達するか**。

  * e-6602 — 完遂の書き込みが収束口を通っているか
  * e-6757 — 判断記録の書き込みが失敗契約の受け口の中に在るか
  * e-6637 — API の書き込み扉がガードを通っているか

3 ファイルがそれぞれ同じ ``ast.Name.id`` / ``ast.Attribute.attr`` の分岐を手書きして
いたため (保守性レビュー PR#785 M-3)、4 つ目を書く人に「どれを写すか」の選択を
迫り、プリミティブを直すときは 3 箇所を探して当て直す必要があった。ここに 1 本だけ
置き、**「何をガードと見なすか」の述語だけを呼び出し側が持つ**。

なぜ substring 検索 (``"name" in source``) にしないか: docstring やコメントに名前が
出ているだけで素通りする false-pass が起きる。緑のガードは信頼されるので、偽の安全は
ガード無しより悪い。実際 e-6757 の修正後は病理の説明として
``except BaseException: pass`` の文字列が docstring に残っており、文字列一致の判定は
偽陽性と偽陰性を同時に起こす。だから常に ``ast.Call`` だけを数える。
"""
from __future__ import annotations

import ast


def callee_name(call: ast.Call) -> str:
    """呼び出しノードから呼ばれている名前を返す (``f()`` と ``o.f()`` の両形)。"""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    return getattr(f, "attr", "") or ""


def called_names(node: ast.AST) -> set:
    """``node`` の内側で **実際に呼ばれている** 関数名の集合。

    ``node`` は関数定義でもクラス定義でもモジュールでもよい。docstring / コメントの
    言及は含まれない (``ast.Call`` のみを数える)。
    """
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = callee_name(sub)
            if name:
                out.add(name)
    return out


def function_named(tree: ast.AST, name: str):
    """``tree`` から名前 ``name`` の関数定義を返す (無ければ None)。"""
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    return None


def called_names_in_function(tree: ast.AST, name: str) -> set:
    """``tree`` の関数 ``name`` から呼ばれている関数名の集合 (無い関数なら空集合)。"""
    fn = function_named(tree, name)
    return called_names(fn) if fn is not None else set()


def functions_of(node: ast.AST) -> dict:
    """``node`` 直下の関数定義を ``{名前: 定義ノード}`` で返す (入れ子は含まない)。

    クラスのメソッド一覧を取るときに使う。``ast.walk`` を使わないのは、入れ子の
    関数まで同じ階層に混ぜると「このクラスのメソッド」という問いの答えがずれるため。
    """
    return {fn.name: fn for fn in getattr(node, "body", [])
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
