"""terminal_gate.py — 終端遷移が監査エントリを運ぶことを、コードの性質にする
(ms-166 e-6893 / e-6894 / e-6895)。

## なぜこのモジュールが在るか

e-6600 までの関門は ``commands_shared._require_reason_or_skip`` を **CLI ハンドラが
手で呼ぶ** 形だった。呼んでいたのは 6 箇所 (``task done`` / ``milestone done・wait・
observe`` / ``activity done・cancel``) だけで、同じ終端状態に届く他の経路 —— 本番 API の
``POST .../done`` / ``PATCH`` status=done、``task update --status done``、``task cancel``
と営業側 4 つの cancel、そして ``mark_done`` / ``stamp_cancel`` を直に呼ぶ新しいコード ——
は素通りした。つまり「終端遷移には理由が要る」は **コードの性質ではなく、6 箇所に手で
書かれた一覧** だった。新しい終端動詞を足す人は関門を継承せず、継承していないことを機械
から知らされない (e-6600 の実装中、docstring に「全 work-item class で同一に強制される」と
書いたが実測すると嘘だった — 独立レビュー 2 体が別々に指摘)。

そこで関門を **終端状態を実際に書く押印層** (``work_model.mark_done`` /
``work_base.stamp_cancel``) に下げる。これで「終端状態に至る」ことと「監査エントリを
運ぶ」ことが同じ 1 つの関数呼び出しに束ねられる。

**ただし押印層は唯一の書き手ではない。** ``record["status"] = "done"`` と手で書く経路が
実測で 10 件在った (面談の取消 / PR の merge・close・reject / マイルストーンの done・取消 /
Operation 配下のタスクの done / 目的達成レビューの却下 / 判断の漏斗 / 過去データの移行)。
手で数えたときは 9 件しか挙がらず、関数名も 1 つ間違えていた —— **数え上げは機械に
やらせないと合わない**。なので主張はこう置く:

  押印層を通る経路は全て監査エントリを運ぶ。通らない経路は 1 件ずつ理由付きで
  ``KNOWN_HANDWRITTEN_TERMINAL`` に数え上げてあり、新しい手書きが増えたら赤くなる。

「迂回は存在しない」とは言わない。緑のガードは信頼されるので、**偽の安全は無ガードより
悪い** (e-6600 で docstring に「全 work-item class で同一に強制される」と書いて撤回した
のと同じ型を、ここで繰り返さないための明記)。

## 3 つの入力の意味 (= 混ぜてはいけない 3 種)

- ``reason``   : 書かれた理由。**値**。
- ``acknowledge``: 「理由なしで良い」という人/AI の **明示的な意思表示**。これも値を
  生む (``ACKNOWLEDGED_REASON`` の定型文が記録に残る) ので、``reason`` との同時指定は
  曖昧 → 拒否する (e-6895: 旧実装は ACKNOWLEDGE を先に見ていたため、
  ``--reason "ちゃんとした理由" --acknowledge`` は書いた理由を黙って捨て、終了コード 0 で
  「理由を書かずに acknowledge した」という定型文だけを残していた。AI は保険で両方付ける
  ので踏みやすい)。
- ``exempt``   : 「この呼び出し元は監査エントリを持ちようがない」という **許可**。値では
  ない。``EXEMPT_CALL_SITES`` に 1 行の理由付きで登録済みのキーだけが通り、その 1 行が
  そのまま記録に残る (= 免除も data に現れるので、黙って外せない)。許可なので ``reason``
  と併用できる (理由があればそれが勝つ)。``acknowledge`` との併用は免除の二重掛けなので
  拒否する。

## 軸の分離 (既存の台帳と混ぜない)

``capability_ledger.COMPLETION_TERMINAL_*`` は **target の完遂** (milestone / opportunity /
operation が「目的を果たした」と宣言され、deliverable と目的達成 decision が発火するか) の
軸を見ている。こちらは **終端遷移が監査エントリを運ぶか** の軸で、対象は target と
work-item の両方、問うているのは発火ではなく入力の有無。2 つは別の問いなので別の宣言を
持つ。片方を他方に畳むと、完遂が発火しない穴と理由が空の穴が同じ violation に潰れて、
どちらが開いているか読めなくなる。
"""

from __future__ import annotations

import ast
import os
import re


# 「理由を書かずに、意図して監査エントリを省いた」ことを記録に残す定型文。
# ms-120 e-3906 の設計: 空文字 ``--reason ""`` は「意図した免除」と「AI が関門を通すために
# 旗を埋めた」の区別が読み取り時に付かないので受け付けず、明示の ``--acknowledge`` だけを
# 免除として認める。この文字列が ``meta.done_reason`` / ``meta.cancel_reason`` に入る。
ACKNOWLEDGED_REASON = "(acknowledged: no detailed reason given)"


#: 終端状態の語彙。``work_model.DONE_STATUS`` / ``work_base.CANCELLED_STATUS`` と同じ値で、
#: ここでは「どの status が監査エントリを要する終端か」という 1 つの問いの答えとして束ねて
#: 宣言する (属性 patch 経路が終端 status を拒否するときに読む集合)。値の重複宣言を避ける
#: ため literal は書かず、各所有 module から取る。
def terminal_statuses() -> frozenset:
    """監査エントリを要する終端 status の集合 (``{"done", "cancelled"}``)。

    値は所有 module (``work_model`` / ``work_base``) から取るので、どちらかが語彙を
    変えてもここが古い literal を持ち続けることはない。関数にしてあるのは、この module が
    ``work_model`` を import すると循環するため (``work_model`` → ``terminal_gate``)。
    """
    import work_base
    import work_model
    return frozenset({work_model.DONE_STATUS, work_base.CANCELLED_STATUS})


