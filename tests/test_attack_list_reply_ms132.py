"""Reply-watch wiring — ms-132 e-4505.

When a contacted prospect (attack-list row at 連絡済) replies, the row advances
連絡済→返信あり, an INBOUND Communication (証跡) is recorded on the Account, and the
human is notified (a trigger). Detection-only: nothing is sent. These tests lock:
- awaiting-reply lists exactly the 連絡済 rows (the reply-watch worklist);
- reply-record drives 連絡済→返信あり + records inbound 証跡 + fires a trigger;
- a reply on a row NOT at 連絡済 records the 証跡 but does not misadvance the phase;
- an unknown Account is refused.
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
import table_doc  # noqa: E402
import table_type  # noqa: E402


def _write(cwd: Path, data: dict) -> None:
    # ms-148 e-5414: SQLite is the source of truth; a direct project.json write
    # only touches the read-only mirror. Seed through the store.
    from store import get_store
    store = get_store(project_file=str(cwd / ".beacon" / "project.json"))
    if hasattr(store, "apply"):
        store.apply(lambda _cur: (data, None), validate=False)
    else:
        store.save_project(data, validate=False)


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


def _clear(monkeypatch):
    for k in ("BEACON_DOC_ID", "BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE",
              "BEACON_SEND_ACC_ID", "BEACON_SEND_MESSAGE_ID", "BEACON_SEND_URL",
              "BEACON_COMM_SUMMARY", "BEACON_TRIGGER_NAME", "BEACON_TRIGGER_MESSAGE",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def _capture(fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return json.loads(buf.getvalue())


@pytest.fixture
def reply_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    data = se.build_sales_project("Smoke", "close")
    se.acquisition_add(data, "獲得A")  # acq-1
    for name in ("未A", "未B"):
        aid = se.account_add(data, name, phase="未接触")
        se.find_account(data, aid)["contacts"] = [
            {"name": name, "email": f"{aid}@example.com"}]
    _write(cwd, data)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_ACQ_ID", "acq-1")
    monkeypatch.setenv("BEACON_ACQ_LIST_TITLE", "攻略")
    monkeypatch.setenv("BEACON_JSON", "1")
    doc_id = _capture(commands.cmd_acquisition_attach_list)["doc_id"]
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_fill()  # rows for acc-1, acc-2 at 未接触
    return cwd, doc_id


def _set_row_phase(doc_id, acc_id, to_phase):
    content, title, model = commands._load_table_model(doc_id)
    table_type.install()
    for row in table_doc.active_rows(model):
        if row["cells"].get("account") == acc_id:
            table_doc.set_cell(model, row["id"], "phase", to_phase,
                               actor="test", at="T0")
            commands._write_table_model(doc_id, title, content, model)
            return
    raise AssertionError(f"row for {acc_id} not found")


def _reply(monkeypatch, capsys, doc_id, acc_id, mid="rmsg-1", summary="返信本文"):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_SEND_ACC_ID", acc_id)
    monkeypatch.setenv("BEACON_SEND_MESSAGE_ID", mid)
    monkeypatch.setenv("BEACON_COMM_SUMMARY", summary)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_reply_record()
    return json.loads(capsys.readouterr().out)


def test_awaiting_reply_lists_only_contacted(reply_cwd, monkeypatch, capsys):
    cwd, doc_id = reply_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")  # acc-2 stays 未接触
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_awaiting_reply()
    out = json.loads(capsys.readouterr().out)
    assert out["phase"] == "連絡済"
    assert [w["acc_id"] for w in out["awaiting"]] == ["acc-1"]
    assert out["awaiting"][0]["email"] == "acc-1@example.com"


def test_reply_record_drives_phase_and_records_inbound(reply_cwd, monkeypatch, capsys):
    cwd, doc_id = reply_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")
    out = _reply(monkeypatch, capsys, doc_id, "acc-1", mid="rmsg-9")
    assert out["phase_driven_to"] == "返信あり"
    assert out["notified"] is True
    data = _read(cwd)
    comms = se.find_account(data, "acc-1").get("communications", [])
    assert len(comms) == 1
    assert comms[0]["direction"] == "inbound" and comms[0]["channel"] == "email"
    assert comms[0]["source"]["ref"] == "rmsg-9"
    # notification trigger fired (name carries the reply's message-id)
    trig = cwd / ".beacon" / "triggers" / "attack-list-reply-acc-1-rmsg-9.json"
    assert trig.exists()


def test_reply_on_non_contacted_row_records_but_no_misadvance(
        reply_cwd, monkeypatch, capsys):
    cwd, doc_id = reply_cwd
    # acc-1 is still 未接触 (never contacted). A reply is recorded but the phase
    # must NOT jump to 返信あり (no misadvance from the wrong phase).
    out = _reply(monkeypatch, capsys, doc_id, "acc-1")
    assert out["phase_driven_to"] is None
    assert out["phase_guard_skipped"] is True  # explicit, not a silent null
    comms = se.find_account(_read(cwd), "acc-1").get("communications", [])
    assert len(comms) == 1 and comms[0]["direction"] == "inbound"


def test_notified_reflects_dedup_on_same_reply(reply_cwd, monkeypatch, capsys):
    # A repeat reply-record for the same acc + message-id must report notified
    # false (the trigger already exists) rather than a false-positive true.
    cwd, doc_id = reply_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")
    first = _reply(monkeypatch, capsys, doc_id, "acc-1", mid="dup-1")
    assert first["notified"] is True
    second = _reply(monkeypatch, capsys, doc_id, "acc-1", mid="dup-1")
    assert second["notified"] is False  # deduped, honestly reported


def _row_cells(doc_id, acc_id):
    _c, _t, model = commands._load_table_model(doc_id)
    for r in table_doc.active_rows(model):
        if r["cells"].get("account") == acc_id:
            return r["cells"]
    raise AssertionError(f"row for {acc_id} not found")


def test_reply_record_stamps_last_contact(reply_cwd, monkeypatch, capsys):
    # e-4623: reply-record fills 最終接触日 (= 返信日) on the SAME write as the phase
    # drive — 打診フェーズ and 最終接触日 commit together (no partial write).
    cwd, doc_id = reply_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")
    _reply(monkeypatch, capsys, doc_id, "acc-1", mid="r1")
    cells = _row_cells(doc_id, "acc-1")
    assert cells["last_contact"] == commands._now_iso()[:10]
    assert cells["phase"] == "返信あり"


def test_reply_stamps_last_contact_even_when_phase_guard_skipped(
        reply_cwd, monkeypatch, capsys):
    # AC2: 最終接触日 updates on EVERY reply-record — including a reply on a row not
    # at 連絡済, where the phase is intentionally left as-is. The date still records
    # that a contact happened (the audit signal must not depend on phase advance).
    cwd, doc_id = reply_cwd  # acc-1 stays 未接触 (never contacted)
    out = _reply(monkeypatch, capsys, doc_id, "acc-1", mid="r2")
    assert out["phase_driven_to"] is None          # phase guard-skipped
    assert _row_cells(doc_id, "acc-1")["last_contact"] == commands._now_iso()[:10]


def test_reply_record_rejects_unknown_account(reply_cwd, monkeypatch, capsys):
    cwd, doc_id = reply_cwd
    with pytest.raises(SystemExit) as ei:
        _reply(monkeypatch, capsys, doc_id, "acc-999")
    assert ei.value.code == 1


def test_awaiting_reply_includes_sent_message_id(reply_cwd, monkeypatch, capsys):
    # A batch with a sent message-id surfaces in the worklist so reply-watch can
    # poll the right thread.
    cwd, doc_id = reply_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")
    data = _read(cwd)
    data.setdefault("attack_list_send_batches", []).append({
        "id": "sendb-1", "doc_id": doc_id, "status": "sent",
        "recipients": [{"acc_id": "acc-1", "message_id": "sent-abc", "sent_at": "T0"}]})
    _write(cwd, data)
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_awaiting_reply()
    out = json.loads(capsys.readouterr().out)
    assert out["awaiting"][0]["message_id"] == "sent-abc"
