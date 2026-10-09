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
運ぶ」ことが同じ 1 つの関数呼び出しに束ねられ、迂回するには押印層を使わずに
``record["status"] = "done"`` を手書きするしかなくなる (その手書きは e-6894 の
構文木による数え上げが赤くする)。

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


def require_audit_from_env(verb: str, *, reason_env: str = "BEACON_REASON",
                           exempt: str = "") -> str:
    """``require_audit`` の env アダプタ (bash フロントエンドが渡す形を読む)。

    ``bin/beacon`` 系の bash フロントエンドは旗を ``BEACON_REASON`` /
    ``BEACON_ACKNOWLEDGE`` に詰めて python を起動するので、その読み替えだけを行う。
    規則そのものは持たない (= 規則は ``require_audit`` の 1 箇所)。

    ``reason_env`` は理由を運ぶ変数名。既定は ``BEACON_REASON`` だが、営業側の取消
    verb は歴史的に ``BEACON_CANCEL_REASON`` / ``BEACON_COMM_REASON`` を使っている
    (命名の統一は別件)。関門に乗せるためにここで名前だけ差し替えられるようにしてある —
    変数名の不一致を理由に「この verb だけ関門に乗らない」が起きないように。
    ``BEACON_ACKNOWLEDGE`` は全 verb 共通。
    """
    return require_audit(
        verb,
        reason=os.environ.get(reason_env, ""),
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
