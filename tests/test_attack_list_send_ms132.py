"""Bulk outreach with the human 1-confirm gate — ms-132 e-4504 (SPEC 方針4).

The point of this feature is the *approval boundary*: no non-human path can cause
an external send's recorded effect, and nothing is sent/recorded before the human
confirm. These tests lock the structural invariants:

- plan (dry-run) writes only a pending batch — no 証跡, no phase change;
- authorize (--confirm) is refused from a bus-origin context (`BEACON_BUS_ORIGIN`);
- a send-record is refused unless an *authorized* batch contains that recipient,
  and refused on a double-send;
- the happy path plan → confirm → record writes the outbound Communication and
  drives the row 未接触 → 連絡済.

Plus the sales_entities batch-model unit invariants.
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


def _write(cwd: Path, data: dict) -> None:
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


# --- batch-model unit invariants -------------------------------------------

def test_batch_lifecycle_pending_authorize():
    data = {}
    b = se.create_send_batch(data, doc_id="d", recipients=[{"acc_id": "acc-1"}],
                             message_digest="x", message_preview="p", created_at="T0")
    assert b["status"] == "pending"
    se.authorize_send_batch(data, b["id"], at="T1", by="me")
    assert se.find_send_batch(data, b["id"])["status"] == "authorized"


def test_replan_supersedes_prior_pending():
    data = {}
    b1 = se.create_send_batch(data, doc_id="d", recipients=[{"acc_id": "acc-1"}],
                              message_digest="x", message_preview="p", created_at="T0")
    b2 = se.create_send_batch(data, doc_id="d", recipients=[{"acc_id": "acc-2"}],
                              message_digest="y", message_preview="q", created_at="T1")
    assert se.find_send_batch(data, b1["id"])["status"] == "cancelled"
    assert b2["status"] == "pending"
    assert se.pending_send_batch_for_doc(data, "d")["id"] == b2["id"]


def test_record_requires_authorized_batch():
    data = {}
    se.create_send_batch(data, doc_id="d", recipients=[{"acc_id": "acc-1"}],
                         message_digest="x", message_preview="p", created_at="T0")
    # pending, not authorized → record refuses
    with pytest.raises(ValueError):
        se.record_batch_send(data, "d", "acc-1", at="T1")


def test_record_rejects_non_recipient_and_double_send():
    data = {}
    b = se.create_send_batch(data, doc_id="d", recipients=[{"acc_id": "acc-1"}],
                             message_digest="x", message_preview="p", created_at="T0")
    se.authorize_send_batch(data, b["id"], at="T1")
    with pytest.raises(ValueError):
        se.record_batch_send(data, "d", "acc-9", at="T2")  # not a recipient
    se.record_batch_send(data, "d", "acc-1", at="T2")
    with pytest.raises(ValueError):
        se.record_batch_send(data, "d", "acc-1", at="T3")  # double send
    assert se.find_send_batch(data, b["id"])["status"] == "sent"  # all recipients done


# --- CLI structural-gate tests ---------------------------------------------

@pytest.fixture
def outreach_cwd(tmp_path, monkeypatch):
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
    # build an attack-list and fill it with the two 未接触 accounts
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_ACQ_ID", "acq-1")
    monkeypatch.setenv("BEACON_ACQ_LIST_TITLE", "攻略")
    monkeypatch.setenv("BEACON_JSON", "1")
    import io
    doc_id = _capture(monkeypatch, commands.cmd_acquisition_attach_list)["doc_id"]
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_fill()
    return cwd, doc_id


_ENV_KEYS = ("BEACON_DOC_ID", "BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE",
             "BEACON_ACQ_LIST_PHASES", "BEACON_SEND_SUBJECT", "BEACON_SEND_MESSAGE",
             "BEACON_SEND_FROM_PHASE", "BEACON_SEND_LIMIT", "BEACON_CONFIRM",
             "BEACON_SEND_ACC_ID", "BEACON_SEND_MESSAGE_ID", "BEACON_SEND_URL",
             "BEACON_BUS_ORIGIN", "BEACON_JSON", "BEACON_FILL_PHASE")


def _clear(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _capture(monkeypatch, fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return json.loads(buf.getvalue())


_SUBJ = "初回連絡"
_MSG = "はじめまして、ご提案があります。"


def _plan(monkeypatch, capsys, doc_id, subject=_SUBJ, message=_MSG, **flags):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_SEND_SUBJECT", subject)
    monkeypatch.setenv("BEACON_SEND_MESSAGE", message)
    monkeypatch.setenv("BEACON_JSON", "1")
    for k, v in flags.items():
        monkeypatch.setenv(k, str(v))
    commands.cmd_acquisition_attack_list_send()
    return json.loads(capsys.readouterr().out)


def test_plan_writes_pending_batch_only(outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    out = _plan(monkeypatch, capsys, doc_id, **{"BEACON_SEND_SUBJECT": "初回"})
    assert out["status"] == "pending"
    assert out["recipient_count"] == 2
    assert {r["acc_id"] for r in out["recipients"]} == {"acc-1", "acc-2"}
    data = _read(cwd)
    # nothing sent/recorded: no communications, batch pending
    for a in data["accounts"]:
        assert not a.get("communications")
    assert se.pending_send_batch_for_doc(data, doc_id) is not None


def test_confirm_is_refused_from_bus_origin(outreach_cwd, monkeypatch, capsys):
    # THE structural invariant: an autonomous/bus context can never authorize.
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_CONFIRM", "1")
    monkeypatch.setenv("BEACON_BUS_ORIGIN", "1")  # simulate DM/auto-execute origin
    with pytest.raises(SystemExit) as ei:
        commands.cmd_acquisition_attack_list_send()
    assert ei.value.code == 1
    # batch must remain pending (not authorized)
    assert se.pending_send_batch_for_doc(_read(cwd), doc_id) is not None


def test_record_refused_before_confirm(outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)  # pending, not authorized
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_SEND_ACC_ID", "acc-1")
    monkeypatch.setenv("BEACON_SEND_MESSAGE_ID", "m1")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_acquisition_attack_list_send_record()
    assert ei.value.code == 1
    assert not any(a.get("communications") for a in _read(cwd)["accounts"])


def _confirm(monkeypatch, doc_id):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_CONFIRM", "1")
    commands.cmd_acquisition_attack_list_send()


def _record(monkeypatch, capsys, doc_id, acc_id, mid="m1",
            subject=_SUBJ, message=_MSG):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_SEND_ACC_ID", acc_id)
    monkeypatch.setenv("BEACON_SEND_MESSAGE_ID", mid)
    monkeypatch.setenv("BEACON_SEND_SUBJECT", subject)
    monkeypatch.setenv("BEACON_SEND_MESSAGE", message)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_send_record()
    return json.loads(capsys.readouterr().out)


def test_confirm_is_refused_when_armed(outreach_cwd, monkeypatch, capsys):
    # An armed (autonomous DM-reply) session must not authorize a bulk send:
    # arming grants auto-reply budget, not blanket send authorization.
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    monkeypatch.setattr(commands, "_read_bus_budget",
                        lambda: {"total": 5, "used": 0})
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_CONFIRM", "1")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_acquisition_attack_list_send()
    assert ei.value.code == 1
    assert se.pending_send_batch_for_doc(_read(cwd), doc_id) is not None  # still pending


def test_happy_path_confirm_then_record_drives_phase_and_records_証跡(
        outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _confirm(monkeypatch, doc_id)
    capsys.readouterr()
    out = _record(monkeypatch, capsys, doc_id, "acc-1", mid="msg-123")
    assert out["comm_id"]
    assert out["phase_driven_to"] == "連絡済"
    data = _read(cwd)
    acc = se.find_account(data, "acc-1")
    comms = acc.get("communications", [])
    assert len(comms) == 1
    assert comms[0]["direction"] == "outbound" and comms[0]["channel"] == "email"
    assert comms[0]["source"]["ref"] == "msg-123"


def test_record_requires_message_id(outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _confirm(monkeypatch, doc_id)
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        _record(monkeypatch, capsys, doc_id, "acc-1", mid="")  # missing message-id
    assert ei.value.code == 1


def test_record_refuses_content_mismatch(outreach_cwd, monkeypatch, capsys):
    # The recorded send's 文面 must match the confirmed one (digest bind).
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _confirm(monkeypatch, doc_id)
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        _record(monkeypatch, capsys, doc_id, "acc-1", message="別の文面にすり替え")
    assert ei.value.code == 1
    assert not any(a.get("communications") for a in _read(cwd)["accounts"])


def test_record_double_send_refused_via_cli(outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _confirm(monkeypatch, doc_id)
    capsys.readouterr()
    _record(monkeypatch, capsys, doc_id, "acc-1")
    with pytest.raises(SystemExit) as ei:
        _record(monkeypatch, capsys, doc_id, "acc-1")  # already sent
    assert ei.value.code == 1


def _row_cells(doc_id, acc_id):
    import table_doc
    _c, _t, model = commands._load_table_model(doc_id)
    for r in table_doc.active_rows(model):
        if r["cells"].get("account") == acc_id:
            return r["cells"]
    raise AssertionError(f"row for {acc_id} not found")


def test_send_record_stamps_last_contact(outreach_cwd, monkeypatch, capsys):
    # e-4623: the 最終接触日 (last_contact) column was schema-defined but no contact
    # flow wrote it. send-record must fill it (= 送信日) on the SAME write as the
    # phase drive — phase and date commit together (no partial write).
    cwd, doc_id = outreach_cwd
    _plan(monkeypatch, capsys, doc_id)
    _confirm(monkeypatch, doc_id)
    capsys.readouterr()
    assert not _row_cells(doc_id, "acc-1").get("last_contact")  # empty before
    _record(monkeypatch, capsys, doc_id, "acc-1", mid="msg-1")
    cells = _row_cells(doc_id, "acc-1")
    assert cells["last_contact"] == commands._now_iso()[:10]     # stamped = today
    assert cells["phase"] == "連絡済"                            # committed together


def test_plan_excludes_rows_without_email(outreach_cwd, monkeypatch, capsys):
    cwd, doc_id = outreach_cwd
    # add a 3rd account with NO contact email, and put it on the list
    data = _read(cwd)
    se.account_add(data, "未C", phase="未接触")  # acc-3, no contacts
    _write(cwd, data)
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_fill()  # adds acc-3 as a row
    capsys.readouterr()
    out = _plan(monkeypatch, capsys, doc_id)
    assert "acc-3" in out["skipped_no_email"]
    assert "acc-3" not in {r["acc_id"] for r in out["recipients"]}
