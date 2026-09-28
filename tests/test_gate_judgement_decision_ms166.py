"""判断 (gate judgement) decision の settle_gate 溶接 (ms-166 e-6599)。

閉じている穴: 商談のフェーズ判断 — advance / retry / terminal / jump — は「なぜこの
商談がこう動いたか」そのものなのに、decision arm に一件も載っていなかった (opp-4 実データ
で decision 0 件)。本当の判断はゲートの settled 行・活動 status・session log に散っており、
監査も再構成も人間の記憶頼みだった。

溶接位置がこのテストの主題:

* 4 遷移が通る唯一の漏斗は ``sales_entities.settle_gate``。cmd 層に置くと
  ``cmd_opportunity_phase`` → ``jump_transition`` が handler を経由せず抜ける
  (= ms-174 の jump-bypass の再生産)。``test_every_transition_stages_a_judgement`` が
  **4 遷移すべて**を実際に走らせて、1 経路も漏れないことを押さえる。
* 書き込みは保存 seam に遅延させる (``decision_outbox``)。データ層は純粋なまま、
  かつ **保存されなかった判断は decision にならない** (検証述語) ことを押さえる。
"""
from __future__ import annotations

import pytest

# sys.path (lib / scripts / tests) は tests/conftest.py が集約 (ms-142 e-5144)。
import decision_outbox
import sales_entities as se


@pytest.fixture(autouse=True)
def _clean_outbox():
    """各テストは空の outbox から始め、漏れを次のテストへ持ち越さない。"""
    decision_outbox.discard()
    yield
    decision_outbox.discard()


def _deal(phase: str = "提案準備"):
    """1 商談 (+ その初期 open gate) を持つ営業 project と、その商談 id。

    実際の funnel を積んだ ``build_sales_project`` から作る — フェーズ順・決着候補を
    手で捏造すると遷移の分岐が本物と違ってしまう。
    """
    data = se.build_sales_project("Acme Sales", "close deals")
    oid = se.opportunity_add(data, "テスト商談", phase=phase, created_at="T0")
    decision_outbox.discard()   # 起票自体が積んだ分は本テストの対象外
    return data, oid


def _payloads() -> list:
    return [p for p, _v in decision_outbox.staged()]


# --- 4 遷移すべてが判断を積む (漏斗の被覆) ---------------------------------------

def test_every_transition_stages_a_judgement():
    """advance / retry / terminal / jump のどれも判断 decision を積む。

    これが 1 つでも欠けると「配線はあるが実際には動かない」— ms-166 が掃討している
    silent 非機能そのものになる。
    """
    seen = {}

    data, oid = _deal()
    se.advance_transition(data, oid, note="初回面談OK", at="T1", actor="human:a@b.c")
    seen["advance"] = _payloads()
    decision_outbox.discard()

    data, oid = _deal()
    se.retry_transition(data, oid, "2026-11-01", note="返事待ち", at="T1",
                        actor="human:a@b.c")
    seen["retry"] = _payloads()
    decision_outbox.discard()

    data, oid = _deal()
    se.terminal_transition(data, oid, "失注", note="予算見送り", at="T1",
                           actor="human:a@b.c")
    seen["terminal"] = _payloads()
    decision_outbox.discard()

    # jump = 手動フェーズ宣言 (corrective)。handler を経由しない経路の代表。
    data, oid = _deal()
    se.jump_transition(data, oid, "商談準備", note="取り違え訂正", at="T1",
                       actor="human:a@b.c")
    seen["jump"] = _payloads()

    for verb, payloads in seen.items():
        assert len(payloads) == 1, f"{verb} が判断 decision を積んでいない: {payloads}"
        assert payloads[0]["kind"] == decision_outbox.GATE_JUDGEMENT_KIND, verb
        assert payloads[0]["related"]["target_id"] == oid, verb

    # outcome は遷移ごとに別物 (= 全部 "advance" に潰れていない)。
    assert seen["advance"][0]["decision"] == se.GATE_ADVANCE
    assert seen["retry"][0]["decision"] == se.GATE_RETRY
    assert seen["terminal"][0]["decision"] == se.GATE_TERMINAL
    # jump は宣言先が terminal かどうかで outcome が割れる (非 terminal → advance)。
    assert seen["jump"][0]["decision"] == se.GATE_ADVANCE


