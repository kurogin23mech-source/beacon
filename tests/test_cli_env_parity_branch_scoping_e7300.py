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
