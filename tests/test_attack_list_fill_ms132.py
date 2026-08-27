"""Bulk-register未接触 Accounts into an attack-list — ms-132 e-4503.

Two layers:
1. ``sales_entities.filter_accounts`` — the condition query (phase / assignee /
   name substring, cancelled excluded).
2. ``beacon acquisition attack-list-fill`` — matched Accounts become prospect
   rows at the list's entry phase; re-running is idempotent (dedup, AC3);
   ``--dry-run`` previews without writing; a non-attack-list doc is refused.
Mirrors the ms-116 in-process harness (``commands.cmd_*`` + BEACON_PROJECT_FILE).
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
    # ms-148 e-5414: SQLite is the source of truth; a direct project.json write
    # only touches the read-only mirror. Seed through the store (apply blind-
    # overwrites without the load→save conflict guard).
    from store import get_store
    store = get_store(project_file=str(cwd / ".beacon" / "project.json"))
    if hasattr(store, "apply"):
        store.apply(lambda _cur: (data, None), validate=False)
    else:
        store.save_project(data, validate=False)


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


@pytest.fixture
def sales_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    data = se.build_sales_project("Smoke Sales", "close deals")
    se.acquisition_add(data, "獲得A")  # acq-1
    _write(cwd, data)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


# --- filter_accounts unit --------------------------------------------------

def _acc(name, phase, assignee=""):
    return {"id": None, "name": name, "phase": phase, "assignee": assignee}


def test_filter_by_phase():
    data = {"accounts": [{"id": "acc-1", "name": "A", "phase": "未接触"},
                         {"id": "acc-2", "name": "B", "phase": "リード"},
                         {"id": "acc-3", "name": "C", "phase": "未接触"}]}
    got = [a["id"] for a in se.filter_accounts(data, phase="未接触")]
    assert got == ["acc-1", "acc-3"]


def test_filter_by_assignee_and_name():
    data = {"accounts": [
        {"id": "acc-1", "name": "Acme工業", "phase": "未接触", "assignee": "alice"},
        {"id": "acc-2", "name": "Beta商事", "phase": "未接触", "assignee": "bob"},
        {"id": "acc-3", "name": "Acme物流", "phase": "未接触", "assignee": "alice"}]}
    assert [a["id"] for a in se.filter_accounts(data, assignee="alice")] == ["acc-1", "acc-3"]
    assert [a["id"] for a in se.filter_accounts(data, name_contains="商事")] == ["acc-2"]


def test_filter_excludes_cancelled():
    data = {"accounts": [{"id": "acc-1", "name": "A", "phase": "未接触"},
                         {"id": "acc-2", "name": "B", "phase": "未接触",
                          "status": se.CANCELLED_STATUS}]}
    assert [a["id"] for a in se.filter_accounts(data, phase="未接触")] == ["acc-1"]


# --- fill command ----------------------------------------------------------

def _make_list(monkeypatch, capsys, acq="acq-1", title="攻略リスト"):
    for k in ("BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE", "BEACON_ACQ_LIST_PHASES",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_ACQ_ID", acq)
    monkeypatch.setenv("BEACON_ACQ_LIST_TITLE", title)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attach_list()
    return json.loads(capsys.readouterr().out)["doc_id"]


def _fill(monkeypatch, capsys, doc_id, **flags):
    for k in ("BEACON_DOC_ID", "BEACON_FILL_PHASE", "BEACON_FILL_ASSIGNEE",
              "BEACON_FILL_NAME", "BEACON_FILL_LIMIT", "BEACON_DRY_RUN",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_DOC_ID", doc_id)
    monkeypatch.setenv("BEACON_JSON", "1")
    for k, v in flags.items():
        monkeypatch.setenv(k, str(v))
    commands.cmd_acquisition_attack_list_fill()
    return json.loads(capsys.readouterr().out)


def _seed_accounts(cwd):
    data = _read(cwd)
    se.account_add(data, "未A", phase="未接触")           # acc-1
    se.account_add(data, "未B", phase="未接触")           # acc-2
    se.account_add(data, "既C", phase="リード")           # acc-3 (contacted, excluded)
    _write(cwd, data)


def _list_rows(monkeypatch, capsys, acq="acq-1"):
    for k in ("BEACON_ACQ_ID", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_ACQ_ID", acq)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_lists()
    return json.loads(capsys.readouterr().out)["lists"]


def test_fill_bulk_adds_untouched_accounts(sales_cwd, monkeypatch, capsys):
    doc_id = _make_list(monkeypatch, capsys)
    _seed_accounts(sales_cwd)
    out = _fill(monkeypatch, capsys, doc_id)
    assert out["matched"] == 2  # only 未接触 (acc-1, acc-2), not リード acc-3
    assert set(out["target_ids"]) == {"acc-1", "acc-2"}
    assert out["entry_phase"] == "未接触"
    rows = _list_rows(monkeypatch, capsys)
    assert rows[0]["row_count"] == 2
    assert rows[0]["phase_counts"] == {"未接触": 2}


def test_fill_is_idempotent_dedup(sales_cwd, monkeypatch, capsys):
    doc_id = _make_list(monkeypatch, capsys)
    _seed_accounts(sales_cwd)
    _fill(monkeypatch, capsys, doc_id)
    out2 = _fill(monkeypatch, capsys, doc_id)  # re-run
    assert out2["target_ids"] == []
    assert set(out2["skipped_duplicates"]) == {"acc-1", "acc-2"}
    assert _list_rows(monkeypatch, capsys)[0]["row_count"] == 2  # no doubles


def test_fill_dry_run_writes_nothing(sales_cwd, monkeypatch, capsys):
    doc_id = _make_list(monkeypatch, capsys)
    _seed_accounts(sales_cwd)
    out = _fill(monkeypatch, capsys, doc_id, BEACON_DRY_RUN="1")
    assert out["dry_run"] is True
    assert set(out["target_ids"]) == {"acc-1", "acc-2"}  # would-add under dry_run
    assert _list_rows(monkeypatch, capsys)[0]["row_count"] == 0  # nothing written


def test_fill_limit(sales_cwd, monkeypatch, capsys):
    doc_id = _make_list(monkeypatch, capsys)
    _seed_accounts(sales_cwd)
    out = _fill(monkeypatch, capsys, doc_id, BEACON_FILL_LIMIT="1")
    assert len(out["target_ids"]) == 1


def test_fill_rejects_non_attack_list(sales_cwd, monkeypatch, capsys):
    # A plain table-doc (not an attack-list) must be refused.
    for k in ("BEACON_TITLE", "BEACON_COLUMNS", "BEACON_SCOPE", "BEACON_TARGET",
              "BEACON_JSON", "BEACON_MS", "BEACON_OP", "BEACON_DOC_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_TITLE", "別表")
    monkeypatch.setenv("BEACON_COLUMNS", json.dumps([{"key": "note", "type": "text"}]))
    monkeypatch.setenv("BEACON_SCOPE", "memo")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_doc_table_create()
    doc_id = json.loads(capsys.readouterr().out)["doc_id"]
    with pytest.raises(SystemExit) as ei:
        _fill(monkeypatch, capsys, doc_id)
    assert ei.value.code == 1
    assert "アタックリスト" in capsys.readouterr().err


def test_fill_default_phase_follows_configured_account_funnel(sales_cwd, monkeypatch, capsys):
    # AX review PR #548: omitting --account-phase must use the project's configured
    # first account phase, not a hardcoded '未接触'. Rename the account funnel entry
    # and confirm the default filter follows it (no silent zero-match).
    doc_id = _make_list(monkeypatch, capsys)
    for k in ("BEACON_FUNNEL_KIND", "BEACON_PHASE_OLD", "BEACON_PHASE_NEW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_FUNNEL_KIND", "account")
    monkeypatch.setenv("BEACON_PHASE_OLD", "未接触")
    monkeypatch.setenv("BEACON_PHASE_NEW", "見込みリスト")
    commands.cmd_phase_rename()
    capsys.readouterr()
    data = _read(sales_cwd)
    se.account_add(data, "候補X", phase="見込みリスト")
    _write(sales_cwd, data)
    out = _fill(monkeypatch, capsys, doc_id)  # no --account-phase
    assert out["filter_phase"] == "見込みリスト"
    assert len(out["target_ids"]) == 1


def test_fill_uses_lists_own_entry_phase(sales_cwd, monkeypatch, capsys):
    # Rename the prospect funnel entry, make a fresh list (bakes the renamed
    # funnel), then fill: new rows must start at the list's entry phase.
    for k in ("BEACON_FUNNEL_KIND", "BEACON_PHASE_OLD", "BEACON_PHASE_NEW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_FUNNEL_KIND", "prospect")
    monkeypatch.setenv("BEACON_PHASE_OLD", "未接触")
    monkeypatch.setenv("BEACON_PHASE_NEW", "新規")
    commands.cmd_phase_rename()
    capsys.readouterr()
    doc_id = _make_list(monkeypatch, capsys, title="新funnelリスト")
    data = _read(sales_cwd)
    se.account_add(data, "未X", phase="新規")  # account phase matches renamed entry
    _write(sales_cwd, data)
    out = _fill(monkeypatch, capsys, doc_id, BEACON_FILL_PHASE="新規")
    assert out["entry_phase"] == "新規"
    assert len(out["target_ids"]) == 1