class TerminalAuditRequired(ValueError):
    """終端遷移に監査エントリ (理由 / 明示的な免除) が無い。

    ``ValueError`` の派生なので、既に ``ValueError`` を 400 に変換している本番 API の
    ハンドラや ``except ValueError`` で受けている CLI 経路は、追加の配線なしで
    回復可能なエラーとして扱える。
    """


# ---------------------------------------------------------------------------
# 免除台帳 — 監査エントリを持ちようがない呼び出し元を、1 行の理由付きで明記する。
#
# キーは呼び出し元の ``<module>.<function>``。値はそのまま記録 (``meta.done_reason`` /
# ``meta.cancel_reason``) に入る 1 行なので、「なぜ理由が無いのか」が後から data を
# 読むだけで分かる。登録を消し忘れると
# ``test_no_stale_terminal_gate_exemption`` が赤くなる (台帳が嘘に腐るのを防ぐ)。
# ---------------------------------------------------------------------------
#
# TODAY THIS LEDGER IS EMPTY, and that is a measured fact, not an aspiration:
# ``unguarded_terminal_calls`` over ``lib/`` + ``server/`` + ``scripts/`` returns 0
# rows, so every one of the 17 押印 call sites either carries an audit entry or
# forwards the obligation to its caller. The nearest candidate for an exemption —
# ``sales_entities.fold_phase_activities``, which auto-closes activities when a
# phase folds — turned out NOT to need one: it already passes a literal reason
# naming the evidence-based close. An exemption is for a call site that cannot
# have an audit entry at all; "the caller didn't bother to thread one" is not
# that, and belongs upstream.
EXEMPT_CALL_SITES: dict[str, str] = {}


def exemption_reason(exempt: str) -> str:
    """``exempt`` キーに対応する台帳の 1 行を返す。未登録なら ``ValueError``。"""
    try:
        return EXEMPT_CALL_SITES[exempt]
    except KeyError:
        known = ", ".join(sorted(EXEMPT_CALL_SITES)) or "(台帳は空)"
        raise ValueError(
            f"終端遷移の免除キー '{exempt}' は terminal_gate.EXEMPT_CALL_SITES に"
            f"登録されていません。免除するなら「なぜ監査エントリを持ちようがないか」を"
            f"1 行添えて台帳に足してください (黙って外さない)。登録済み: {known}"
        ) from None


def require_audit(verb: str, *, reason: str = "", acknowledge: bool = False,
                  exempt: str = "") -> str:
    """終端遷移が運ぶ監査エントリを 1 つに確定して返す。足りなければ例外。

    終端状態 (``done`` / ``cancelled``) を書く押印層 —— ``work_model.mark_done`` と
    ``work_base.stamp_cancel`` —— が最初に通る唯一の関門。CLI も本番 API も内部の
    composition も、押印する限りここを通る。

    Args:
        verb: エラー文に出す人向けの動詞 (例 ``"task done"`` / ``"activity cancel"``)。
        reason: 書かれた理由。
        acknowledge: 「理由なしで良い」の明示的な意思表示。
        exempt: ``EXEMPT_CALL_SITES`` に登録済みの免除キー。

    Returns:
        記録に残すべき 1 行 (理由そのもの / ``ACKNOWLEDGED_REASON`` / 台帳の免除理由)。

    Raises:
        ValueError: ``reason`` と ``acknowledge`` の同時指定 (e-6895)、``acknowledge``
            と ``exempt`` の同時指定、未登録の免除キー。
        TerminalAuditRequired: 理由も免除も無い。
    """
    reason = (reason or "").strip()
    if reason and acknowledge:
        raise ValueError(
            f"`{verb}` takes --reason OR --acknowledge, not both. Passing both "
            f"is ambiguous: the old implementation read --acknowledge first, so "
            f"your written reason was discarded unread and only the waiver "
            f"boilerplate was recorded — with exit code 0, so nothing said so "
            f"(ms-166 e-6895). Drop whichever one you did not mean."
        )
    if acknowledge and exempt:
        raise ValueError(
            f"`{verb}`: --acknowledge cannot be combined with the exemption key "
            f"'{exempt}'. An exempted call site already has its audit line from "
            f"the ledger and must not also waive."
        )
    if reason:
        return reason
    if acknowledge:
        return ACKNOWLEDGED_REASON
    if exempt:
        return exemption_reason(exempt)
    raise TerminalAuditRequired(
        f"`{verb}` requires an audit entry. Pass --reason \"...\" to "
        f"record why, or --acknowledge to deliberately proceed without a "
        f"written reason. An empty --reason \"\" is no longer accepted "
        f"(it was ambiguous — use --acknowledge to waive on purpose)."
    )


#: 「書かれた理由」を運ぶ env 変数の正準名。全 verb 共通 —— ``BEACON_ACKNOWLEDGE`` が
#: 最初から 1 名だったのと揃える。
#:
#: 名前に ``AUDIT_`` を付けているのは、兄弟 module の ``readonly_gate.REASON_ENV``
#: (= 読み取り専用モードに入っている「理由」= help / surface-probe の env 対応表) と
#: **別概念なのに同名になる** のを避けるため。同じ語で違うものを指す定数が 2 つ在ると、
#: 片方を grep した人がもう片方を読んでしまう。
AUDIT_REASON_ENV = "BEACON_REASON"

