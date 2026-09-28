"""ms-176 e-6605 — ボール (次に動くのは誰か) を証跡からの導出で読む。

営業インスタンスの実データでは全商談のボールが「自分」に張り付いていた。真因は導出の
欠落ではなく **読み手** で、``derive_ball`` (最新の証跡の向きから導出) は正しく動いていた
のに、コックピットが読む 3 つの面が起票時の静的宣言フィールドをそのまま載せていた:

  * ``beacon opportunity list --json`` (生レコードを dump)
  * ``sales_entities.project_targets`` (status / UI / 運用室 が読む detail)
  * ``sales_entities.overdue_activities`` (活動の期日超過行)

修正は「1 つの規則を両粒度に効かせ、読み手を導出優先に統一する」形にした — 証跡記録の
seam で静的フィールドに焼き付ける案は採らない (焼いた値は証跡の取消 / 綴じ直しで腐り、
導出と焼印の 2 源になる)。ここではその選択を釘付けにする:

1. ``derive_ball(target, linked_id=…)`` が同じ規則で活動粒度も導出する (第2の規則を作らない)。
2. ``effective_ball`` が導出優先・宣言 fallback の唯一の住所である。
3. 上記 3 面すべてが outbound 証跡の後に「相手」を出す (= 退行したら赤くなる behavior ガード)。
4. 証跡の取消 / 綴じ直しでボールが追随する (焼印なら腐るところ)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402
import sales_entities as se  # noqa: E402

SELF, THEIRS = se.BALL_SELF, se.BALL_COUNTERPART


def _deal_with_two_activities():
    data = se.build_sales_project("Ball Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    opp_id = se.opportunity_add(data, "商談X", account_id="acc-1")
    a1 = se.activity_add(data, opp_id, "提案書を送る")
    a2 = se.activity_add(data, opp_id, "会食を設定する")
    return data, opp_id, a1, a2


def _act(data, act_id):
    return se.find_activity(data, act_id)[1]


# ---------------------------------------------------------------------------
# 1 / 2. ひとつの規則・ひとつの住所
# ---------------------------------------------------------------------------

def test_derive_ball_narrows_to_one_work_item_with_the_same_rule():
    data, opp_id, a1, a2 = _deal_with_two_activities()
    opp = se.find_opportunity(data, opp_id)
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")

    # 商談粒度 (絞らない) と活動粒度 (linked_id で絞る) が同じ規則で導出される
    assert se.derive_ball(opp) == THEIRS
    assert se.derive_ball(opp, linked_id=a1) == THEIRS
    # 証跡が無い活動は「不明」 = None (self を既定にしない)
    assert se.derive_ball(opp, linked_id=a2) is None


def test_effective_ball_prefers_evidence_and_falls_back_to_the_declaration():
    data, opp_id, a1, a2 = _deal_with_two_activities()
    opp = se.find_opportunity(data, opp_id)
    # 証跡ゼロ → 宣言 (起票時の既定 = self) が土台
    assert se.effective_ball(opp) == SELF
    assert se.effective_ball(opp, _act(data, a1)) == SELF

    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    assert se.effective_ball(opp) == THEIRS
    assert se.effective_ball(opp, _act(data, a1)) == THEIRS
    # 別の活動は巻き込まれない (活動粒度が混ざらない)
    assert se.effective_ball(opp, _act(data, a2)) == SELF

    se.communication_add(data, a1, "返信あり", direction="inbound", channel="email")
    assert se.effective_ball(opp, _act(data, a1)) == SELF


def test_declaration_cannot_override_the_evidence():
    # 手で宣言しても、証跡が在る限り盤面は導出値を出す (宣言は土台であって上書きでない)。
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    opp = se.find_opportunity(data, opp_id)
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    se.activity_update(data, a1, who_has_the_ball=SELF)
    assert _act(data, a1)["who_has_the_ball"] == SELF      # 宣言は保存される
    assert se.effective_ball(opp, _act(data, a1)) == THEIRS  # 盤面は導出


def test_no_reader_writes_the_ball_back_onto_the_record():
    # 読み手は記録を書き換えない (焼印を作らない = 第2の真実源を作らない)。
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    se.project_targets(data)
    se.overdue_activities(data, "2099-01-01")
    se.opportunities_awaiting_judgement(data, "2099-01-01")
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before


# ---------------------------------------------------------------------------
# 3. 3 つの読み手 — outbound の後に「相手」を出す
# ---------------------------------------------------------------------------

def test_target_projection_surfaces_the_derived_ball():
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    row = next(t for t in se.project_targets(data) if t["id"] == opp_id)
    assert row["detail"]["who_has_the_ball"] == SELF
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    row = next(t for t in se.project_targets(data) if t["id"] == opp_id)
    assert row["detail"]["who_has_the_ball"] == THEIRS


def test_overdue_activities_surface_the_derived_ball_per_activity():
    data, opp_id, a1, a2 = _deal_with_two_activities()
    se.activity_update(data, a1, deadline="2026-01-01")
    se.activity_update(data, a2, deadline="2026-01-01")
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    rows = {r["act_id"]: r["who_has_the_ball"]
            for r in se.overdue_activities(data, "2026-06-01")}
    assert rows[a1] == THEIRS   # 送ったので相手待ち (= 催促の素材)
    assert rows[a2] == SELF     # 何も送っていないので自分のボール


def test_opportunity_list_json_surfaces_the_derived_ball(tmp_path, monkeypatch,
                                                        capsys):
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.delenv("BEACON_OPP_ALL", raising=False)

    commands.cmd_opportunity_list()
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["id"] == opp_id)
    # コックピットが読む JSON。生レコードのままだと起票時の self が出続けていた。
    assert row["who_has_the_ball"] == THEIRS


# ---------------------------------------------------------------------------
# 4. 証跡を直すとボールが追随する (焼印なら腐るところ)
# ---------------------------------------------------------------------------

def test_cancelling_the_evidence_takes_the_ball_back():
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    opp = se.find_opportunity(data, opp_id)
    comm_id = se.communication_add(data, a1, "誤記録", direction="outbound",
                                   channel="email")
    assert se.effective_ball(opp, _act(data, a1)) == THEIRS
    se.communication_cancel(data, comm_id, reason="そんな送信はしていない")
    assert se.effective_ball(opp, _act(data, a1)) == SELF
    assert se.effective_ball(opp) == SELF


def test_refiling_the_evidence_moves_the_ball_with_it():
    data, opp_id, a1, a2 = _deal_with_two_activities()
    opp = se.find_opportunity(data, opp_id)
    comm_id = se.communication_add(data, a1, "提案書を送付", direction="outbound",
                                   channel="email")
    se.communication_retarget(data, comm_id, a2, reason="綴じ先を間違えた")
    assert se.effective_ball(opp, _act(data, a1)) == SELF
    assert se.effective_ball(opp, _act(data, a2)) == THEIRS


def test_activity_update_discloses_when_the_declaration_is_overridden(tmp_path,
                                                                     monkeypatch,
                                                                     capsys):
    data, opp_id, a1, _a2 = _deal_with_two_activities()
    se.communication_add(data, a1, "提案書を送付", direction="outbound", channel="email")
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_ACT_ID", a1)
    monkeypatch.setenv("BEACON_ACTIVITY_BALL", SELF)
    for k in ("BEACON_ACTIVITY_DESC", "BEACON_ACTIVITY_DEADLINE"):
        monkeypatch.delenv(k, raising=False)

    commands.cmd_activity_update()
    out = capsys.readouterr().out
    # 宣言が黙って効かないのではなく、盤面に出る値と直し方を伝える
    assert "導出値" in out and THEIRS in out
    assert "communication cancel" in out and "retarget" in out
    # 独立 AX レビュー (misleading) の 2 点を釘付けにする:
    # (1) 確認行そのものが他の読み手と同じ値 (導出値) を出す — 同じ who_has_the_ball が
    #     1 つの出力内で 2 つの値に見えてはならない。
    assert f"ball={THEIRS}" in out and f"ball={SELF}" not in out
    # (2) 復旧コマンドは実際の comm-id を埋める (プレースホルダのまま出さない)。
    assert "<comm-id>" not in out
    comm_id = se.communications_of(se.find_opportunity(data, opp_id),
                                   linked_id=a1, include_cancelled=False)[-1]["id"]
    assert f"communication cancel {comm_id}" in out
