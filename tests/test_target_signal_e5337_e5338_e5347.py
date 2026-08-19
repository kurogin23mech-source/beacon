"""ms-146 — the pieces that turn a pile of notes into a signal:

  * e-5347: a cancelled work item leaves the remaining-count denominator.
  * e-5338: a declared field may constrain its value to a fixed set, enforced on
    EVERY write path.
  * e-5337: declaration-driven time-budget tracking, so the engine can say
    "this has spent more than it said it would" without knowing what an
    executive is.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_target  # noqa: E402
import target_descriptor as td  # noqa: E402
import target_engine as te  # noqa: E402
import work_model as wm  # noqa: E402


UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [{"key": "purpose", "label": "上位目的", "type": "string"}],
    "phases": [
        {"key": "started", "label": "着手", "fields": [
            {"key": "time_budget_h", "label": "時間予算", "type": "number"}]},
        {"key": "enough", "label": "十分やった", "terminal": True},
    ],
    "work_item_fields": [
        {"key": "budget_h", "label": "時間予算", "type": "number"}],
    "evidence_fields": [
        {"key": "moved", "label": "効いたか", "type": "string",
         "choices": ["効いた", "効いてない", "わからない"]},
        {"key": "spent_h", "label": "消費時間", "type": "number"}],
    "budget_tracking": {"target_budget_field": "time_budget_h",
                        "work_item_budget_field": "budget_h",
                        "evidence_spend_field": "spent_h"},
}


def _target():
    data = {"name": "t"}
    rec = te.create_target(data, UNDERTAKING, label="セミナー資料を作る",
                           fields={"purpose": "9月セミナーで成約3件"})
    return data, rec


# ---------------------------------------------------------------------------
# e-5347 — a cancelled item is no longer OWED.
# ---------------------------------------------------------------------------

def test_cancelled_items_leave_the_remaining_count():
    """Deciding NOT to do something is the point of this class; a total that does
    not shrink tells the owner their decision changed nothing."""
    data, rec = _target()
    for desc_text in ("構成を起こす", "スライドを作る", "配布資料を作る"):
        te.add_work_item(data, UNDERTAKING, rec["id"], desc_text)
    assert te.project_target(UNDERTAKING, rec)["work_items_total"] == 3

    te.cancel_work_item(data, UNDERTAKING, rec["id"], f"{rec['id']}-w1",
                        reason="スコープ訂正で不要になった")
    proj = te.project_target(UNDERTAKING, rec)
    assert proj["work_items_total"] == 2
    assert proj["work_items_done"] == 0


def test_cancelling_never_inflates_the_done_count():
    """``work_items_done`` keeps meaning "actually finished"."""
    data, rec = _target()
    te.add_work_item(data, UNDERTAKING, rec["id"], "やる")
    te.add_work_item(data, UNDERTAKING, rec["id"], "やらない")
    te.complete_work_item(data, UNDERTAKING, rec["id"], f"{rec['id']}-w1")
    te.cancel_work_item(data, UNDERTAKING, rec["id"], f"{rec['id']}-w2",
                        reason="やらないと決めた")
    proj = te.project_target(UNDERTAKING, rec)
    assert (proj["work_items_done"], proj["work_items_total"]) == (1, 1)


def test_a_cancelled_item_is_still_readable():
    """Out of the denominator, NOT out of the record — nothing is deleted."""
    data, rec = _target()
    te.add_work_item(data, UNDERTAKING, rec["id"], "やらない")
    te.cancel_work_item(data, UNDERTAKING, rec["id"], f"{rec['id']}-w1",
                        reason="スコープ訂正")
    stored = te.list_work_items(rec)
    assert len(stored) == 1
    assert wm.is_cancelled(stored[0])
    assert stored[0]["meta"]["cancel_reason"] == "スコープ訂正"


# ---------------------------------------------------------------------------
# e-5338 — declared choices, enforced on every write path.
# ---------------------------------------------------------------------------

def test_evidence_accepts_a_declared_choice():
    data, rec = _target()
    ev = te.add_evidence(data, UNDERTAKING, rec["id"], summary="通し1本",
                         fields={"moved": "効いてない", "spent_h": "2"})
    assert ev["moved"] == "効いてない"


def test_evidence_refuses_a_value_outside_the_choices():
    """A near-miss spelling would break "効いてない twice in a row" detection."""
    data, rec = _target()
    with pytest.raises(te.TargetEngineError) as e:
        te.add_evidence(data, UNDERTAKING, rec["id"],
                        fields={"moved": "まあまあ効いた"})
    assert "選べません" in str(e.value)
    assert te.list_evidence(rec) == []


def test_choices_are_enforced_at_create_and_at_phase_advance():
    desc = json.loads(json.dumps(UNDERTAKING))
    desc["fields"].append({"key": "mode", "label": "様式", "type": "string",
                           "choices": ["A", "B"]})
    # A leading phase so the target does NOT start already inside the phase whose
    # field we want to test the advance into.
    desc["phases"].insert(0, {"key": "not_started", "label": "やってない"})
    desc["phases"][1]["fields"].append(
        {"key": "risk", "label": "リスク", "type": "string",
         "choices": ["高", "低"]})
    data = {"name": "t"}
    with pytest.raises(te.TargetEngineError):
        te.create_target(data, desc, label="x", fields={"mode": "C"})
    rec = te.create_target(data, desc, label="x", fields={"mode": "A"})
    with pytest.raises(te.TargetEngineError):
        te.advance_target(data, desc, rec["id"], to_phase="started",
                          fields={"risk": "中"})
    assert te.current_phase(rec) != "started", "a refused advance must not move"


def test_an_unconstrained_field_still_takes_anything():
    data, rec = _target()
    ev = te.add_evidence(data, UNDERTAKING, rec["id"],
                         fields={"spent_h": "1.5"})
    assert ev["spent_h"] == "1.5"


def test_choices_declaration_is_validated():
    bad = json.loads(json.dumps(UNDERTAKING))
    bad["evidence_fields"][0]["choices"] = []
    assert td.validate_descriptor(bad)
    dup = json.loads(json.dumps(UNDERTAKING))
    dup["evidence_fields"][0]["choices"] = ["a", "a"]
    assert td.validate_descriptor(dup)


# ---------------------------------------------------------------------------
# e-5337 — time budget vs recorded spend.
# ---------------------------------------------------------------------------

def test_budget_status_totals_spend_from_evidence():
    data, rec = _target()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "4"})
    item = te.add_work_item(data, UNDERTAKING, rec["id"], "原稿",
                            fields={"budget_h": "2"})
    te.add_evidence(data, UNDERTAKING, rec["id"], linked_id=item["id"],
                    fields={"moved": "効いた", "spent_h": "3"})
    status = te.budget_status(UNDERTAKING, rec)
    assert status["target_budget"] == 4.0
    assert status["spent_total"] == 3.0
    assert status["over_target"] is False
    assert status["items"][0]["spent"] == 3.0
    assert status["items"][0]["over"] is True, "3h spent against a 2h budget"
    assert te.is_over_budget(UNDERTAKING, rec) is True


def test_overrunning_the_target_budget_is_detected():
    data, rec = _target()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "4"})
    te.add_evidence(data, UNDERTAKING, rec["id"],
                    fields={"moved": "効いてない", "spent_h": "5"})
    status = te.budget_status(UNDERTAKING, rec)
    assert status["over_target"] is True
    assert status["spent_total"] == 5.0
    assert te.is_over_budget(UNDERTAKING, rec) is True


def test_spend_with_no_linked_item_still_counts_toward_the_target():
    """Time spent without naming an item is still time spent."""
    data, rec = _target()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "1"})
    te.add_evidence(data, UNDERTAKING, rec["id"],
                    fields={"moved": "わからない", "spent_h": "2"})
    assert te.budget_status(UNDERTAKING, rec)["spent_total"] == 2.0


def test_a_cancelled_items_budget_is_no_longer_owed():
    data, rec = _target()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "10"})
    item = te.add_work_item(data, UNDERTAKING, rec["id"], "やらない",
                            fields={"budget_h": "1"})
    te.add_evidence(data, UNDERTAKING, rec["id"], linked_id=item["id"],
                    fields={"moved": "効いてない", "spent_h": "5"})
    te.cancel_work_item(data, UNDERTAKING, rec["id"], item["id"],
                        reason="やらないと決めた")
    status = te.budget_status(UNDERTAKING, rec)
    assert status["items"] == [], "a cancelled item is not owed a budget"
    assert status["spent_total"] == 5.0, "but the time really was spent"


def test_a_class_declaring_no_budget_tracking_is_untouched():
    plain = json.loads(json.dumps(UNDERTAKING))
    plain.pop("budget_tracking")
    data = {"name": "t"}
    rec = te.create_target(data, plain, label="x")
    assert te.budget_status(plain, rec) == {}
    assert te.is_over_budget(plain, rec) is False


def test_unparseable_numbers_do_not_break_the_read():
    """One malformed note must not make the whole overrun read fail."""
    data, rec = _target()
    te.add_evidence(data, UNDERTAKING, rec["id"],
                    fields={"moved": "効いた", "spent_h": "だいたい2時間"})
    assert te.budget_status(UNDERTAKING, rec)["spent_total"] == 0.0


# ---------------------------------------------------------------------------
# CLI grammar for both.
# ---------------------------------------------------------------------------

_ENV = ("BEACON_TC_KIND", "BEACON_TC_FIELDS", "BEACON_TC_REQUIRED_FIELDS",
        "BEACON_TC_WI_FIELDS", "BEACON_TC_REQUIRED_WI_FIELDS",
        "BEACON_TC_EV_FIELDS", "BEACON_TC_REQUIRED_EV_FIELDS",
        "BEACON_TC_PHASE_FIELDS", "BEACON_TC_REQUIRED_PHASE_FIELDS",
        "BEACON_TC_REMOVE_FIELDS", "BEACON_TC_RENAME_FIELDS",
        "BEACON_TC_BUDGET_TRACKING")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    plain = json.loads(json.dumps(UNDERTAKING))
    plain.pop("budget_tracking")
    plain["evidence_fields"] = []
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "milestones": [], "target_classes": [plain],
        "undertakings": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _desc(proj_path):
    return json.loads((proj_path / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))["target_classes"][0]


def test_cli_parses_a_pipe_separated_choice_list(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_REQUIRED_EV_FIELDS",
                       "moved:効いたか:string:効いた|効いてない|わからない\n")
    cmd_target.cmd_target_class_update()
    field = td.evidence_fields(_desc(proj))[0]
    assert field["choices"] == ["効いた", "効いてない", "わからない"]
    assert field["required"] is True


def test_cli_refuses_a_single_choice(proj, monkeypatch):
    """One choice is not a choice — almost certainly a typo for a list."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_EV_FIELDS", "moved:効いたか:string:効いた\n")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()


