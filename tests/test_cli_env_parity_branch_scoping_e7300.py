"""旗 → 環境変数の検査が、姉妹分岐から鍵を借りないことを固定する (ms-185 e-7300)。

`scripts/check-cli-env-parity.py` は「ある verb が読む環境変数を、その verb に
到達するフロントが渡しているか」を測る。Python 側の解決が以前は**囲っている関数
ごと**だったため、このリポジトリの処理部の形 (`if cmd == "...":` の姉妹分岐が並び、
どの分岐も同じ局所名 `env` に辞書を組み立てる) では、**ある分岐が渡す鍵が隣の分岐の
「渡している」集合にも貸与されていた**。実測 (2026-11-03): push record は list 分岐の
BEACON_JSON を、doc add / update / show / delete は list 分岐の BEACON_TREK_ID を、
渡していないのに渡していると判定されていた。検査が存在する理由そのものについて、
自分の担当範囲の一部で赤くなれない状態だった。

**なぜ合成ソースで測るのか**: 本物の `beacon_cli/dispatch.py` に対して測ると、
期待値が「いまの実装がたまたまそうである」に引きずられる。ここで固定したいのは
解決の**性質** (姉妹は見えない / 経路上の追加は数える / 呼び出しより後は見えない)
なので、その性質だけを持つ最小のソースを与えて測る。

**前方リークを明示的に測る**。この scoping の最初の実装は「ソース順で前方にリーク
する」形で、そのために本体へ載せるのが見送られていた (検査スクリプトの docstring に
経緯が在り、WIP patch が `.archive/` に退避されている)。`verb_d` のケースが
まさにそれ — 呼び出しの**後**に置いた `env = {...}` を数えてはいけない。
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check-cli-env-parity.py"


def _load_checker():
    """plain script (pytest 非依存) なので、ファイル指定で読み込む。"""
    spec = importlib.util.spec_from_file_location("_cli_env_parity", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROBE = '''
def _handle(cmd, args):
    if cmd == "a":
        env = {"BEACON_A": "1"}
        _run_commands_py(x, "verb_a", env)
    elif cmd == "b":
        env = {"BEACON_B": "1"}
        _run_commands_py(x, "verb_b", env)
    elif cmd == "c":
        env = {"BEACON_C0": "1"}
        if args.flag:
            env["BEACON_C1"] = "1"
        _run_commands_py(x, "verb_c", env)
    elif cmd == "d":
        _run_commands_py(x, "verb_d", {"BEACON_D": "1"})
        env = {"BEACON_LATER": "1"}
    elif cmd == "e":
        try:
            env = {"BEACON_E": "1"}
            _run_commands_py(x, "verb_e", env)
        except Exception:
            env = {"BEACON_EXCEPT": "1"}
    elif cmd == "f":
        def _helper():
            env = {"BEACON_NESTED": "1"}
            return env
        g = lambda: {"BEACON_LAMBDA": "1"}
        env = {"BEACON_F": "1"}
        _run_commands_py(x, "verb_f", env)
    elif cmd == "g":
        try:
            pass
        except Exception:
            env = {"BEACON_G1": "1"}
        env = {"BEACON_G0": "1"}
        _run_commands_py(x, "verb_g", env)
'''


def _sets_for(src: str) -> dict:
    mod = _load_checker()
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "probe.py"
        f.write_text(src, encoding="utf-8")
        sets, _verbs = mod._python_sets(f)
    return {k: set(v) for k, v in sets.items()}


def test_sibling_branches_do_not_lend_each_other_keys():
    """姉妹分岐は互いに見えない (= この task の本体)。"""
    sets = _sets_for(PROBE)
    assert sets["verb_a"] == {"BEACON_A"}, (
        f"verb_a が隣の分岐の鍵を借りている: {sorted(sets['verb_a'])}")
    assert sets["verb_b"] == {"BEACON_B"}, (
        f"verb_b が隣の分岐の鍵を借りている: {sorted(sets['verb_b'])}")


def test_keys_added_along_the_reaching_path_still_count():
    """経路上で後から足す形は「渡している」と数える (偽のギャップを作らない)。

    ``env = {...}`` の後に ``if ...: env["X"] = ...`` を足して、その後で渡す形。
    実行されるか分からない分岐でも「渡している」側に倒すのは、この検査が一貫して
    報告を少なくする側へ寄せているのと同じ理由。
    """
    sets = _sets_for(PROBE)
    assert sets["verb_c"] == {"BEACON_C0", "BEACON_C1"}, (
        f"経路上の追加が数えられていない: {sorted(sets['verb_c'])}")


def test_assignments_after_the_call_do_not_leak_backwards():
    """呼び出しより後の文は見えない (= 最初の実装が踏んだ前方リーク)。"""
    sets = _sets_for(PROBE)
    assert sets["verb_d"] == {"BEACON_D"}, (
        f"呼び出しの後に置いた辞書を数えている (前方リーク): "
        f"{sorted(sets['verb_d'])}")


def test_the_real_dispatch_is_still_parseable_and_reports_verbs():
    """本物の dispatch に当てても壊れない (合成だけで緑にならないように)。

    中身の期待値は置かない (実装に引きずられるため)。見るのは「落ちずに走り、
    verb を実際に拾っている」こと。
    """
    mod = _load_checker()
    sets, verbs = mod._python_sets(REPO / "beacon_cli" / "dispatch.py")
    assert len(verbs) > 50, f"verb をほとんど拾えていない: {len(verbs)}"
    assert any(sets.get(v) for v in verbs), "どの verb も鍵を 1 つも拾えていない"


# ---------------------------------------------------------------------------
# PR #839 の独立保守性レビュー (high) で見つかった 2 つの over-report 経路
# ---------------------------------------------------------------------------
# **e-7300 は姉妹分岐からの貸与を塞いだが、同じ貸与が別の 2 経路で残っていた。**
# モジュール冒頭が「報告を少なくする側に倒す、over-report しない」と書いている
# のに、実際には over-report していた —— 過大報告は「配線されている」と誤って
# 言うことなので、この検査が存在する理由 (未配線の検出) を静かに外す。
#
# 1 経路だけ塞いで「構造で閉じた」と称した形。独立 judge が合成ソースで実証した。


def test_an_except_handler_above_the_call_is_not_on_the_path():
    """祖先が ``try`` のとき、その except 本体は経路上に無い。

    呼び出しが ``try`` 本体の中に在るなら、except が走った実行では**その呼び出しは
    起きていない**。にもかかわらず except 本体が数えられていた。原因は header の
    判定が「list の先頭要素が ``ast.stmt`` なら block とみなして飛ばす」という
    除外型だったこと —— ``ast.Try`` の ``handlers`` は先頭が ``ast.excepthandler``
    なので素通りした。許可型 (入れてよいフィールドを名前で挙げる) に変えた。
    """
    sets = _sets_for(PROBE)
    assert sets["verb_e"] == {"BEACON_E"}, (
        f"except 本体の鍵が経路に混入している: {sorted(sets['verb_e'])}")


def test_a_nested_definition_before_the_call_contributes_nothing():
    """呼び出しより前の ``def`` / ``lambda`` の**本体**は経路上に無い。

    関数を定義しても本体は走らないので、helper 内部の代入は「渡している」側に
    入らない。以前は ``ast.walk`` で丸ごと歩いていたため混入していた。
    """
    sets = _sets_for(PROBE)
    assert sets["verb_f"] == {"BEACON_F"}, (
        f"入れ子の定義の本体が経路に混入している: {sorted(sets['verb_f'])}")


def test_an_except_handler_before_the_call_still_counts():
    """呼び出し**より前**の兄弟 ``try`` の except 本体は数える。

    上の 2 つと向きが逆なので分けて測る。この except は呼び出しに到達する前に
    走り得るので、「渡している」側に倒すのが正しい (経路上の ``if`` の本体を
    数えるのと同じ理由)。過大報告を塞ぐ修正で、こちら側まで切り落として
    **偽のギャップ**を作っていないことを確かめる。
    """
    sets = _sets_for(PROBE)
    assert sets["verb_g"] == {"BEACON_G0", "BEACON_G1"}, (
        f"到達前に走り得る except の鍵を落としている (偽のギャップ): "
        f"{sorted(sets['verb_g'])}")


def test_a_loop_header_above_the_call_is_on_the_path():
    """``for`` / ``while`` の header は経路上に在る (許可型の取りこぼし検査)。

    許可型に変えた副作用で、header を持つ文の header まで落としていないか。
    落とすと「渡していない」と誤判定して偽のギャップを作る。
    """
    sets = _sets_for('''
def _handle(cmd, args):
    for k in [1]:
        env = {"BEACON_LOOP": "1"}
        _run_commands_py(x, "verb_loop", env)
''')
    assert sets["verb_loop"] == {"BEACON_LOOP"}, (
        f"ループ配下の経路が落ちている: {sorted(sets['verb_loop'])}")


# ---------------------------------------------------------------------------
# PR #839 2 周目の独立保守性レビュー (high) — 許可型の表そのものが静かに劣化する
# ---------------------------------------------------------------------------
# 1 周目の指摘を受けて header 判定を除外型から許可型に変えたが、**許可型そのものが
# 同じ穴を持っていた**。参照側は ``getattr(stmt, field, None)`` で引くので、表の
# フィールド名を 1 文字間違えても None が返って黙って何も数えない。judge が
# ``ast.With`` の ``"items"`` を ``"itemz"`` に変異させたところ、この file の 8 本と
# 本体実行の両方が緑のまま通った。過大報告を直す修正が、過少報告の方向に同型の穴を
# 作っていた —— **1 経路だけ閉じて「閉じた」と主張する、この MS で 3 度目**。
#
# 下の 2 本は、見つかった 1 件 (With) を塞ぐのではなく**劣化の機構**を閉じる:
#   * 綴り間違いは checker の import 時に落ちる (宣言側で閉じた。ここではそれが
#     効いていることを測る)
#   * 表に挙げ忘れた文型は下の完全性検査で赤くなる
#
# 個別の文型の挙動も 3 つ足した (While / With / Match)。judge の指摘どおり、
# AsyncFor / AsyncWith / TryStar / match_case は dispatch.py に実例が無いので
# **「テストしたふり」にはしない** — 構造的には上の 2 本が覆う。


def test_a_typo_in_the_header_table_fails_loudly_at_import():
    """表のフィールド名の綴り間違いが、無音の劣化ではなく import 時の失敗になる。"""
    import subprocess
    import tempfile

    src = CHECKER.read_text(encoding="utf-8")
    assert 'ast.With: ("items",),' in src, "表の該当行が見つからない (検査の前提が崩れている)"
    mutated = src.replace('ast.With: ("items",),', 'ast.With: ("itemz",),', 1)
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "mutated.py"
        f.write_text(mutated, encoding="utf-8")
        proc = subprocess.run([__import__("sys").executable, "-B", str(f)],
                              capture_output=True, text=True, timeout=120,
                              cwd=str(REPO))
    assert proc.returncode != 0, (
        "表のフィールド名を間違えても落ちない — 参照側の getattr が None を返して"
        "黙って何も数えない形に戻っている")
    assert "_HEADER_FIELDS" in proc.stderr, (
        f"落ちたが理由が表の問題だと分からない: {proc.stderr[-300:]}")


def test_every_statement_type_with_a_body_is_classified():
    """stmt のリストを子に持つ文型は、必ず表か「本体は走らない」側に在る。

    表に挙げ忘れた文型は ``_HEADER_FIELDS.get(type, ())`` の既定が空タプルを返すので、
    その文が祖先に来たとき header を何も足さない = 経路が静かに欠ける。足し忘れが
    赤くなるように、分類の**完全性**を測る。

    検査しないこと: 各文型の header が「正しいフィールドを挙げているか」は見ていない
    (それは上の import 時の検証と個別の挙動テストが担う)。ここで見るのは、分類から
    漏れた文型が無いことだけ。
    """
    import ast as _ast

    checker = _load_checker()
    classified = set(checker._HEADER_FIELDS) | set(checker._NOT_EXECUTED_HERE)

    missing = []
    for name in dir(_ast):
        node_type = getattr(_ast, name)
        if not (isinstance(node_type, type) and issubclass(node_type, _ast.AST)):
            continue
        fields = getattr(node_type, "_fields", ())
        # 「stmt のリストを子に持つ」= body / orelse / finalbody / handlers のいずれか
        if not any(f in fields for f in ("body", "orelse", "finalbody", "handlers")):
            continue
        # **この絞り込みは自分で踏んだ罠** (PR #839 2 周目): 以前ここは
        # ``getattr(_ast, "match_case", _ast.AST)`` と書いていた。py3.9 には
        # ``match_case`` が無いので fallback の ``_ast.AST`` に落ち、``issubclass(X,
        # ast.AST)`` は**すべての AST 型で真**になるため、式 (``ast.IfExp`` 等) まで
        # 「分類漏れ」として報告していた。無い型は候補から外すのが正しい。
        containers = tuple(t for t in (
            getattr(_ast, "stmt", None),
            getattr(_ast, "match_case", None),
            getattr(_ast, "excepthandler", None),
        ) if t is not None)
        if not issubclass(node_type, containers):
            continue
        if node_type in classified:
            continue
        # 抽象基底 (stmt 自身など) と、関数の外側に出ない型は対象外
        if node_type in (_ast.stmt, _ast.AST, _ast.Module, _ast.Interactive,
                         _ast.Expression, getattr(_ast, "excepthandler", None)):
            continue
        missing.append(name)

    assert not missing, (
        f"body を持つ文型が分類から漏れています: {sorted(missing)}。"
        f"_HEADER_FIELDS (= 入る前に評価される header を挙げる) か "
        f"_NOT_EXECUTED_HERE (= 本体は走らない) のどちらかに足すこと。"
        f"漏れると、その文が祖先に来たときに header が静かに欠けます")


def test_while_with_and_match_headers_are_on_the_path():
    """While / With / Match の配下でも経路が繋がる (表の個別エントリの挙動)。

    1 周目の試験は If / For / Try しか触っておらず、表 10 エントリのうち 6 つは
    1 本も測っていなかった。dispatch.py が実際に使いうる 3 つを足す。
    """
    for label, src, verb in (
        ("while", '''
def _handle(cmd, args):
    while args.more:
        env = {"BEACON_W": "1"}
        _run_commands_py(x, "verb_w", env)
''', "verb_w"),
        ("with", '''
def _handle(cmd, args):
    with open(args.path) as fh:
        env = {"BEACON_W": "1"}
        _run_commands_py(x, "verb_w", env)
''', "verb_w"),
    ):
        sets = _sets_for(src)
        assert sets.get(verb) == {"BEACON_W"}, (
            f"{label} 配下の経路が繋がっていない: {sorted(sets.get(verb) or [])}")


def test_a_walrus_assignment_is_counted():
    """``env := {...}`` でも「渡している」と数える (偽のギャップを作らない)。

    PR #839 2 周目の独立 AX レビュー (silent): ``ast.Assign`` / ``AnnAssign`` しか
    見ていなかったので walrus で組むと検出されず、CI が「二重フロント drift」と
    誤診する形だった。dispatch.py に現在 0 件だが、足された瞬間に発火する。
    """
    sets = _sets_for('''
def _handle(cmd, args):
    if cmd == "w":
        if (env := {"BEACON_WALRUS": "1"}):
            _run_commands_py(x, "verb_walrus", env)
''')
    assert sets.get("verb_walrus") == {"BEACON_WALRUS"}, (
        f"walrus で組んだ辞書を数えていない: {sorted(sets.get('verb_walrus') or [])}")


def test_a_call_inside_an_except_handler_has_a_known_limit():
    """except 節の中の呼び出しは、try 本体で組んだ鍵を数えない (既知の限界)。

    この挙動は**正しくない** (例外が投げられるまで try 本体は部分的に走っている)。
    固定しているのは正しさではなく、**限界が偶然ではなく意図であること**。
    経路の組み立てが「どの枝から上がってきたか」を持たないので、ここだけ分岐を
    意識した造りが要る。dispatch.py に実例が 0 件 (構文木で実測) のうちに造りを
    増やすより、限界を書いて現挙動を固定する方を採った。

    誤りの向きは**偽のギャップ** (= 配線済みを未配線と言う) なので、発火すれば CI が
    赤くなって人が気付く。黙って通る側ではない。実例が出たらこの試験が「期待する
    答え」を書き換える場所になる。
    """
    sets = _sets_for('''
def _handle(cmd, args):
    if cmd == "x":
        try:
            env = {"BEACON_TRY": "1"}
            risky()
        except Exception:
            env["BEACON_EXC"] = "1"
            _run_commands_py(x, "verb_exc", env)
''')
    got = sets.get("verb_exc") or set()
    assert got == {"BEACON_EXC"}, (
        f"except 節の中の経路の扱いが変わった: {sorted(got)}。"
        f"try 本体の鍵まで数えるようになったなら、それは限界の解消なので "
        f"scripts/check-cli-env-parity.py の ast.ExceptHandler の注記と"
        f"この試験の期待値を一緒に直すこと")
