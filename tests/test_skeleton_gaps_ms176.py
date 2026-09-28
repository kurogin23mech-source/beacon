"""ms-176 e-6606 — 商談の骨格 (想定金額 / 期日) を業務イベントに寄生させて埋める。

想定金額・期日が空のまま残る。「埋めましょう」と独立に促す運動は形骸化するので、**どうせ
通る業務イベント** (見積が固まって金額を入れた / 面談を確定した) の中で、まだ空いている
骨格を 1 度だけ差し出す。強度は警告 + 明示スキップ (hard block しない) — 記録の鮮度は後から
直せる可逆な問題で、既存の permissive な警告 (opportunity_phase_warnings) と同じ系列に置く。
不可逆な外部発行だけを hard gate にする (送信台帳 = e-6608) 非対称が方針3 / 4 の骨。

併せて、期日には **後から入れる経路が存在しなかった** (起票時の `opportunity add --deadline`
だけ) ことを直した。促しが存在しないコマンドを案内していては意味が無いので、`beacon
opportunity deadline` を両フロント (bash / Python dispatch) に足してある。
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


def _deal(**kwargs):
    data = se.build_sales_project("Skeleton Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    opp_id = se.opportunity_add(data, "商談X", account_id="acc-1", **kwargs)
    return data, opp_id


def _opp(data, opp_id):
    return se.find_opportunity(data, opp_id)


# ---------------------------------------------------------------------------
# 検知 — 「空だと何が壊れるか」を持つフィールドだけを挙げる
# ---------------------------------------------------------------------------

def test_both_skeleton_fields_are_reported_when_empty():
    data, opp_id = _deal()
    gaps = se.skeleton_field_gaps(data, _opp(data, opp_id))
    assert [g["field"] for g in gaps] == ["goal_amount", "deadline"]
    for g in gaps:
        assert g["why"] and g["how"]          # 理由と直し方を必ず持つ


def test_filled_fields_drop_out_one_by_one():
    data, opp_id = _deal(goal_amount=3_000_000)
    assert [g["field"] for g in se.skeleton_field_gaps(data, _opp(data, opp_id))] \
        == ["deadline"]
    se.set_opportunity_deadline(data, opp_id, "2026-12-31")
    assert se.skeleton_field_gaps(data, _opp(data, opp_id)) == []


def test_probability_is_deliberately_not_nagged_about():
    # 見込み売上は 金額 × **フェーズの** 成約率 で積むので、商談ごとの probability が
    # 空でも積み上げは壊れない。壊れないフィールドを促すと促し全体が読み飛ばされる。
    data, opp_id = _deal(goal_amount=1, deadline="2026-12-31")
    opp = _opp(data, opp_id)
    assert opp.get("probability") in (None, "")
    assert se.skeleton_field_gaps(data, opp) == []


def test_detection_is_pure_read():
    data, opp_id = _deal()
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    se.skeleton_field_gaps(data, _opp(data, opp_id))
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before


def test_format_is_empty_when_nothing_is_missing():
    assert se.format_skeleton_gap_echo([], event="面談を確定しました") == ""


def test_format_names_the_event_and_says_it_is_optional():
    data, opp_id = _deal()
    band = se.format_skeleton_gap_echo(
        se.skeleton_field_gaps(data, _opp(data, opp_id)),
        event="面談を確定しました")
    assert "面談を確定しました" in band          # 業務の流れの一部として読める
    assert "任意" in band                        # 明示スキップ可 (hard block でない)
    assert f"beacon opportunity amount {opp_id}" in band
    assert f"beacon opportunity deadline {opp_id}" in band


# ---------------------------------------------------------------------------
# 期日の後追い設定 — 促しが案内するコマンドが実在すること
# ---------------------------------------------------------------------------

def test_deadline_can_be_set_and_cleared_after_creation():
    data, opp_id = _deal()
    se.set_opportunity_deadline(data, opp_id, "2026-12-31")
    assert _opp(data, opp_id)["deadline"] == "2026-12-31"
    se.set_opportunity_deadline(data, opp_id, "")
    assert _opp(data, opp_id)["deadline"] == ""


def test_deadline_rejects_a_non_iso_date():
    data, opp_id = _deal()
    with pytest.raises(ValueError):
        se.set_opportunity_deadline(data, opp_id, "2026/12/31")


def test_deadline_is_not_the_gates_transition_date():
    # 前進ゲートの遷移日 (= 判定予定日) と商談の期日は別物。片方を入れても
    # もう片方は埋まらない (促しが「もう入っている」と誤判定しないこと)。
    data, opp_id = _deal(goal_amount=1)
    se.set_transition_date(data, opp_id, "2026-11-01")
    assert [g["field"] for g in se.skeleton_field_gaps(data, _opp(data, opp_id))] \
        == ["deadline"]


# ---------------------------------------------------------------------------
# 業務イベントへの寄生 (CLI)
# ---------------------------------------------------------------------------

def _cwd_with(data, tmp_path, monkeypatch, name="proj"):
    cwd = tmp_path / name
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def test_setting_the_amount_offers_the_remaining_gap(tmp_path, monkeypatch, capsys):
    data, opp_id = _deal()
    _cwd_with(data, tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_OPP_ID", opp_id)
    monkeypatch.setenv("BEACON_OPP_AMOUNT", "3000000")

    commands.cmd_opportunity_amount()
    out = capsys.readouterr().out
    assert "Set amount" in out                       # 本題は通る
    assert "期日 が未設定" in out                     # 残りの骨格を差し出す
    assert "想定金額 が未設定" not in out             # いま埋めたものは promote しない


def test_confirming_a_meeting_offers_the_gaps(tmp_path, monkeypatch, capsys):
    data, opp_id = _deal()
    _cwd_with(data, tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_MTG_OPP", opp_id)
    monkeypatch.setenv("BEACON_MTG_AT", "2026-11-05T10:00:00+09:00")
    for k in ("BEACON_MTG_END", "BEACON_MTG_LOCATION", "BEACON_MTG_EVENT_ID",
              "BEACON_MTG_CAL_NS", "BEACON_MTG_CAL_ACCT", "BEACON_MTG_SET_TRANSITION"):
        monkeypatch.delenv(k, raising=False)

    commands.cmd_meeting_schedule()
    out = capsys.readouterr().out
    assert "Scheduled meeting" in out                # 面談確定そのものは完了する
    assert "想定金額 が未設定" in out and "期日 が未設定" in out
    assert "任意" in out                             # 埋めずに進める


def test_no_echo_when_the_skeleton_is_complete(tmp_path, monkeypatch, capsys):
    data, opp_id = _deal(goal_amount=3_000_000, deadline="2026-12-31")
    _cwd_with(data, tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_MTG_OPP", opp_id)
    monkeypatch.setenv("BEACON_MTG_AT", "2026-11-05T10:00:00+09:00")
    for k in ("BEACON_MTG_END", "BEACON_MTG_LOCATION", "BEACON_MTG_EVENT_ID",
              "BEACON_MTG_CAL_NS", "BEACON_MTG_CAL_ACCT", "BEACON_MTG_SET_TRANSITION"):
        monkeypatch.delenv(k, raising=False)

    commands.cmd_meeting_schedule()
    out = capsys.readouterr().out
    assert "Scheduled meeting" in out and "未設定" not in out


def test_the_deadline_verb_is_wired_on_both_cli_frontends():
    # 両フロント配線 (bin/beacon の bash 経路と Python dispatch の argparse 経路)。
    # 片方だけだと Windows / pipx 利用者が invalid choice で落ちる (ms-44 e-1171)。
    root = Path(__file__).parent.parent
    assert "deadline) shift 2; cmd_opportunity_deadline" in \
        (root / "bin" / "beacon").read_text(encoding="utf-8")
    assert "cmd_opportunity_deadline()" in \
        (root / "bin" / "lib" / "cmd_opportunity.sh").read_text(encoding="utf-8")
    dispatch_src = (root / "beacon_cli" / "dispatch.py").read_text(encoding="utf-8")
    assert 'opp_sub.add_parser("deadline"' in dispatch_src
    assert '"opportunity_deadline", env' in dispatch_src
    assert "opportunity_deadline" in \
        (LIB / "commands.py").read_text(encoding="utf-8")