#: 旧名 (deprecated alias)。終端 verb ごとに別名を使っていた歴史の残り。
#: ``AUDIT_REASON_ENV`` が空のときだけ順に見る。
#:
#: ms-166 e-6894 (AX + 保守性レビューが独立に同じ指摘 = 合意度 2/2): 当初は
#: ``require_audit_from_env(reason_env=...)`` という可変パラメータで 4 つの別名を
#: 温存した。これは「不一致を直す」のでなく「不一致を関数の正式な形として固定する」
#: 形で、次に終端 verb を足す人が 5 つ目の名前を作る前例になる。パラメータを消して
#: 1 名に寄せ、旧名はここ 1 箇所の移行用 fallback に落とした —— 新しい別名を
#: 「書ける場所」が無くなったのが効き目。
#:
#: 古い bash フロント + 新しい lib の組み合わせは現にありうる (作業フォルダの CLI が
#: main 側の lib を走らせる構成) ので、いきなり切ると理由が落ちる。移行が済んだら
#: この tuple を空にする。
LEGACY_AUDIT_REASON_ENVS = (
    "BEACON_CANCEL_REASON",     # account / opportunity / acquisition の取消
    "BEACON_COMM_REASON",       # communication cancel
    "BEACON_MTG_CANCEL_REASON",  # meeting cancel
)


#: 「監査の旗が **この起動のコマンド行に実際に現れた** か」を運ぶ env。値 (理由の本文) と
#: は別の事実なので別の変数にする。
#:
#: なぜ要るか (ms-166 e-6894, AX finding #1 の修正中に実測で判明): 非終端の動詞
#: (``target work-item list`` / ``acquisition status in_progress``) に監査の旗を渡したら
#: 拒否したいが、「``BEACON_REASON`` が空でない」から「旗が渡された」を推論すると、
#: shell に ``BEACON_REASON`` を export している利用者や、同一プロセス内で前の呼び出しの
#: env が残っている経路で、**旗を渡していない list が拒否される**。値の有無は
#: 「渡されたか」の代わりにならない。フロントだけが「コマンド行に在ったか」を知っている
#: ので、フロントが明示的に立てる。
AUDIT_FLAG_GIVEN_ENV = "BEACON_AUDIT_FLAG_GIVEN"


def audit_flag_was_given() -> bool:
    """監査の旗がこの起動のコマンド行に現れたか (環境に残っている値と区別する)。"""
    return os.environ.get(AUDIT_FLAG_GIVEN_ENV) == "1"


def reason_from_env() -> str:
    """正準名を見て、空なら旧名を順に見る (移行用の 1 箇所)。"""
    v = os.environ.get(AUDIT_REASON_ENV, "")
    if v.strip():
        return v
    for legacy in LEGACY_AUDIT_REASON_ENVS:
        v = os.environ.get(legacy, "")
        if v.strip():
            return v
    return ""


def require_audit_from_env(verb: str, *, exempt: str = "") -> str:
    """``require_audit`` の env アダプタ (bash フロントエンドが渡す形を読む)。

    ``bin/beacon`` 系の bash フロントエンドは旗を ``BEACON_REASON`` /
    ``BEACON_ACKNOWLEDGE`` に詰めて python を起動するので、その読み替えだけを行う。
    規則そのものは持たない (= 規則は ``require_audit`` の 1 箇所)。理由の変数名も
    verb ごとに差し替えられない (= ``AUDIT_REASON_ENV`` 1 名 + 移行用の旧名 fallback)。
    """
    return require_audit(
        verb,
        reason=reason_from_env(),
        acknowledge=os.environ.get("BEACON_ACKNOWLEDGE") == "1",
        exempt=exempt,
    )


# ---------------------------------------------------------------------------
# 構文木による数え上げ (e-6894) — 「どの関数が終端状態を書くか」を手書きの関数名では
# なくコードから導出し、関門に乗っていない経路を集合比較で赤くする。
#
# 宣言するのは **押印層の関数名** (少数で安定。動詞は足すたびに増えるが、終端状態を書く
# 押印層は増えない)。e-6600 の留めは対象を ``cmd_activity_done`` /
# ``cmd_activity_cancel`` と手で書いていたため、関門に乗らない新しい終端動詞が足されても
# 緑のまま何も言わなかった (= 緑の留めが「もう全部閉じている」という誤った確信を与える)。
# ---------------------------------------------------------------------------

#: 終端状態を実際に書く押印層の関数名。これを呼ぶ関数が「終端に到達する経路」。
TERMINAL_STAMP_FUNCTIONS: frozenset = frozenset({"mark_done", "stamp_cancel"})

#: 監査エントリを運んでいると認める keyword 引数。押印層呼び出しのどれかに付いていれば、
#: その関数は義務を上流へ転送している (= 自分で関門を張る必要はない)。
AUDIT_FORWARDING_KEYWORDS: frozenset = frozenset({"reason", "acknowledge", "exempt"})


