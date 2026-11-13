"""証拠ゼロで承認したことを **後から数えられる** ことの試験 (ms-119 e-7247)。

何が起きていたか
----------------
2026-10-30 に実例を観測した。ms-173 の目的達成レビュー依頼 (進行中→観察中の遷移) が、
添付された証拠が空のまま承認済みになっていた。この機構の狙いは「実装者の自己申告では
なく、文脈ゼロの独立した審査が実コードから作った証拠を人間が見て承認する」ことなので、
証拠ゼロの承認はその狙いを素通りする。同じ日に、証拠を後から付けようとしたら
「証拠は verdict の前に付けるもの」と正しく拒否された —— **拒否する側は動いていて
承認する側が緩い**、という非対称だった。

受入条件 5 項目のうち 4 つは e-4205 で既に入っていた (この file を書く前に 1 つずつ
実測して確認した):

1. 証拠ゼロの承認が承認者に伝わる        → ``EvidenceRequiredError`` + 専用メッセージ
2. 証拠なし承認が記録に残り **数えられる** → **これが欠けていた**
3. 承認を禁止しない                      → ``BEACON_ACK_NO_EVIDENCE=1`` の意識的 bypass
4. 過去を遡行しない                      → 既存記録は触らない
5. 黙って通す状態に戻したら赤くなる試験   → ``test_target_approval_evidence.py``

2 が欠けていた理由は、``approval_gate.no_evidence_ack`` の読み手が **表示の 1 行だけ**
だったこと。記録はされているのに集計する経路が無いので、「証拠なしで承認されたものが
何件あるか」を監査する人が答えられなかった。

この file が留めるもの
----------------------
意識的な bypass が、**追記のみの判断記録に数えられる語で残る** こと。専用の問い合わせ
動詞は増やしていない (判断記録の evidence は既に検索可能なタグの語彙なので、そこに
1 つ載せれば ``beacon decision list`` で数えられる)。
"""

import cmd_target


def _entry(*, ack, evidence=None, review_evidence=None):
    return {"id": "e-1", "type": "transition_approval",
            "meta": {"intent": "完了主張",
                     "evidence": list(evidence or []),
                     "review_evidence": list(review_evidence or []),
                     "approval_gate": {"signal": "user-override",
                                       "no_evidence_ack": ack}}}


def _captured(monkeypatch, entry):
    """``_record_completion_verdict_decision`` が送る payload を捕まえる。"""
    seen = {}

    class _Client:
        pass

    import commands_shared as cs
    import contextlib
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cs, "_get_api_client",
                        lambda: (_Client(), {"project_id": "p"}))
    monkeypatch.setattr(cs, "best_effort_completion_decision",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(cs, "record_completion_decision",
                        lambda client, pid, payload, **kw: seen.update(payload))
    cmd_target._record_completion_verdict_decision(
        "ms-9", "observing", entry, "承認理由")
    return seen


def test_a_no_evidence_approval_is_countable(monkeypatch):
    """意識的な bypass が、数えられる語で判断記録に残ること。"""
    seen = _captured(monkeypatch, _entry(ack=True))
    assert cmd_target.NO_REVIEW_EVIDENCE_ACK_TAG in (seen.get("evidence") or []), (
        "証拠ゼロの承認が数えられる形で残っていない: " + str(seen.get("evidence")))
    # 読む人にも伝わること (タグだけだと機械しか気づけない)
    assert "証拠ゼロ" in (seen.get("rationale") or ""), seen.get("rationale")


def test_an_evidenced_approval_does_not_carry_the_tag(monkeypatch):
    """証拠つきの承認にはタグが付かないこと (= 数が水増しされない)。

    境界を測る: 全件に付けてしまうと「何件あるか」の答えが常に総数になり、
    数えられること自体が意味を失う。
    """
    seen = _captured(monkeypatch, _entry(
        ack=False, evidence=["commit:abc1234"],
        review_evidence=[{"source": "independent-judge:fable",
                          "verdict": "attained", "summary": "全 AC 達成"}]))
    ev = seen.get("evidence") or []
    assert cmd_target.NO_REVIEW_EVIDENCE_ACK_TAG not in ev, ev
    assert "commit:abc1234" in ev and "review:independent-judge:fable" in ev, ev


def test_the_tag_does_not_replace_the_real_evidence(monkeypatch):
    """bypass でも、在る証拠は落とさないこと。

    「証拠ゼロ」は独立レビューの証拠が無いことを指す。実装者が出した refs は
    別の軸なので、タグを足すついでに消してはならない。
    """
    seen = _captured(monkeypatch, _entry(ack=True, evidence=["commit:abc1234"]))
    ev = seen.get("evidence") or []
    assert "commit:abc1234" in ev, ev
    assert cmd_target.NO_REVIEW_EVIDENCE_ACK_TAG in ev, ev


# ===========================================================================
# e-7029: 承認の注記が override の種類を見ること (同じ出力の中で矛盾しない)
# ===========================================================================

def test_the_optin_path_is_not_called_an_ai_approval():
    """``user-override`` を「AI が承認した」と断定しないこと (ms-119 e-7029)。

    実例 (2026-10-15, profile-extractor の ms-43)::

        gate: user-override (session_kind=unset, evidence=independent-judge:fable)
        ⚠ AI セッションが override で承認しました — この遷移は監査対象です。

    1 行目が ``user-override`` (= 人間が明示的に opt-in するための経路) と正しく
    ラベル付けしているのに、2 行目が同じ出力の中でそれと矛盾する。Claude Code の
    ``!`` 経路 (人間がターミナルから直接打つ正規の経路) では ``session_kind`` が
    宣言されないまま ``unset`` で残るので、**人間の正当な承認でも必ずこの警告が
    出ていた**。警告が常に出るなら監査する側は区別が付かず、警告としての情報量が
    実質ゼロになる。
    """
    notice = "\n".join(cmd_target.format_approval_gate_notice(
        {"signal": "user-override", "session_kind": "unset"}))
    assert "AI セッションが override で承認" not in notice, notice
    assert "断定できません" in notice, "断定できないことを言っていない:\n" + notice
    assert "opt-in" in notice, notice


def test_a_declared_human_approval_gets_no_notice():
    """人間が宣言して opt-in したときは注記を出さないこと (gate 行で足りる)。"""
    assert cmd_target.format_approval_gate_notice(
        {"signal": "user-override", "session_kind": "human"}) == []
    assert cmd_target.format_approval_gate_notice(
        {"signal": "human-session", "session_kind": "human"}) == []


def test_slipping_past_the_guard_still_warns_loudly():
    """guard をすり抜けた形には **強い警告** を残すこと。

    e-7029 の修正は「警告を弱める」ことではない。本当に怪しい形
    (``ai-session-unguarded``) を、人間の正当な opt-in と区別して際立たせること。
    ここを弱めると e-4006 の狙い (AI の自己承認を隠せなくする) が消える。
    """
    notice = "\n".join(cmd_target.format_approval_gate_notice(
        {"signal": "ai-session-unguarded", "session_kind": "unset"}))
    assert "⚠" in notice and "監査対象" in notice, notice
    assert "すり抜け" in notice, notice