def test_jump_to_terminal_stages_terminal_outcome():
    data, oid = _deal()
    se.jump_transition(data, oid, "失注", note="先方都合", at="T1", actor="human:a@b.c")
    assert _payloads()[0]["decision"] == se.GATE_TERMINAL


# --- 中身 (target / 理由 / 帰属 / 証拠) -------------------------------------------

def test_payload_carries_target_reason_and_context():
    data, oid = _deal(phase="先方検討中")
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, reason="決裁者と接触",
                   actor="human:a@b.c", at="T1")
    p = _payloads()[0]
    assert p["related"]["target_id"] == oid          # 判断が指す対象
    assert p["rationale"] == "決裁者と接触"           # なぜ
    # 位置情報は context が運ぶ (evidence に自己参照を積まない)。
    assert f"opportunity={oid}" in p["context"]
    assert "phase=先方検討中" in p["context"]
    assert f"gate={gate['id']}" in p["context"]


def test_anchor_is_evidence_and_gate_id_is_not():
    """evidence は「判定を促した実物」(anchor work item) だけ。ゲート自身は積まない。

    ゲート id を evidence に積むのは判断そのものの自己参照で、裏付けの無い決定を
    非空に見せかける旧挙動 (e-5650 で廃止) の再演になる。裏付けが無いなら空のまま
    残すのが監査シグナルとして正しい。
    """
    data, oid = _deal()
    act = se.activity_add(data, oid, "初回面談", deadline="2026-10-01")
    decision_outbox.discard()
    gate = se.current_gate(data, oid)
    se.anchor_gate(data, gate["id"], act, at="T1")
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, actor="human:a@b.c", at="T2")
    p = _payloads()[0]
    assert p["evidence"] == [f"work-item:{act}"]
    assert not any(gate["id"] in e for e in p["evidence"])

    decision_outbox.discard()
    data, oid = _deal()
    gate = se.current_gate(data, oid)               # anchor なし
    se.settle_gate(data, gate["id"], outcome=se.GATE_RETRY, actor="human:a@b.c", at="T2")
    assert _payloads()[0]["evidence"] == []          # 空 = 裏付け無しを隠さない


@pytest.mark.parametrize("actor,session_kind,expected", [
    ("human:a@b.c", "human", "human-delegated"),
    ("human:a@b.c", "",      "AI-proposed-human-chose"),
    ("human",       "",      "AI-proposed-human-chose"),
    ("claude",      "human", "autonomous-AI"),
    ("",            "human", "autonomous-AI"),
])
def test_decided_by_is_derived_from_actor(monkeypatch, actor, session_kind, expected):
    """decided_by は settle actor から導出する (固定文字列ではない)。

    機械 actor / 空は最も audit-critical な ``autonomous-AI`` に倒す。
    """
    monkeypatch.setenv("BEACON_SESSION_KIND", session_kind)
    data, oid = _deal()
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, actor=actor, at="T1")
    assert _payloads()[0]["decided_by"] == expected


def test_decided_by_values_are_in_the_vocabulary():
    from decision_vocab import DECIDED_BY
    for actor in ("human:a@b.c", "human", "claude", ""):
        assert decision_outbox.decided_by_for_actor(actor) in DECIDED_BY


@pytest.mark.parametrize("session_kind", ["human", "", "ai"])
def test_gate_attribution_has_one_source(monkeypatch, session_kind):
    """人間所有のゲート判断の帰属は 1 箇所から出る (ms-166 e-6601 保守性 M-1)。

    この写像はかつて cmd_target / target_completion / decision_outbox の 3 箇所に別コピー
    されており、互いに docstring で「matching ...」と注記し合うだけで共有していなかった。
    decided_by の意味論を変える者が 3 箇所を手で探す必要があり、1 箇所見落とすと判断の
    帰属が capability ごとに静かに割れる。共有 leaf へ集約したので、**同じ入力に対して
    全経路が同じ答えを返す** ことをここで固定する (再び割れたら落ちる)。
    """
    monkeypatch.setenv("BEACON_SESSION_KIND", session_kind)
    import commands_shared
    import cmd_target
    canonical = commands_shared.decided_by_for_gate()
    assert cmd_target._decided_by_for_gate() == canonical
    # decision_outbox は「actor が人間か」を読む層を足しているだけで、人間判定が
    # 立てば共有 leaf と同じ答えに落ちる。
    assert decision_outbox.decided_by_for_actor("human:a@b.c") == canonical