def _enclosing_function(funcs: list, lineno: int) -> str:
    """``lineno`` を含む最も内側の関数名 (無ければ ``""``)。"""
    best, best_span = "", None
    for name, start, end in funcs:
        if start <= lineno <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _function_index(tree: ast.AST) -> list:
    """``[(name, start_lineno, end_lineno)]`` — 入れ子も含む全関数。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.lineno,
                        getattr(node, "end_lineno", node.lineno)))
    return out


def _called_name(node: ast.Call) -> str:
    """呼び出しの末端名 (``work_model.mark_done(...)`` → ``mark_done``)。"""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def census_terminal_stamp_calls(source: str, module: str) -> list:
    """``source`` 内の押印層呼び出しを数え上げる (e-6894)。

    Returns:
        ``[{"module", "function", "stamp", "lineno", "forwards_audit"}]`` ——
        ``forwards_audit`` はその呼び出しに ``reason`` / ``acknowledge`` / ``exempt``
        のいずれかの keyword が付いていたか。付いていれば義務を上流へ転送しており、
        付いていなければその関数自身が関門を張るか免除台帳に載る必要がある。

    押印層の **定義** 自身 (``def mark_done`` の中の再帰的な名前解決) は数えない:
    関数名で判定するのではなく「呼び出し式があるか」で見るので、定義だけの関数は
    そもそも呼び出しを含まない。
    """
    tree = ast.parse(source)
    funcs = _function_index(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in TERMINAL_STAMP_FUNCTIONS:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        found.append({
            "module": module,
            "function": _enclosing_function(funcs, node.lineno),
            "stamp": name,
            "lineno": node.lineno,
            "forwards_audit": bool(kwargs & AUDIT_FORWARDING_KEYWORDS),
        })
    return found


def unguarded_terminal_calls(sources: dict) -> list:
    """関門も転送も免除も無い押印層呼び出しを返す (e-6894 の集合比較の答え)。

    Args:
        sources: ``{module_name: source_text}``。

    Returns:
        ``census_terminal_stamp_calls`` の行のうち、``forwards_audit`` が偽で、かつ
        その ``<module>.<function>`` が免除台帳に無いもの。空でなければ「終端状態を
        書くのに監査エントリを運ばない経路」が在るということ。
    """
    bad = []
    for module, source in sources.items():
        for hit in census_terminal_stamp_calls(source, module):
            if hit["forwards_audit"]:
                continue
            key = f"{hit['module']}.{hit['function']}"
            if key in EXEMPT_CALL_SITES:
                continue
            bad.append(hit)
    return bad


# ---------------------------------------------------------------------------
# 手書きの終端書き込みの数え上げ (e-6894 の第 2 軸)
#
# 上の ``unguarded_terminal_calls`` は **押印層を呼ぶ** 経路だけを見る。だから
# ``record["status"] = "cancelled"`` と手で書く経路は 1 件も挙がらず、押印層を直した
# だけで「終端に至るには押印層を通るしかない」と言うのは **嘘** になる。実測すると
# そういう手書きが lib/ に 9 件在った (meeting の取消 / PR の merge・close・reject /
# マイルストーンの done・取消 / Operation 配下のタスクの done / 目的達成レビューの却下 /
# 送信バッチの差し替え)。
#
# ``feedback_guard_enumerate_all_writers`` と同型の罠 —— 守りたい状態への書き手を
# 数え上げる前に「1 箇所に寄せた」と名乗ること。緑の留めは「もう全部閉じている」という
# 確信を与えるので、**偽の安全は無ガードより悪い**。そこで第 2 軸として手書きも数え上げ、
# 既知の 9 件は理由付きで台帳に受理し (= ratchet)、**新しい手書きは赤くする**。
# ---------------------------------------------------------------------------

#: 押印層そのもの。ここでの status 代入は「手書きの迂回」ではなく押印の実装。
_STAMP_OWNERS: frozenset = frozenset({
    "work_model.mark_done", "work_base.stamp_cancel",
})

#: 終端 status を名前で持つ module-level 定数 (値が終端 literal のもの)。走査中に
#: ソースから導出するので、ここに名前を書き並べる必要は無い (下の ``_terminal_consts``)。
_TERMINAL_LITERALS: frozenset = frozenset({"done", "cancelled"})

#: 台帳の行が「債務が残っているか」を **機械が読む印**。散文の言い回しとは分離する。
#:
#: 以前は test が散文から「未整理の債務」という 4 文字を grep していた (独立保守性
#: レビュー #788 low)。それだと、分類が何も変わっていないのに文章を読みやすく直した
#: だけでテストが割れ、逆に別の行に同じ語が偶然出れば債務と誤数えする。印を定数にして
#: 名前で参照すれば、文章は自由に書き換えられる。
LEDGER_TAG_DEBT = "[債務]"
LEDGER_TAG_SETTLED = "[成立済み]"

# ratchet 台帳: 終端 status を手書きしている既知の経路を、1 行の理由付きで受理する。
# 直したら **行を消す** (``test_no_stale_handwritten_terminal_rows`` が削除を強制するので、
# 台帳が嘘に腐らない)。新しい行が増えたら checker が FAIL する。
KNOWN_HANDWRITTEN_TERMINAL: dict = {
    "core.milestone_done": (
        "マイルストーンの完遂。CLI の `milestone done` が共有の関門を通しており、"
        "reason を受けて meta に残す。押印層への寄せ替えは target 側の完遂印"
        "(capture_target_completion) と重なるので別途。"),
    "core.milestone_delete": (
        "マイルストーンの取消。CLI の `milestone delete` が reason を要求する。"),
    "core.operation_task_done": (
        "Operation 配下のタスクの done。reason を受けて meta に残すが、work-item の"
        "押印層とは別の格納先 (operations[].entries) を歩いている。"),
    # PR 3 行は「理由は PR 側の記録が持つ」で一律に片付けていたが、実測すると
    # 3 行の状況が違った (独立レビュー #788 finding #1 の指摘を実コードで照合し、
    # 指摘の内訳も訂正した結果)。成立している行と成立していない行を混ぜると、
    # 読み手が「PR 系はまとめて債務」と丸めてしまい、本当に開いている穴が埋もれる。
    # どの行がどちらかは散文でなく ``PR_RATIONALE_CONTRACT`` が機械で測る。
    "core.pr_merge": (
        "PR の取り込みに伴う entry の done。" + LEDGER_TAG_DEBT + ": merge は理由を受け取らず、"
        "かつ approve を経たことを要求しない (core.pr_merge / cmd_pr_merge のどちらにも "
        "review_status の検査が無い) ので、approve を飛ばして merge すると理由はどこにも"
        "残らない。「判断点は approve 側」は運用上の順序であってコードの性質ではない。"),
    "core.pr_close": (
        "PR を取り込まずに閉じる。" + LEDGER_TAG_DEBT + ": 両フロントとも理由の旗を渡さず "
        "(cmd_pr_close が読むのは ENTRY_ID / JSON だけ)、approve も要求しないので、"
        "却下理由を書かずに閉じられる。3 行の中で最も素通りに近い。"),
    "core.pr_reject": (
        "PR の却下。" + LEDGER_TAG_SETTLED + ": cmd_pr_reject が rationale 無しを "
        "exit 1 で拒否し、両フロントが同じ handler に収束する (PR 系に server API 経路は"
        "無い) ので、却下理由は必ず review_status 側に在る。ここでの status 代入は"
        "その従属的な反映。"),
    "sales_entities.settle_gate": (
        "判断 (gate judgement) の漏斗を閉じる。advance / retry / terminal / jump の"
        "全部がここへ収束する **判断族** で、完遂族ではない (混ぜると前進が完遂扱いに"
        "なって壊れる — capability_ledger の警告と同じ線)。監査行は "
        "work_base.record_audit_event が誰・いつ・なぜで持つ。"),
    "sales_entities._migrate_opp_gates": (
        "過去データの遡行補完 (移行処理)。人の操作ではないので、人に書かせる理由が"
        "存在しない。"),
    "transition_approval.append_verdict": (
        "目的達成レビューの却下に伴う entry の取消。却下理由は approval_rationale と"
        "approval_history が持つので、ここでの status 代入は従属的な反映。"),
    "sales_entities.create_send_batch": (
        "再計画で古い送信バッチを差し替えるときの内部的な無効化。人の操作ではなく"
        "再計画の副作用なので、人に書かせる理由が存在しない。"),
}


# ---------------------------------------------------------------------------
# 台帳の PR 行が主張する「理由はどこに在るか」を、散文でなく **測る** (e-6912)
#
# 上の 3 行はもともと「理由は PR 側の記録が持つ」と 1 つの散文で片付けていた。実測
# すると ``pr_reject`` だけが成立していて、``pr_merge`` / ``pr_close`` は理由を受け取る
# 経路すら無かった。**散文は腐る** —— しかも腐っても緑のままなので、次の読み手は台帳を
# 信じて「PR 系はまとめて債務」または「まとめて成立」と丸める。
#
# 同じことが実際に 2 度起きた: (1) 私がこの 3 行を一律「未整理の債務」と書き、
# (2) それを読んだ独立レビューが実コードを当てて「2 行は成立」と訂正したが、その内訳も
# 半分外していた (``pr_merge`` は approve を要求しないので成立していない)。**人が
# 手で読む限り、この種の主張は毎回ずれる。** だから主張を測れる形で宣言し、実装と
# 突き合わせる。
# ---------------------------------------------------------------------------

#: PR のライフサイクル動詞が理由 (rationale) を要求するか、という **測れる主張**。
#: 上の ``KNOWN_HANDWRITTEN_TERMINAL`` の PR 3 行の散文は、この宣言の言い換えでなければ
#: ならない (= 散文だけを直して実装と離れるのを防ぐ)。
#:
#: - ``"required"``: 理由無しを拒否する (= 理由は必ずどこかに在る)
#: - ``"absent"``  : 理由の旗を受け取らない (= 理由はどこにも無い。未整理の債務)
#:
#: 判定は ``pr_rationale_contract_violations`` が ``lib/cmd_pr.py`` の実装と両フロント
#: (bash / python) から導出する。宣言を変えずに実装を変えたら赤くなる。
PR_RATIONALE_CONTRACT: dict = {
    "pr_approve": "required",
    "pr_reject": "required",
    "pr_request_changes": "required",
    "pr_merge": "absent",
    "pr_close": "absent",
}

#: 理由を運ぶ env 変数名 (両フロントが handler へ渡す形)。
_RATIONALE_ENV = "BEACON_RATIONALE"


def _reads_rationale_env(fn: ast.AST) -> bool:
    """関数の中で ``BEACON_RATIONALE`` を env から読んでいるか。

    判定は **呼び出しの実引数に env 名が現れるか** で行う。呼び出し名
    (``os.environ.get`` / ``os.getenv`` / ``environ.get``) で絞ろうとすると、
    ``_called_name`` が末端名 (``get``) しか返さないため、任意の ``dict.get`` と
    区別できず偽陰性になる (実測で approve / reject / request-changes の 3 件を
    取りこぼした)。env 名そのものは他の用途に渡らないので、実引数に出た時点で
    「読んでいる」と断定できる。docstring やコメントは実引数ではないので入らない。
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and arg.value == _RATIONALE_ENV:
                return True
    return False


