"""target 側のレビュー実施記録が、存在しない関門を名乗らないことの固定
(ms-119 / e-7439)。

**何が起きていた** (独立思想レビュー medium, 2026-11-11): `review done --target`
は記録のあと「完了の関門を通過できます」と印字していたが、**target 側のこの記録を
読む完了 / 承認の関門は存在しない**。docstring も PR 側 (そこでは本当に
`beacon pr approve` を止める) の言葉をそのまま持ってきて「関門を外す」と書いていた。
README が review status の契約を偽って書いていた件 (commit 6c4d9a68) と同じ family
—— 機構が自分の効き目を過大に名乗る形。

**どちらで直したか**: SPEC が思想レビューを「Advisory クラス = 助言・非 blocking」と
宣言しているので、**gate を足すのではなく文面を実態に合わせた** (user 判断、
2026-11-11)。gate を足す側を選ぶと SPEC の方針変更になる。

**この module が測るもの**: 文面の言い回しではなく、**文面が依拠している構造**
—— 「target の review-due 記録を読むのは記録 (review done) と免除 (review skip)
の 2 つだけで、完了 / 承認の経路は誰も読まない」という読み手の集合。新しい読み手が
増えたらここが赤くなり、その人に文面の見直しを促す (散文の不在を文字列で測るのでは
なく、散文が主張している性質そのものを集合で測る)。
"""

import ast
import pathlib

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"

# target の review-due 印を読む / 書き換える関数群。PR 側 (`review_due_filename`
# を使う経路) は別物なので含めない。
_TARGET_REVIEW_DUE_NAMES = {
    "_target_review_due_has_binding",
    "_target_review_due_state",
    "_remove_binding_from_target_review_due",
}

# いま読んでよい唯一の 2 つ。どちらも「レビューの帳簿付け」であって関門ではない。
_EXPECTED_READERS = {
    ("commands.py", "cmd_review_skip"),          # 理由付き免除
    ("commands.py", "_emit_review_done_for_target"),  # 実施の記録
}


def _readers():
    found = set()
    for path in sorted(LIB.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if used & _TARGET_REVIEW_DUE_NAMES:
                found.add((path.name, node.name))
    return found


def test_only_review_bookkeeping_reads_the_target_review_due_mark():
    """**本体**: target の review-due 印の読み手は記録と免除の 2 つだけ。

    `_emit_review_done_for_target` が印字する「{target} の完了 / 承認は この記録を
    読みません」は、この集合が成り立っていて初めて真になる。新しい読み手が増えたら
    (特に完了 / 承認の経路に) ここで止めて、文面の見直しを促す。
    """
    actual = _readers()
    assert actual == _EXPECTED_READERS, (
        "target の review-due 印の読み手が変わりました。\n"
        f"  想定: {sorted(_EXPECTED_READERS)}\n"
        f"  実際: {sorted(actual)}\n"
        "完了 / 承認の経路が読むようになったなら、"
        "`_emit_review_done_for_target` が印字する『完了 / 承認は この記録を"
        "読みません』と docstring の『関門ではない』を実態に合わせて直してください "
        "(ms-119 e-7439: 機構が効き目を過大に名乗る形に戻さない)。")


def test_completion_and_approval_paths_are_not_among_the_readers():
    """判定役が名指しした 3 つの完了 / 承認経路が読み手に居ないことを明示で固定。

    上の集合比較に含まれてはいるが、**この 3 つが居ないこと**が文面の根拠なので、
    名前で直接言っておく (集合が将来広がったとき、何を守っていたのかが残る)。
    """
    reader_names = {fn for _, fn in _readers()}
    for gate in ("cmd_target_approve", "target_transition_approval_approve",
                 "cmd_milestone_done"):
        assert gate not in reader_names, (
            f"{gate} が target の review-due を読むようになりました。"
            "文面 (助言としての記録) が偽になるので、両方を直してください。")
