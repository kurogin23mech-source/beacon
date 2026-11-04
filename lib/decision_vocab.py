#!/usr/bin/env python3
"""decision_vocab.py — the single source of the decision-arm vocabulary (ms-154 e-5652).

The ``decided_by`` enum used to be defined twice — ``server/decision_event.py``
(``DECIDED_BY``, the schema authority) and ``lib/cmd_decision.py`` (``_DECIDED_BY``,
the CLI's early-reject copy) — kept in sync only by a hand comment. That is a
silent-drift hazard (§2 SSoT / PR#676 maintainability review): adding a fifth
attribution to one side and forgetting the other splits the vocabulary.

This leaf module is the shared source both sides import. It lives in ``lib/``
because ``lib/`` is on the path of every runtime that needs it:
- the CLI runs from ``lib/`` natively;
- the server adds ``lib/`` to ``sys.path`` at startup (``server/app.py``) and already
  imports lib leaves (``core`` / ``work_model``);
- tests get ``lib/`` on the path via ``tests/conftest.py``.

Pure data, stdlib-only — no imports, so it can be a leaf for both the CLI and the
server without introducing a dependency cycle.
"""
from __future__ import annotations


# decided_by (= 誰が決めたか) の一級 enum (ms-154 §設計方針1 / AC1)。
# autonomous-AI が最も audit-critical (= 人間が見ていない判断こそ検分が要る)。
DECIDED_BY: frozenset[str] = frozenset(
    {
        "autonomous-AI",            # 人間未確認の AI 単独決定 (最も audit-critical)
        "AI-proposed-human-chose",  # AI が選択肢を提示し人間が選んだ
        "human-delegated",          # 人間が AI に判断を委譲した
        "programmatic",             # コードが機械的に決めた (= AI 判断ですらない)
    }
)


# ではない = 未知の kind も build_decision_event は受け付ける)。新経路を足したらここに
# 文書化する。ms-90 の 5 経路 + ms-154 decision arm の捕獲対象。
KNOWN_DECISION_KINDS: frozenset[str] = frozenset(
    {
        # ms-90 Trek 由来の経路。ms-185 e-7070 で "trek-review" / "halt" /
        # "resume" の 3 つを外した: 判断の継ぎ目 (leader のレビュー / 綱を引く /
        # 戻す) と builder ごと消えたので、**産出できない種別**になった。
        # この集合は拒否の許可リストではなく「見覚えのある種別」の台帳であり、
        # 捕獲被覆の検査 (= 既知の種別すべてに producer が在るか) の母集団でもある。
        # 産出者の無い種別を残すと、その検査が永久に赤くなる。
        # 読み出しへの影響なし: 種別は開いた語彙 (任意の文字列を書ける) で、
        # 一覧も絞り込みもこの集合で検証していない。本番の判断記録 1,000 件を
        # 実測して 3 種別とも 0 件であることも確認済み (e-7070)。
        "dm-send", "scope-approval",
        # ms-154 decision arm の捕獲対象 (e-5592 / e-5593 / e-5594)
        "task-done", "completion-verdict", "review-adjudication", "log-backstop",
        # ms-166 e-6599: 判断 (gate judgement) = 対象を次のどの状態へ倒すか の決定。
        # 完遂 (completion-verdict) とは別族 — advance / retry は完遂ではない。
        "gate-judgement",
        # ms-166 e-6756: 採否 (disposition) = 達成主張を通す前に、未着手の重要タスクを
        # どう処分したか (done / superseded / blocks-attainment) の判定。
        # ``cmd_target._record_disposition_decision`` が e-5651 から書いていたのに、
        # 「新経路を足したらここに文書化する」という上の規律が守られておらず、本番に
        # 1000 件中 86 件あるのに既知 kind として載っていなかった (= 捕獲台帳の被覆
        # 検査が disposition を素通りし、配線が外れても checker が緑のままだった)。
        "disposition",
    }
)
