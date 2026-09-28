"""ms-176 e-6607 — フェーズ seed 活動の質 (同義重複を除き、文脈を持たせる)。

営業インスタンスの実データで、先方検討中フェーズの seed が「合意確認日を確定」と
「合意の確認を取る」の 2 本に割れていた。どちらを done にすべきか読めない同義重複で、
他の seed も定型 1 行しか持たず「何のための活動か」が分からない。定型だけの一覧は AI も
人も読み飛ばすので、活動一覧の信号対雑音比が落ちる。

ここで固定するのは **文言そのものではなく規則** (seed は per-company の編集可能な config
なので、会社が言い換える自由は残す):

  * 各フェーズの seed に重複・包含関係が無い。
  * 面談以外の seed は「何のための活動か」を括弧で持つ。
  * 面談の「実施」anchor は文言を変えない (kind=meeting が意味を運び、発火源として他所から
    名前で参照される)。

**適用範囲の注意**: これは新規プロジェクトが受け取る seed。既存プロジェクトは自分の
``opportunity_phases`` を project.json に持っているので、この変更では書き換わらない
(per-company config、master=人間)。既存商談の文言を直すには ``beacon phase`` 系で
その会社の funnel を編集する。
"""
from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import sales_entities as se  # noqa: E402

PROGRESS_PHASES = [p for p in se.DEFAULT_OPPORTUNITY_PHASES if not p.get("terminal")]


def _anchors(pdef):
    return se.phase_activity_anchors(pdef)


def test_every_progress_phase_seeds_at_least_two_steps():
    assert PROGRESS_PHASES, "進行フェーズの seed が空"
    for pdef in PROGRESS_PHASES:
        assert len(_anchors(pdef)) >= 2, pdef["name"]


def test_no_synonym_or_containment_duplication_within_a_phase():
    for pdef in PROGRESS_PHASES:
        cores = [a["desc"].split("（")[0] for a in _anchors(pdef)]
        assert len(cores) == len(set(cores)), f"{pdef['name']}: 同一文言の重複"
        for i, a in enumerate(cores):
            for j, b in enumerate(cores):
                if i != j:
                    assert a not in b, f"{pdef['name']}: '{a}' が '{b}' に包含 (重複の疑い)"


def test_non_meeting_seeds_state_their_purpose():
    for pdef in PROGRESS_PHASES:
        for a in _anchors(pdef):
            if a["kind"] == se.ANCHOR_KIND_MEETING:
                continue        # 「実施」は kind が意味を運ぶ
            assert "（" in a["desc"] and a["desc"].rstrip().endswith("）"), \
                f"{pdef['name']}: '{a['desc']}' に目的が無い"


def test_meeting_anchor_wording_stays_stable():
    # 発火源 (前進ゲートの anchor) として他所から名前で参照されるため、
    # 「実施」anchor の文言は目的注記を付けずそのまま保つ。
    meetings = [a["desc"] for pdef in PROGRESS_PHASES for a in _anchors(pdef)
                if a["kind"] == se.ANCHOR_KIND_MEETING]
    assert meetings == ["初回面談を実施", "提案面談を実施"]


def test_the_agreement_phase_has_two_distinct_steps_not_synonyms():
    # 旧 seed の実害そのもの: 「合意確認日を確定」/「合意の確認を取る」。
    pdef = next(p for p in PROGRESS_PHASES if p["name"] == "先方検討中")
    descs = [a["desc"] for a in _anchors(pdef)]
    assert len(descs) == 2
    assert "期限" in descs[0]          # 待ちを無期限にしないための期限合意
    assert "可否" in descs[1]          # 可否を聞いて次フェーズを決める判断
    assert "合意確認日を確定" not in descs and "合意の確認を取る" not in descs


def test_settlement_seed_points_at_the_real_contract_verb():
    # 「締結」だけでは何をすれば済むのか読めない。実在する記録コマンドを名指しする。
    pdef = next(p for p in PROGRESS_PHASES if p["name"] == "合意済み")
    last = _anchors(pdef)[-1]["desc"]
    assert "contract sign" in last
    root = Path(__file__).parent.parent
    assert "cmd_opportunity_contract_sign() {" in \
        (root / "bin" / "lib" / "cmd_opportunity.sh").read_text(encoding="utf-8")


def test_seeding_a_new_deal_creates_the_improved_steps_once():
    data = se.build_sales_project("Seed Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    oid = se.opportunity_add(data, "商談X", account_id="acc-1")
    first = se.instantiate_phase_activities(data, oid)
    assert len(first) == 3
    # 再実行しても増えない (description による冪等)
    assert se.instantiate_phase_activities(data, oid) == []
    descs = [a["description"] for a in se.find_opportunity(data, oid)["activities"]]
    assert all("（" in d or d.endswith("実施") for d in descs)