def test_gate_and_review_attribution_stay_asymmetric(monkeypatch):
    """ゲート判断とレビュー採否の写像は **意図的に非対称** — 集約で潰さない。

    ゲートは人間所有なので AI セッションでも「人間が選んだ」側 (AI-proposed-human-chose)、
    レビュー採否は AI 単独判断なので最も監査が要る autonomous-AI に倒す。共有 leaf を
    1 つにまとめる過程でこの非対称を取り違えると、監査上いちばん見たい「人間が見ていない
    AI の判断」が人間承認済みに化ける。
    """
    import commands_shared
    monkeypatch.setenv("BEACON_SESSION_KIND", "")      # AI セッション
    assert commands_shared.decided_by_for_gate() == "AI-proposed-human-chose"
    assert commands_shared.decided_by_for_review() == "autonomous-AI"
    monkeypatch.setenv("BEACON_SESSION_KIND", "human")  # 人間端末
    assert commands_shared.decided_by_for_gate() == "human-delegated"
    assert commands_shared.decided_by_for_review() == "human-delegated"


# --- 保存されなかった判断は decision にならない -----------------------------------

def test_flush_emits_only_gates_that_survived_the_save():
    """検証述語が「保存後の data で本当に settled か」を見る。

    settle した data とは別の (= その変更を含まない) data で flush すると 1 件も出ない。
    これが「判断したことになっているが商談は動いていない」記録を構造的に不可能にする。
    """
    data, oid = _deal()
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, actor="human:a@b.c", at="T1")
    _payload, verify = decision_outbox.staged()[0]

    assert verify(data) is True                      # 保存された世界
    assert verify(_deal()[0]) is False               # ゲートが open のままの世界
    assert verify({}) is False                       # そもそも商談が無い世界


def test_flush_drains_and_is_local_mode_safe(monkeypatch):
    """local mode では 0 件で静かに終わり、待ち行列は必ず空になる (再試行しない)。"""
    import commands_shared
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    data, oid = _deal()
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, actor="human:a@b.c", at="T1")
    assert decision_outbox.flush(data) == 0
    assert decision_outbox.staged() == ()


def test_flush_writes_verified_payloads_in_cloud_mode(monkeypatch):
    import commands_shared
    sent = []

    class _Client:
        def record_decision(self, project_id, payload):
            sent.append((project_id, payload))
            return {"decision_id": "dec-x"}

    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (_Client(), {"project_id": "proj-1"}))
    data, oid = _deal()
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_TERMINAL, reason="失注",
                   actor="human:a@b.c", at="T1")
    assert decision_outbox.flush(data) == 1
    project_id, payload = sent[0]
    assert project_id == "proj-1"
    assert payload["kind"] == decision_outbox.GATE_JUDGEMENT_KIND
    assert payload["decision"] == se.GATE_TERMINAL
    # who は server が token から刻む — 呼び出し側は載せない。
    assert "who" not in payload


def test_flush_failure_never_breaks_the_caller(monkeypatch, caplog):
    """decision 書き込みの失敗は WARNING で可視化して飲む (silent にも fatal にもしない)。"""
    import commands_shared

    class _Boom:
        def record_decision(self, project_id, payload):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (_Boom(), {"project_id": "proj-1"}))
    data, oid = _deal()
    gate = se.current_gate(data, oid)
    se.settle_gate(data, gate["id"], outcome=se.GATE_ADVANCE, actor="human:a@b.c", at="T1")
    with caplog.at_level("WARNING"):
        assert decision_outbox.flush(data) == 0      # 書けていないので 0
    assert any("decision write failed" in r.getMessage() for r in caplog.records)
    assert decision_outbox.staged() == ()            # 再試行せず捨てる


# --- outbox 自体の契約 -------------------------------------------------------------

def test_stage_rejects_a_payload_without_kind_or_decision():
    with pytest.raises(ValueError):
        decision_outbox.stage({"decision": "advance"}, verify=None)
    with pytest.raises(ValueError):
        decision_outbox.stage({"kind": "gate-judgement"}, verify=None)
    assert decision_outbox.staged() == ()


def test_stage_requires_an_explicit_verify_decision():
    """検証述語は省略できない (ms-166 e-6601 AX-3)。

    「保存された判断だけを書く」がこの module の存在意義なので、その保証を docstring の
    お願いに委ねない。省略できると、次に別の seam を足す者が無意識に検証なしで発行でき、
    この module が構造的に不可能にしたと称する病理をそのまま再生産できてしまう。検証を
    付けられない形なら ``verify=None`` を明示的に渡させる (挙動は同じでも、書いた本人が
    一度意識する分だけ構造的)。
    """
    with pytest.raises(TypeError):
        decision_outbox.stage({"kind": "gate-judgement", "decision": "advance"})
    assert decision_outbox.staged() == ()


