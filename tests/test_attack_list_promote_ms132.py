"""Lead conversion — ms-132 e-4506.

A reacted prospect (attack-list row at 返信あり / アポ) is promoted to an
Opportunity, and the Account's lifecycle phase is driven 未接触→リード. The
attack-list row is left in place (history preserved). Guards: only 返信あり/アポ
rows convert, and an Account that already has a live Opportunity is refused
(no duplicate deal).
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


def _write(cwd, data):
    # ms-148 e-5414: SQLite is the source of truth; a direct project.json write
    # only touches the read-only mirror. Seed through the store.
    from store import get_store
    store = get_store(project_file=str(cwd / ".beacon" / "project.json"))
    if hasattr(store, "apply"):
        store.apply(lambda _cur: (data, None), validate=False)
    else:
        store.save_project(data, validate=False)


def _read(cwd):
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


def _clear(monkeypatch):
    for k in ("BEACON_DOC_ID", "BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE",
              "BEACON_SEND_ACC_ID", "BEACON_OPP_TITLE", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def _capture(fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return json.loads(buf.getvalue())


@pytest.fixture
def promote_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    data = se.build_sales_project("Smoke", "close")
    se.acquisition_add(data, "獲得A")
    for name in ("未A", "未B"):
        aid = se.account_add(data, name, phase="未接触")
        se.find_account(data, aid)["contacts"] = [{"name": name, "email": f"{aid}@x.com"}]
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
    commands.cmd_acquisition_attack_list_fill()
    return cwd, doc_id


def _set_row_phase(doc_id, acc_id, to_phase):
    content, title, model = commands._load_table_model(doc_id)
    table_type.install()
    for row in table_doc.active_rows(model):
        if row["cells"].get("account") == acc_id:
            table_doc.set_cell(model, row["id"], "phase", to_phase, actor="t", at="T0")
            commands._write_table_model(doc_id, title, content, model)
            return
    raise AssertionError("row not found")


def _promote(monkeypatch, capsys, doc_id, acc_id, title=""):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_SEND_ACC_ID", acc_id)
    if title:
        monkeypatch.setenv("BEACON_OPP_TITLE", title)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attack_list_promote()
    return json.loads(capsys.readouterr().out)


def test_promote_replied_creates_opp_and_drives_account_phase(promote_cwd, monkeypatch, capsys):
    cwd, doc_id = promote_cwd
    _set_row_phase(doc_id, "acc-1", "返信あり")
    out = _promote(monkeypatch, capsys, doc_id, "acc-1")
    assert out["opportunity"].startswith("opp-")
    assert out["account_phase_driven_to"] == "リード"
    assert out["account_phase"] == "リード"   # actual resulting phase (truth field)
    assert out["row_kept"] is True
    data = _read(cwd)
    opp = next(o for o in data["opportunities"] if o["id"] == out["opportunity"])
    assert opp["account_id"] == "acc-1"
    assert se.find_account(data, "acc-1")["phase"] == "リード"
    # row preserved (still present, history intact)
    _c, _t, model = commands._load_table_model(doc_id)
    assert any(r["cells"].get("account") == "acc-1" for r in table_doc.active_rows(model))


def test_promote_appointment_phase_also_converts(promote_cwd, monkeypatch, capsys):
    cwd, doc_id = promote_cwd
    _set_row_phase(doc_id, "acc-1", "アポ")
    out = _promote(monkeypatch, capsys, doc_id, "acc-1")
    assert out["opportunity"].startswith("opp-")


def test_promote_refused_for_non_reacted_row(promote_cwd, monkeypatch, capsys):
    cwd, doc_id = promote_cwd
    _set_row_phase(doc_id, "acc-1", "連絡済")  # contacted, not yet replied
    with pytest.raises(SystemExit) as ei:
        _promote(monkeypatch, capsys, doc_id, "acc-1")
    assert ei.value.code == 1
    assert not _read(cwd).get("opportunities")


def test_promote_refused_if_account_has_live_opp(promote_cwd, monkeypatch, capsys):
    cwd, doc_id = promote_cwd
    _set_row_phase(doc_id, "acc-1", "返信あり")
    _promote(monkeypatch, capsys, doc_id, "acc-1")  # first promote ok
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        _promote(monkeypatch, capsys, doc_id, "acc-1")  # duplicate refused
    assert ei.value.code == 1
    assert len(_read(cwd)["opportunities"]) == 1


def test_promote_does_not_regress_advanced_account(promote_cwd, monkeypatch, capsys):
    cwd, doc_id = promote_cwd
    _set_row_phase(doc_id, "acc-1", "返信あり")
    data = _read(cwd)
    se.find_account(data, "acc-1")["phase"] = "成約顧客"  # already far along
    _write(cwd, data)
    out = _promote(monkeypatch, capsys, doc_id, "acc-1")
    assert out["account_phase_driven_to"] is None  # not regressed
    assert se.find_account(_read(cwd), "acc-1")["phase"] == "成約顧客"