def _refuses_when_falsy(fn: ast.AST, name: str) -> bool:
    """``if not <name>: ... sys.exit(...)`` の形で拒否しているか。

    「拒否がある」を substring で見ると、 docstring の説明文や別変数の検査で
    false-pass する。``if`` の test が当該名前の否定であることと、その枝の中に
    ``sys.exit`` が在ることの両方を構文木で確かめる。
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == name):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _called_name(inner) in ("sys.exit", "exit"):
                return True
    return False


def pr_rationale_contract_violations(handler_source: str,
                                     frontend_sources: dict) -> list:
    """``PR_RATIONALE_CONTRACT`` の宣言と、実装 + 両フロントを突き合わせる。

    Args:
        handler_source: ``lib/cmd_pr.py`` のソース (handler = 規則の収束点)。
        frontend_sources: ``{表示名: ソース}`` —— 理由の env を渡す側
            (``bin/lib/cmd_pr.sh`` / ``beacon_cli/dispatch.py``)。``"absent"`` と
            宣言した動詞に対して、どのフロントも理由を渡していないことを確かめる
            (片方のフロントだけが渡すと、弱い側ではなく強い側が見落とされる)。

    Returns:
        違反の説明文のリスト (空なら宣言と実装が一致)。
    """
    tree = ast.parse(handler_source)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out = []
    for verb, claim in sorted(PR_RATIONALE_CONTRACT.items()):
        fn = funcs.get("cmd_" + verb)
        if fn is None:
            out.append(f"{verb}: 宣言しているが handler cmd_{verb} が見つからない "
                       f"(rename したなら PR_RATIONALE_CONTRACT も直すこと)")
            continue
        reads = _reads_rationale_env(fn)
        if claim == "required":
            if not reads:
                out.append(f"{verb}: 'required' と宣言しているが "
                           f"{_RATIONALE_ENV} を読んでいない")
            elif not _refuses_when_falsy(fn, "rationale"):
                out.append(f"{verb}: 'required' と宣言しているが、理由が空のときに "
                           f"sys.exit で拒否する枝が無い (受理して進むなら 'absent')")
        elif claim == "absent":
            if reads:
                out.append(f"{verb}: 'absent' と宣言しているが handler が "
                           f"{_RATIONALE_ENV} を読んでいる (理由を受けるようになったなら "
                           f"宣言と台帳の散文を 'required' 側へ直すこと)")
            for label, src in sorted(frontend_sources.items()):
                if _frontend_passes_rationale(src, verb):
                    out.append(f"{verb}: 'absent' と宣言しているが {label} が "
                               f"{_RATIONALE_ENV} を渡している")
        else:
            out.append(f"{verb}: 未知の宣言 '{claim}' (required / absent のみ)")

    # 構文木でも「どの verb に渡しているか」が決まらない起動 (= handler 名を動的に
    # 組む分岐) は、突き合わせを静かに無効化しうる。ただし今在る分岐は
    # `if cmd in ("approve", "reject", "request-changes")` の内側にあり、到達する
    # verb が全て 'required' なので問題にならない。**到達し得る verb が絞れて、
    # そこに 'absent' が居ない時だけ黙る** —— 絞れない時は晒す。
    absent = {v for v, c in PR_RATIONALE_CONTRACT.items() if c == "absent"}
    for label, src in sorted(frontend_sources.items()):
        for lineno, reachable in unresolved_dynamic_rationale_dispatch(src):
            if reachable is not None and not (reachable & absent):
                continue
            scope = ("到達する verb が静的に絞れない"
                     if reachable is None
                     else f"到達する verb に 'absent' 宣言のもの "
                          f"({', '.join(sorted(reachable & absent))}) が含まれる")
            out.append(
                f"{label}:{lineno}: handler 名を動的に組みつつ {_RATIONALE_ENV} を"
                f"渡している。{scope}ので、PR_RATIONALE_CONTRACT の突き合わせが"
                f"この経路を見られない (= 宣言 'absent' の verb が実は理由を"
                f"受け取っていても緑のまま通る)。handler 名を literal に戻すか、"
                f"到達する verb を `if cmd in (...)` で絞ること")
    return out


def _frontend_passes_rationale(source: str, verb: str) -> bool:
    """フロントが ``verb`` の起動時に理由の env を渡しているか。

    python フロント (= ソースが Python として読める) は **構文木で** 見る。bash は
    構文木が無いので、handler 名の手前の env 並びをテキストで見る。

    テキスト窓方式を python にも使っていたが、独立保守性レビュー (#788 medium) が
    実測で穴を指摘した: ``dispatch.py`` には既に ``subcmd = "pr_" + cmd.replace(...)``
    と handler 名を **動的に組む** 起動が在り、そこでは verb の literal が
    ``BEACON_RATIONALE`` の近傍に現れない。merge / close をその共有分岐へ寄せる改修を
    すると、理由を渡しているのに窓方式は「渡していない」と答えて **緑のまま通る**
    (= 偽の安全)。構文木なら dict の鍵を直接見るのでこの形に強い。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _frontend_passes_rationale_textual(source, verb)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _called_name(node) != "_run_commands_py":
            continue
        # 第 2 実引数が handler 名。literal で verb を指しているものだけを突き合わせる
        # (動的に組むものは下の ``unresolved_dynamic_rationale_dispatch`` が別枠で晒す)。
        name_arg = node.args[1] if len(node.args) > 1 else None
        if not (isinstance(name_arg, ast.Constant) and name_arg.value == verb):
            continue
        if _call_env_has_rationale(node):
            return True
    return False


def _call_env_has_rationale(node: ast.Call) -> bool:
    """``_run_commands_py(..., {...})`` の env に理由の鍵が入っているか。"""
    for arg in list(node.args[2:]) + [kw.value for kw in node.keywords]:
        if isinstance(arg, ast.Dict):
            for k in arg.keys:
                if isinstance(k, ast.Constant) and k.value == _RATIONALE_ENV:
                    return True
        elif isinstance(arg, ast.Name):
            # 変数に組んだ env を渡す形。鍵の有無がこの式から読めないので、
            # 同じ関数内で ``env[_RATIONALE_ENV] = ...`` と足す形を拾う。
            return False
    return False


def unresolved_dynamic_rationale_dispatch(source: str) -> list:
    """handler 名を動的に組みつつ理由を渡している起動を ``[(行, 到達 verb 集合 | None)]``。

    構文木でも「どの verb に渡しているか」が決まらない形 (= ``_run_commands_py(root,
    subcmd, {... BEACON_RATIONALE ...})``)。ここに merge / close が合流すると宣言と
    実装の突き合わせが **静かに無効化される** ので、沈黙せず人に見せる
    (独立保守性レビュー #788 medium)。

    到達 verb は囲っている ``if cmd in ("a", "b")`` / ``if cmd == "a"`` から取る
    (``cmd`` の値がそのまま handler 名の一部になる形)。絞れなければ ``None`` を返し、
    呼び出し側に「判定できない」ことを伝える (絞れないことを緑に丸めない)。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # 各 If ノードの「verb 集合」を先に作り、子孫 → 祖先の対応を引けるようにする。
    scopes = []  # (If ノード, verb 集合 | None)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            scopes.append((node, _verb_set_of_test(node.test)))

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) != "_run_commands_py":
            continue
        name_arg = node.args[1] if len(node.args) > 1 else None
        if isinstance(name_arg, ast.Constant):
            continue
        if not _call_env_has_rationale(node):
            continue
        reachable = None
        for if_node, verbs in scopes:
            if verbs is None:
                continue
            if any(n is node for n in ast.walk(if_node)):
                # 最も内側 (= 最小) の集合を採る。
                reachable = verbs if reachable is None else (reachable & verbs)
        # handler 名が接頭辞 + cmd の形なら、接頭辞を付けて宣言のキーに合わせる。
        if reachable is not None:
            prefix = _dynamic_handler_prefix(tree, name_arg)
            if prefix is None:
                reachable = None   # 名前の組み立て方が読めない = 絞れていない
            else:
                reachable = {(prefix + v.replace("-", "_")) for v in reachable}
        out.append((getattr(node, "lineno", 0), reachable))
    return sorted(out, key=lambda t: t[0])


def _verb_set_of_test(test: ast.AST):
    """``cmd == "x"`` / ``cmd in ("x", "y")`` から verb 集合。読めなければ ``None``。

    ``None`` は「この条件からは verb を絞れない」の意。呼び出し側はこれを緑に
    丸めず「絞れていない」として扱う。
    """
    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name):
        return None
    if test.left.id not in ("cmd", "verb", "sub", "subcmd"):
        return None
    out = set()
    for comp in test.comparators:
        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
            out.add(comp.value)
        elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
            for elt in comp.elts:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                    return None     # 1 つでも読めない要素が居たら集合を信用しない
                out.add(elt.value)
        else:
            return None
    return out or None


def _dynamic_handler_prefix(tree: ast.AST, name_arg: ast.AST):
    """``subcmd = "pr_" + cmd...`` の接頭辞 literal。読めなければ ``None``。

    ``None`` は「handler 名の組み立て方が読めない」の意 (= 到達 verb を宣言のキーに
    変換できないので絞れていない扱いにする)。
    """
    if not isinstance(name_arg, ast.Name):
        return None
    target = name_arg.id
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == target for t in node.targets):
            continue
        val = node.value
        if isinstance(val, ast.BinOp) and isinstance(val.op, ast.Add) \
                and isinstance(val.left, ast.Constant) \
                and isinstance(val.left.value, str):
            if found is not None and found != val.left.value:
                return None     # 同名に複数の組み立て方 = 読めない
            found = val.left.value
        else:
            return None         # 足し算以外の組み立て方は読めない
    return found


def _frontend_passes_rationale_textual(source: str, verb: str) -> bool:
    """bash 用: handler 名の手前に現れる env の並びを見る (構文木が無いので)。"""
    for m in re.finditer(re.escape(verb), source):
        # handler 名の手前 400 文字を同じ起動の env 並びとして見る (bash は行継続)。
        window = source[max(0, m.start() - 400):m.start()]
        # 直前に別の handler 起動が挟まっていたら、そこから後ろだけを見る。
        for other in PR_RATIONALE_CONTRACT:
            if other == verb:
                continue
            cut = window.rfind(other)
            if cut != -1:
                window = window[cut + len(other):]
        if _RATIONALE_ENV in window:
            return True
    return False


def _terminal_consts(tree: ast.AST) -> set:
    """module-level の ``NAME = "done"`` / ``NAME = "cancelled"`` の NAME 集合。

    ``MEETING_CANCELLED`` / ``SEND_BATCH_CANCELLED`` のように終端 literal を別名で
    持つ定数を、名前を手で並べずにソースから導出する (手書きの一覧に戻らないため)。
    """
    names = set()
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant)
                and node.value.value in _TERMINAL_LITERALS):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def _assigns_terminal(value: ast.AST, const_names: set) -> bool:
    """``value`` の式の中に終端 status を指す項があるか。

    三項演算子 (``"approved" if ok else "cancelled"``) のように条件で終端に倒れる形も
    拾うため、代入される式の **部分木全体** を見る。
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and node.value in _TERMINAL_LITERALS:
            return True
        if isinstance(node, ast.Name) and node.id in const_names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in (
                "DONE_STATUS", "CANCELLED_STATUS"):
            return True
    return False


def _targets_status_field(targets: list) -> bool:
    """代入先が ``X["status"]`` / ``X.status`` か。"""
    for t in targets:
        if isinstance(t, ast.Subscript):
            sl = t.slice
            if isinstance(sl, ast.Constant) and sl.value == "status":
                return True
        elif isinstance(t, ast.Attribute) and t.attr == "status":
            return True
    return False


def census_handwritten_terminal_writes(source: str, module: str) -> list:
    """``source`` 内で status に終端を手書き代入している箇所を数え上げる (e-6894)。

    Returns:
        ``[{"module", "function", "lineno", "key"}]`` —— ``key`` は
        ``<module>.<function>`` で、``KNOWN_HANDWRITTEN_TERMINAL`` の行と突き合わせる
        ための識別子。押印層そのもの (``_STAMP_OWNERS``) は除く。
    """
    tree = ast.parse(source)
    funcs = _function_index(tree)
    consts = _terminal_consts(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not _targets_status_field(node.targets):
            continue
        if not _assigns_terminal(node.value, consts):
            continue
        fn = _enclosing_function(funcs, node.lineno)
        key = f"{module}.{fn}"
        if key in _STAMP_OWNERS:
            continue
        out.append({"module": module, "function": fn,
                    "lineno": node.lineno, "key": key})
    return out


def unregistered_handwritten_terminal_writes(sources: dict) -> list:
    """台帳に無い手書きの終端書き込みを返す (空であるべき集合)。

    Args:
        sources: ``{module_name: source_text}`` —— ``module_name`` は
            ``"core"`` のような import 名 (``lib/core.py`` ではない)。
    """
    bad = []
    for module, source in sources.items():
        for hit in census_handwritten_terminal_writes(source, module):
            if hit["key"] in KNOWN_HANDWRITTEN_TERMINAL:
                continue
            bad.append(hit)
    return bad


# ---------------------------------------------------------------------------
# 結果 (outcome) を運んでいない押印呼び出しの数え上げ (e-6894 の第 3 軸)
#
# ``outcome`` (= その仕事から何が出たか) は e-6600 で mark_done / stamp_cancel の
# 第一級の引数になったが、実測すると渡しているのは 18 箇所中 3 箇所だけ。結果は任意の
# 項目なので「渡していない = 誤り」ではない。問題は **次の人が真似る見本が 8 割そちら側
# に在る** こと —— 新しい終端動詞を足す人は既存の呼び出しをパターンとして写すので、
# 古い形 (結果を落とす形) に誘導される。
#
# そこで「なぜ結果を運ばないのか」を 1 行ずつ台帳に書かせる (黙って外さない)。これで
# 新しい終端動詞は「結果を運ぶ」か「運ばない理由を書く」のどちらかを選ばされる。
# ---------------------------------------------------------------------------

#: 結果を運ばない押印呼び出しと、その理由。1 関数 1 行 (同じ関数内の複数呼び出しは 1 行)。
#: 結果を運ぶように直したら **行を消す** (stale 検査が削除を強制する)。
KNOWN_NO_OUTCOME: dict = {
    "core.task_delete": (
        "開発タスクの取消。『何も生まなかった』が既定で、途中まで成果が出た取消を"
        "書きたい場合の先例は activity_cancel が作ってある (outcome を受ける)。"),
    "sales_entities.cancel_gate": (
        "判断の漏斗を取り消す内部操作。判断そのものが成立しなかったので結果が無い。"),
    "sales_entities.contract_cancel": (
        "誤起票した契約の取消。契約は締結されなかったので結果が無い。"),
    "sales_entities.opportunity_cancel": (
        "商談の取消。決着したときの結果は フェーズと判断の漏斗が持つので、取消側で"
        "1 行の結果に潰さない。"),
    "sales_entities.account_cancel": (
        "顧客の取消。顧客は『成果を生む単位』ではなく相手の identity なので結果の"
        "次元を持たない。"),
    "sales_entities.communication_cancel": (
        "誤って記録した証跡の取消。証跡の中身がそのまま結果に当たるため、別立ての"
        "1 行を足すと二重になる。"),
    "sales_entities.meeting_cancel": (
        "予定の取消。開催されなかったので結果が無い (開催された面談の結果は議事録を"
        "取り込んだ証跡が持つ)。"),
    "sales_entities.acquisition_cancel": (
        "獲得施策の打ち切り。打ち切り時点までの実績はアタックリストの行が持つ。"),
    "sales_entities.acquisition_set_status": (
        "獲得施策の完了。成果はアタックリストの実績 (接触数 / 転換数) が持つので、"
        "1 行の結果に潰すと粒度が落ちる。"),
    "sales_entities.fold_phase_activities": (
        "フェーズ折り畳み時の自動 close。結果は close の根拠にした証跡"
        "(communication) が持つ。"),
    "target_engine.close_target": (
        "記述子 target の完遂。target の『生み出した価値』は deliverable arm が別次元"
        "として持つので、work-item 用の 1 行 outcome とは別。照合結果は "
        "completion_check の verdict field に入る。"),
    "target_engine.complete_work_item": (
        "記述子 target の work-item の完了。結果は記述子が宣言した field に書く形"
        "(--field) なので、汎用の 1 行 outcome は使わない。"),
    "target_engine.cancel_work_item": (
        "記述子 target の work-item の取消。同上 — やらないと決めた理由は reason が"
        "持ち、結果は存在しない。"),
}


def census_outcome_on_terminal_calls(sources: dict) -> dict:
    """押印呼び出しを「結果を運ぶ / 運ばない」に分けて返す (e-6894)。

    Returns:
        ``{"carries": [key, ...], "omits": [key, ...]}`` —— key は
        ``<module>.<function>``。押印層そのもの (``_STAMP_OWNERS``) は除く。
    """
    carries, omits = set(), set()
    for module, source in sources.items():
        tree = ast.parse(source)
        funcs = _function_index(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node) not in TERMINAL_STAMP_FUNCTIONS:
                continue
            fn = _enclosing_function(funcs, node.lineno)
            key = f"{module}.{fn}"
            if key in _STAMP_OWNERS:
                continue
            if any(k.arg == "outcome" for k in node.keywords):
                carries.add(key)
            else:
                omits.add(key)
    # 同じ関数が両方の形で呼んでいる場合は「運ぶ」側に数える (見本としては十分)。
    omits -= carries
    return {"carries": sorted(carries), "omits": sorted(omits)}


def undeclared_outcome_omissions(sources: dict) -> list:
    """結果を運ばないのに理由が台帳に無い押印呼び出し (空であるべき集合)。"""
    omits = census_outcome_on_terminal_calls(sources)["omits"]
    return [k for k in omits if k not in KNOWN_NO_OUTCOME]
