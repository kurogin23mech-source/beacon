"""ms-146 e-5344 — CLI layer: `--field key=value` on `beacon target work-item add`
and `beacon target evidence add` reaches the engine, and the value it wrote is
echoed back.

Separate from the engine tests because the CLI is where the two failure modes a
user actually hits live: a flag that parses but never reaches the engine (a
silent drop), and a write that lands but prints nothing (indistinguishable from
a no-op — the AX病理 ms-120 exists to remove).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_target  # noqa: E402


UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [{"key": "started", "label": "着手"},
               {"key": "enough", "label": "十分やった", "terminal": True}],
    "work_item_fields": [
        {"key": "budget_h", "label": "時間予算(時間)", "type": "number",
         "required": True}],
    "evidence_fields": [
        {"key": "moved", "label": "上位目的に効いたか", "type": "string",
         "required": True}],
}

_ENV_KEYS = ("BEACON_WI_ACTION", "BEACON_EV_ACTION", "BEACON_TARGET_CLASS",
             "BEACON_TARGET_ID", "BEACON_WI_ITEM_ID", "BEACON_WI_DESC",
             "BEACON_EV_SUMMARY", "BEACON_EV_FOR", "BEACON_FIELDS",
             "BEACON_REASON", "BEACON_JSON")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": [],
        "target_classes": [UNDERTAKING],
        "undertakings": [{"id": "ut-1", "label": "セミナー準備",
                          "kind": "undertaking", "phase": "started",
                          "status": "todo", "work_items": [], "evidence": [],
                          "phase_history": []}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _stored(proj_path, arm):
    data = json.loads((proj_path / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))
    return data["undertakings"][0][arm]


def test_work_item_field_flag_reaches_the_record(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_WI_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_WI_DESC", "原稿を考える")
    monkeypatch.setenv("BEACON_FIELDS", "budget_h=2\n")
    cmd_target.cmd_target_work_item()
    items = _stored(proj, "work_items")
    assert items[0]["budget_h"] == "2"
    # echoed back — a write the user cannot see is indistinguishable from a no-op
    assert "budget_h = 2" in capsys.readouterr().out


def test_work_item_missing_required_field_exits_nonzero(proj, monkeypatch):
    monkeypatch.setenv("BEACON_WI_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_WI_DESC", "原稿を考える")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_work_item()
    assert e.value.code != 0
    assert _stored(proj, "work_items") == []


def test_evidence_field_flag_reaches_the_record(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_EV_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_EV_SUMMARY", "通し1本")
    monkeypatch.setenv("BEACON_FIELDS", "moved=効いてない\n")
    cmd_target.cmd_target_evidence()
    evs = _stored(proj, "evidence")
    assert evs[0]["moved"] == "効いてない"
    assert "moved = 効いてない" in capsys.readouterr().out


def test_evidence_missing_required_field_exits_nonzero(proj, monkeypatch):
    monkeypatch.setenv("BEACON_EV_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_EV_SUMMARY", "通し1本")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_evidence()
    assert e.value.code != 0
    assert _stored(proj, "evidence") == []


def test_undeclared_field_is_refused_not_dropped(proj, monkeypatch):
    monkeypatch.setenv("BEACON_WI_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_WI_DESC", "原稿")
    monkeypatch.setenv("BEACON_FIELDS", "budget_h=2\nnope=x\n")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_work_item()
    assert _stored(proj, "work_items") == []


# ---------------------------------------------------------------------------
# ms-146 e-5348 — the cancel verb at the CLI layer.
# ---------------------------------------------------------------------------

def _add_item(monkeypatch):
    monkeypatch.setenv("BEACON_WI_ACTION", "add")
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_WI_DESC", "やらないやつ")
    monkeypatch.setenv("BEACON_FIELDS", "budget_h=1\n")
    cmd_target.cmd_target_work_item()
    monkeypatch.delenv("BEACON_FIELDS", raising=False)
    monkeypatch.delenv("BEACON_WI_DESC", raising=False)


def test_cancel_requires_a_reason(proj, monkeypatch):
    """A cancel with no reason is the record that reads worst later."""
    _add_item(monkeypatch)
    monkeypatch.setenv("BEACON_WI_ACTION", "cancel")
    monkeypatch.setenv("BEACON_WI_ITEM_ID", "ut-1-w1")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_work_item()
    assert e.value.code != 0
    assert _stored(proj, "work_items")[0]["status"] != "cancelled"


def test_cancel_with_a_reason_records_it(proj, monkeypatch, capsys):
    _add_item(monkeypatch)
    monkeypatch.setenv("BEACON_WI_ACTION", "cancel")
    monkeypatch.setenv("BEACON_WI_ITEM_ID", "ut-1-w1")
    monkeypatch.setenv("BEACON_REASON", "成約に効かないのでやらない")
    cmd_target.cmd_target_work_item()
    item = _stored(proj, "work_items")[0]
    assert item["status"] == "cancelled"
    assert item["meta"]["cancel_reason"] == "成約に効かないのでやらない"
    out = capsys.readouterr().out
    assert "取り消し" in out and "成約に効かない" in out


def test_list_shows_a_cancelled_item_as_cancelled(proj, monkeypatch, capsys):
    """A cancel the list cannot show is a silent no-op wearing a different hat."""
    _add_item(monkeypatch)
    monkeypatch.setenv("BEACON_WI_ACTION", "cancel")
    monkeypatch.setenv("BEACON_WI_ITEM_ID", "ut-1-w1")
    monkeypatch.setenv("BEACON_REASON", "やらないと決めた")
    cmd_target.cmd_target_work_item()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_WI_ACTION", "list")
    cmd_target.cmd_target_work_item()
    out = capsys.readouterr().out
    assert "×" in out
    assert "やらないと決めた" in out
