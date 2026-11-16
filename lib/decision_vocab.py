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
        # この集合は拒否の許可リストではなく「いま seam が産出する種別」の台帳であり、
        # 捕獲被覆の検査 (= 既知の種別すべてに producer が在るか) の母集団でもある。
        # 産出者の無い種別を残すと、その検査が永久に赤くなる。
        #
        # 読み出しへの影響は **あった** (e-7320 / 訂正記録 dec-25283a3f53a53838)。
        # 書き込みと一覧は確かにこの集合で検証していないが、``beacon decision list
        # --kind`` の開示経路 (lib/cmd_decision._recognized_decision_kinds) がこの
        # 集合を読んでおり、外した 3 種別を引くと「綴り違いの可能性」と警告していた
        # ——綴りは正しいのだから、警告そのものが嘘になる。引退した種別は下の
        # RETIRED_DECISION_KINDS に移して読み手だけが解決できる形にしてある。
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


# 引退した種別 (ms-185 e-7320)。**読む側だけ**のための辞書。
#
# 上の KNOWN_DECISION_KINDS は「いま seam が産出する種別」の台帳で、捕獲被覆の検査の
# 母集団でもある。産出者の無い種別をそこに残すと検査が永久に赤くなるので、産出を止めた
# 種別はそこから外すのが正しい。だが外した瞬間に、過去の判断記録をその種別で引いた人へ
# 「綴り違いの可能性」と警告が出る — 綴りは正しいので、開示が嘘になる。
#
# そこで **産出する種別** (上) と **過去に産出していた種別** (ここ) を分ける。
# 先例は e-7067 の ``lib/dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS`` ——
# 規則は消したが、過去の監査記録に残る判定理由の説明だけは読めるまま残した形。
# ここも同じで、**定数は置かない** (= 誰もセットしない判定を作らない)。この辞書は
# 産出者を要求しないし、要求されてもいけない: 産出者が戻るなら KNOWN へ戻す話になる。
RETIRED_DECISION_KINDS: dict[str, str] = {
    "trek-review":
        "【引退した種別】共同作業空間 (Trek) の leader が配下の作業を検分した判断。"
        "ms-185 で Trek 本体を落としたため、この判断を書く継ぎ目は無くなりました。",
    "halt":
        "【引退した種別】自律で進んでいる作業の綱を引いて止めた判断。ms-185 で Trek "
        "本体を落としたため、この判断を書く継ぎ目は無くなりました。",
    "resume":
        "【引退した種別】綱を引いて止めた作業を再開させた判断。ms-185 で Trek 本体を "
        "落としたため、この判断を書く継ぎ目は無くなりました。",
}


def explain_retired_decision_kind(kind: str) -> str:
    """``kind`` が引退した種別なら 1 行の説明を返す (無ければ空文字列)。

    呼ぶ側 (``beacon decision list --kind``) は 0 件の理由を言い分けるために使う:
    「綴りが的を外している」のではなく「その種別はもう産出されていない」。
    """
    return RETIRED_DECISION_KINDS.get(kind, "")
