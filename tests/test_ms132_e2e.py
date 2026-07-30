"""ms-132 端から端まで (e-4507): インサイドセールスの一連フローを 1 本で通す。

施策(Acquisition) → アタックリスト(table-doc)紐づけ → 未接触Account一括登録 →
一括連絡(計画→人間confirm→送信記録=連絡済) → 返信検知(→返信あり) → リード転換
(→商談 + Account phase リード)。加えて e-4507 のライフサイクル整備(observing 除去 /
打ち切りは削除)を検証する。in-process(commands.cmd_* + BEACON_PROJECT_FILE)で駆動。
"""

from __future__ import annotations

import hashlib
import io
import json
import contextlib
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402
import core  # noqa: E402
import sales_entities as se  # noqa: E402


def _read(cwd):
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


def _write(cwd, data):
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


ALL_ENV = ("BEACON_DOC_ID", "BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE",
           "BEACON_ACQ_LIST_PHASES", "BEACON_ACQ_STATUS", "BEACON_CANCEL_REASON",
           "BEACON_SEND_SUBJECT", "BEACON_SEND_MESSAGE", "BEACON_SEND_FROM_PHASE",
           "BEACON_SEND_LIMIT", "BEACON_CONFIRM", "BEACON_SEND_ACC_ID",
           "BEACON_SEND_MESSAGE_ID", "BEACON_OPP_TITLE", "BEACON_JSON",
           "BEACON_FILL_PHASE", "BEACON_COMM_SUMMARY")


def _run(monkeypatch, fn, capsys, **env):
    for k in ALL_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("BEACON_JSON", "1")
    fn()
    out = capsys.readouterr().out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


@pytest.fixture
def proj(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    data = se.build_sales_project("E2E", "close")
    se.acquisition_add(data, "獲得A")  # acq-1
    for name in ("未A", "未B"):
        aid = se.account_add(data, name, phase="未接触")
        se.find_account(data, aid)["contacts"] = [
            {"name": name, "email": f"{aid}@ex.com"}]
    _write(cwd, data)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def test_full_funnel_end_to_end(proj, monkeypatch, capsys):
    cwd = proj
    subj, msg = "初回のご案内", "はじめまして、ご提案があります。"

    # 1. attach an attack-list to the施策
    doc_id = _run(monkeypatch, commands.cmd_acquisition_attach_list, capsys,
                  BEACON_ACQ_ID="acq-1", BEACON_ACQ_LIST_TITLE="攻略")["doc_id"]

    # 2. bulk-register the untouched Accounts as rows (未接触)
    fill = _run(monkeypatch, commands.cmd_acquisition_attack_list_fill, capsys,
                BEACON_DOC_ID=doc_id)
    assert set(fill["target_ids"]) == {"acc-1", "acc-2"}

    # 3. bulk outreach: plan (dry-run) → human confirm → send-record per recipient
    plan = _run(monkeypatch, commands.cmd_acquisition_attack_list_send, capsys,
                BEACON_DOC_ID=doc_id, BEACON_SEND_SUBJECT=subj, BEACON_SEND_MESSAGE=msg)
    assert plan["status"] == "pending" and plan["recipient_count"] == 2
    _run(monkeypatch, commands.cmd_acquisition_attack_list_send, capsys,
         BEACON_DOC_ID=doc_id, BEACON_CONFIRM="1")  # authorize (human)
    for acc in ("acc-1", "acc-2"):
        rec = _run(monkeypatch, commands.cmd_acquisition_attack_list_send_record, capsys,
                   BEACON_DOC_ID=doc_id, BEACON_SEND_ACC_ID=acc,
                   BEACON_SEND_MESSAGE_ID=f"sent-{acc}", BEACON_SEND_SUBJECT=subj,
                   BEACON_SEND_MESSAGE=msg)
        assert rec["phase_driven_to"] == "連絡済"

    # 4. one prospect replies → row 連絡済→返信あり + inbound 証跡
    rep = _run(monkeypatch, commands.cmd_acquisition_attack_list_reply_record, capsys,
               BEACON_DOC_ID=doc_id, BEACON_SEND_ACC_ID="acc-1",
               BEACON_SEND_MESSAGE_ID="reply-1", BEACON_COMM_SUMMARY="興味あります")
    assert rep["phase_driven_to"] == "返信あり"

    # 5. lead conversion: the reacted prospect → Opportunity + Account phase リード
    promo = _run(monkeypatch, commands.cmd_acquisition_attack_list_promote, capsys,
                 BEACON_DOC_ID=doc_id, BEACON_SEND_ACC_ID="acc-1")
    assert promo["opportunity"].startswith("opp-")
    assert promo["account_phase"] == "リード"

    # ---- verify the whole ledger ----
    data = _read(cwd)
    acc1 = se.find_account(data, "acc-1")
    dirs = sorted(c["direction"] for c in acc1.get("communications", []))
    assert dirs == ["inbound", "outbound"]           # 証跡: 送信 + 返信
    assert acc1["phase"] == "リード"                  # Account lifted to lead
    assert any(o["account_id"] == "acc-1" for o in data["opportunities"])  # deal exists
    # list state: acc-1 → 返信あり, acc-2 → 連絡済
    lists = _run(monkeypatch, commands.cmd_acquisition_lists, capsys,
                 BEACON_ACQ_ID="acq-1")["lists"]
    assert lists[0]["phase_counts"] == {"返信あり": 1, "連絡済": 1}


def test_acquisition_lifecycle_no_observing(proj, monkeypatch, capsys):
    cwd = proj
    # observing is no longer a valid Acquisition status (e-4507)
    assert "observing" not in se.ACQUISITION_STATUSES
    data = _read(cwd)
    with pytest.raises(ValueError):
        se.acquisition_set_status(data, "acq-1", "observing")
    # todo → in_progress → done flows
    se.acquisition_set_status(data, "acq-1", "in_progress")
    se.acquisition_set_status(data, "acq-1", "done")
    assert se.find_acquisition(data, "acq-1")["status"] == "done"


def test_acquisition_discontinue_is_delete(proj, monkeypatch, capsys):
    # 打ち切り = soft-cancel (delete), not a lifecycle status. Tombstoned, audit kept.
    _run(monkeypatch, commands.cmd_acquisition_delete, capsys,
         BEACON_ACQ_ID="acq-1", BEACON_CANCEL_REASON="方針変更で中止")
    acq = se.find_acquisition(_read(cwd_of(monkeypatch)), "acq-1")
    assert acq["status"] == core.CANCELLED_STATUS if hasattr(core, "CANCELLED_STATUS") \
        else acq["status"] == "cancelled"
    assert acq["meta"]["cancel_reason"] == "方針変更で中止"


def cwd_of(monkeypatch):
    import os
    return Path(os.getcwd())