def test_cli_budget_tracking_requires_declared_fields(proj, monkeypatch,
                                                      capsys):
    """Pointing the tracker at an undeclared field would report 0 spent forever."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_BUDGET_TRACKING",
                       "time_budget_h:budget_h:nope_h")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    assert "宣言されていません" in capsys.readouterr().err
    assert "budget_tracking" not in _desc(proj)


def test_cli_budget_tracking_lands_when_the_fields_exist(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_EV_FIELDS", "spent_h:消費時間:number\n")
    monkeypatch.setenv("BEACON_TC_BUDGET_TRACKING",
                       "time_budget_h:budget_h:spent_h")
    cmd_target.cmd_target_class_update()
    assert _desc(proj)["budget_tracking"]["evidence_spend_field"] == "spent_h"


# ---------------------------------------------------------------------------
# e-5338 — "this stopped working": a STREAK of ineffective evidence.
# ---------------------------------------------------------------------------

STALLING = json.loads(json.dumps(UNDERTAKING))
STALLING["stall_signal"] = {"evidence_field": "moved", "value": "効いてない",
                            "threshold": 2}


def _stalling_target():
    data = {"name": "t"}
    rec = te.create_target(data, STALLING, label="練習する",
                           fields={"purpose": "成約"})
    return data, rec


def _ev(data, rec, moved, spent="1"):
    return te.add_evidence(data, STALLING, rec["id"],
                           fields={"moved": moved, "spent_h": spent})


def test_two_ineffective_in_a_row_stalls():
    data, rec = _stalling_target()
    _ev(data, rec, "効いた")
    assert te.stall_status(STALLING, rec)["stalled"] is False
    _ev(data, rec, "効いてない")
    assert te.stall_status(STALLING, rec)["streak"] == 1
    _ev(data, rec, "効いてない")
    st = te.stall_status(STALLING, rec)
    assert (st["streak"], st["stalled"]) == (2, True)


def test_something_that_works_resets_the_streak():
    """Diminishing returns is a shape over time; a target that struggled early
    and then found its footing must NOT be told to stop."""
    data, rec = _stalling_target()
    _ev(data, rec, "効いてない")
    _ev(data, rec, "効いてない")
    assert te.stall_status(STALLING, rec)["stalled"] is True
    _ev(data, rec, "効いた")
    st = te.stall_status(STALLING, rec)
    assert (st["streak"], st["stalled"]) == (0, False)


def test_evidence_silent_about_effect_does_not_reset_the_streak():
    """A note that says nothing about effect is silence, not a recovery."""
    data, rec = _stalling_target()
    _ev(data, rec, "効いてない")
    te.add_evidence(data, STALLING, rec["id"], summary="移動しただけ",
                    fields={"moved": "効いてない"})
    # an evidence record carrying no value for the field at all
    te.list_evidence(rec).append({"id": "x", "summary": "メモ"})
    assert te.stall_status(STALLING, rec)["stalled"] is True


def test_a_class_declaring_no_stall_signal_is_untouched():
    data, rec = _target()
    assert te.stall_status(UNDERTAKING, rec) == {}


def test_stop_signals_reports_both_kinds_and_never_acts():
    data, rec = _stalling_target()
    te.advance_target(data, STALLING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "1"})
    _ev(data, rec, "効いてない", spent="2")
    _ev(data, rec, "効いてない", spent="1")
    kinds = {s["kind"] for s in te.stop_signals(STALLING, rec)}
    assert "budget_target" in kinds
    assert "stall" in kinds
    # reporting only — the target is not moved or closed (設計方針2)
    assert te.current_phase(rec) == "started"
    assert not work_model_is_done(rec)


def work_model_is_done(rec):
    import work_model as _wm
    return _wm.is_done(rec)


def test_no_signals_when_nothing_suggests_stopping():
    data, rec = _stalling_target()
    te.advance_target(data, STALLING, rec["id"], to_phase="started",
                      fields={"time_budget_h": "10"})
    _ev(data, rec, "効いた", spent="1")
    assert te.stop_signals(STALLING, rec) == []


def test_cli_stall_signal_requires_a_declared_evidence_field(proj, monkeypatch,
                                                            capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_STALL_SIGNAL", "nope:効いてない:2")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    assert "宣言されていません" in capsys.readouterr().err


def test_cli_stall_signal_value_must_be_a_declared_choice(proj, monkeypatch,
                                                          capsys):
    """A marker the field can never hold would look guarded while being blind."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_EV_FIELDS",
                       "moved:効いたか:string:効いた|効いてない\n")
    monkeypatch.setenv("BEACON_TC_STALL_SIGNAL", "moved:まあまあ:2")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    assert "選択肢にありません" in capsys.readouterr().err


def test_cli_stall_signal_lands(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_EV_FIELDS",
                       "moved:効いたか:string:効いた|効いてない\n")
    monkeypatch.setenv("BEACON_TC_STALL_SIGNAL", "moved:効いてない:2")
    cmd_target.cmd_target_class_update()
    assert _desc(proj)["stall_signal"] == {"evidence_field": "moved",
                                           "value": "効いてない",
                                           "threshold": 2}