def test_broken_verify_predicate_drops_the_record(monkeypatch):
    """述語が壊れている = 検証できない → 書かない側に倒す (誤記録より欠落)。"""
    import commands_shared
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)

    def _boom(_data):
        raise KeyError("shape changed")

    decision_outbox.stage({"kind": "gate-judgement", "decision": "advance"}, verify=_boom)
    assert decision_outbox.flush({}) == 0


# --- 開いたゲートが無い遷移も取りこぼさない -----------------------------------------

def test_transition_without_an_open_gate_still_stages_a_judgement():
    """決着済みからの corrective jump は閉じるゲートを持たないが、判断は起きている。

    旧形 (``if gate is not None: settle_gate(...)``) はこの枝で何も記録せず、フェーズ
    だけ黙って動いていた — 「配線はあるが実際には動かない」の同型。funnel helper
    ``settle_current_gate`` が両枝を拾う。
    """
    data, oid = _deal()
    se.terminal_transition(data, oid, "失注", note="予算見送り", at="T1",
                           actor="human:a@b.c")
    assert se.current_gate(data, oid) is None        # 決着 = 開いたゲートは無い
    decision_outbox.discard()

    se.jump_transition(data, oid, "提案準備", note="失注は取り違え", at="T2",
                       actor="human:a@b.c")
    payloads = _payloads()
    assert len(payloads) == 1, "開いたゲートが無い遷移で判断が落ちている"
    assert payloads[0]["related"]["target_id"] == oid
    assert payloads[0]["rationale"] == "失注は取り違え"
    assert "gate=none-open" in payloads[0]["context"]  # 閉じる work item が無かった旨


def test_no_open_gate_judgement_has_no_verify_predicate():
    """閉じるゲートが無い形は post-save 述語を持てない — 付けたふりをしない。

    述語を持てないことを明示する (= 常に False を返す述語を付けて静かに全件落とす、の
    逆) 。flush 自体が保存成功後にしか走らないので、保存されなかった遷移が decision に
    残る経路は依然として無い。
    """
    data, oid = _deal()
    se.terminal_transition(data, oid, "失注", at="T1", actor="human:a@b.c")
    decision_outbox.discard()
    se.jump_transition(data, oid, "提案準備", at="T2", actor="human:a@b.c")
    _payload, verify = decision_outbox.staged()[0]
    assert verify is None


# --- 書いた判断が read 窓に載る (e-5970 の病理を新 kind で再発させない) -------------

def test_gate_judgement_survives_the_decision_read_window():
    """新 kind を足したとき「書けるが list に載らない」を作っていないことを押さえる。

    ms-166 の旗艦バグ (e-5970) がまさにこれ — 永続化は成功しているのに read 窓の都合で
    一件も返らなかった。kind を増やすたびに同じ穴が開きうるので、新 kind でも既定 read
    と ``--kind`` 指定の両方に載ることを固定する。
    """
    import os
    import sys
    server = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "server"))
    if server not in sys.path:
        sys.path.insert(0, server)
    import decision_event as de

    assert decision_outbox.GATE_JUDGEMENT_KIND in de.KNOWN_DECISION_KINDS

    rows = [{"decision_id": f"dec-old-{i:04d}", "kind": "dm-send", "decision": "sent",
             "created_at": f"2026-07-{(i % 27) + 1:02d}T00:00:{i % 60:02d}.000000Z"}
            for i in range(300)]
    rows.append({"decision_id": "dec-gate-1",
                 "kind": decision_outbox.GATE_JUDGEMENT_KIND,
                 "decision": "advance",
                 "created_at": "2026-09-01T10:00:00.000000Z"})

    default_read = de.window_decision_events(rows, limit=100)
    assert any(r["decision_id"] == "dec-gate-1" for r in default_read), \
        "既定 read 窓に gate-judgement が載らない (古い側で truncate されている)"

    by_kind = de.window_decision_events(rows, kind=decision_outbox.GATE_JUDGEMENT_KIND,
                                        limit=100)
    assert [r["decision_id"] for r in by_kind] == ["dec-gate-1"]
