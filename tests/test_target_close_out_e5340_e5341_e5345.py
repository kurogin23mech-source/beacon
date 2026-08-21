"""ms-146 — the three remaining pieces of the executive class engine:

  * e-5345: the line you drew at the start is on screen at the moment you claim
    you have met it. A 照合 demanded while the line is off-screen is a check
    against memory and mood — the exact faculty the line exists to replace.
  * e-5341: no phase may sit above the terminal one. Phases are an ordered
    ladder, so the top rung is what the engine tells its owner to climb toward.
  * e-5340: an idea that arrives mid-work becomes its own target, cheaply, and
    the thing you were already doing is left completely alone.
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


UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [{"key": "purpose", "label": "上位目的", "type": "string",
                "required": True}],
    "phases": [
        {"key": "not_started", "label": "やってない"},
        {"key": "started", "label": "着手", "fields": [
            {"key": "enough_line", "label": "十分ライン", "type": "text",
             "required": True},
            {"key": "time_budget_h", "label": "時間予算", "type": "number"}]},
        {"key": "enough", "label": "十分やった", "terminal": True, "fields": [
            {"key": "enough_check", "label": "照合結果", "type": "text",
             "required": True},
            {"key": "overrun_note", "label": "超過してやったこと",
             "type": "text"}]},
    ],
    "evidence_fields": [{"key": "spent_h", "label": "消費時間",
                         "type": "number"}],
    "budget_tracking": {"target_budget_field": "time_budget_h",
                        "work_item_budget_field": "budget_h",
                        "evidence_spend_field": "spent_h"},
}


def _started(spent=None):
    data = {"name": "t"}
    rec = te.create_target(data, UNDERTAKING, label="セミナー原稿",
                           fields={"purpose": "9月セミナーで成約3件"})
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="started",
                      fields={"enough_line": "事例3本まで。それ以上は磨かない",
                              "time_budget_h": "4"})
    if spent is not None:
        te.add_evidence(data, UNDERTAKING, rec["id"],
                        fields={"spent_h": str(spent)})
    return data, rec


# ---------------------------------------------------------------------------
# e-5345 — the reference the 照合 is against.
# ---------------------------------------------------------------------------

def test_reference_carries_what_was_decided_earlier():
    data, rec = _started()
    ref = te.completion_reference(UNDERTAKING, rec, "enough")
    by_key = {r["key"]: r["value"] for r in ref}
    assert by_key["purpose"] == "9月セミナーで成約3件"
    assert by_key["enough_line"] == "事例3本まで。それ以上は磨かない"
    assert by_key["time_budget_h"] == "4"


def test_reference_excludes_the_phase_being_entered():
    """Those are what the owner is writing right now, not what they are held to."""
    data, rec = _started()
    rec["enough_check"] = "書いた"
    keys = {r["key"] for r in te.completion_reference(UNDERTAKING, rec, "enough")}
    assert "enough_check" not in keys
    assert "overrun_note" not in keys


def test_reference_skips_fields_with_no_value():
    data = {"name": "t"}
    rec = te.create_target(data, UNDERTAKING, label="x",
                           fields={"purpose": "p"})
    keys = {r["key"] for r in te.completion_reference(UNDERTAKING, rec, "enough")}
    assert keys == {"purpose"}


def test_next_phase_after_reports_where_a_bare_advance_lands():
    data, rec = _started()
    assert te.next_phase_after(UNDERTAKING, rec) == "enough"


# --- CLI ---

_ENV = ("BEACON_TARGET_CLASS", "BEACON_TARGET_ID", "BEACON_TO_PHASE",
        "BEACON_FIELDS", "BEACON_REASON", "BEACON_LABEL", "BEACON_JSON")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    data, rec = _started(spent=5.5)
    data.update({"milestones": [], "target_classes": [UNDERTAKING]})
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_a_failed_terminal_advance_still_shows_the_line(proj, monkeypatch,
                                                        capsys):
    """The first attempt usually FAILS on the missing 照合 field — and that is
    precisely when the owner needs the line in front of them."""
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_TO_PHASE", "enough")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_advance()
    out = capsys.readouterr().out
    assert "照合の材料" in out
    assert "事例3本まで" in out


def test_the_overrun_is_shown_next_to_the_line(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_TO_PHASE", "enough")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_advance()
    out = capsys.readouterr().out
    assert "5.5h" in out
    assert "超過" in out


def test_a_successful_terminal_advance_shows_it_too(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_TO_PHASE", "enough")
    monkeypatch.setenv("BEACON_FIELDS", "enough_check=事例2本で止めた\n")
    cmd_target.cmd_target_advance()
    out = capsys.readouterr().out
    assert "照合の材料" in out
    assert "フェーズ進行" in out


def test_a_non_terminal_advance_does_not_show_it(tmp_path, monkeypatch,
                                                 capsys):
    """Showing it on every step would make it wallpaper."""
    data = {"name": "t", "milestones": [], "target_classes": [UNDERTAKING]}
    rec = te.create_target(data, UNDERTAKING, label="x",
                           fields={"purpose": "p"})
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", rec["id"])
    monkeypatch.setenv("BEACON_TO_PHASE", "started")
    monkeypatch.setenv("BEACON_FIELDS", "enough_line=L\n")
    cmd_target.cmd_target_advance()
    assert "照合の材料" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# e-5341 — nothing above the terminal.
# ---------------------------------------------------------------------------

def _cls(phases):
    return {"kind": "x", "label": "X", "profession": "dev",
            "type": "single-shot", "id_prefix": "x-", "collection": "xs",
            "phases": phases}


def test_a_phase_after_the_terminal_is_refused():
    problems = td.validate_descriptor(_cls([
        {"key": "started", "label": "着手"},
        {"key": "enough", "label": "十分やった", "terminal": True},
        {"key": "plus", "label": "プラスアルファやった"}]))
    assert problems
    assert any("plus" in p and "終端" in p for p in problems), problems


def test_the_refusal_explains_what_to_do_instead():
    problems = td.validate_descriptor(_cls([
        {"key": "enough", "label": "十分", "terminal": True},
        {"key": "plus", "label": "プラス"}]))
    assert any("field として記録" in p for p in problems), problems


def test_the_terminal_being_last_is_fine():
    assert td.validate_descriptor(_cls([
        {"key": "started", "label": "着手"},
        {"key": "enough", "label": "十分やった", "terminal": True}])) == []


def test_a_class_with_no_terminal_is_unaffected():
    assert td.validate_descriptor(_cls([
        {"key": "a", "label": "A"}, {"key": "b", "label": "B"}])) == []


def test_the_shipped_undertaking_class_satisfies_the_rule():
    """The class this MS ships must itself obey the shape it enforces, and its
    overrun note must live ON the terminal phase — a record kept after finishing,
    never a rung above it."""
    assert td.validate_descriptor(UNDERTAKING) == []
    assert td.phase_keys(UNDERTAKING)[-1] == "enough"
    assert td.terminal_phase_keys(UNDERTAKING) == ["enough"]
    terminal = td.get_phase(UNDERTAKING, "enough")
    assert any(f["key"] == "overrun_note" for f in terminal["fields"])


# ---------------------------------------------------------------------------
# e-5340 — split a passing thought out.
# ---------------------------------------------------------------------------

def test_split_creates_a_new_target_and_records_where_it_came_from():
    data, rec = _started()
    new = te.split_target(data, UNDERTAKING, rec["id"],
                          label="既存パンフレットも直したくなった")
    assert new["id"] != rec["id"]
    assert new[te.SPLIT_FROM_KEY] == rec["id"]
    assert new["label"] == "既存パンフレットも直したくなった"


def test_split_inherits_the_objective_so_it_is_one_command():
    """A thought that arrives while serving an objective almost always serves the
    same one — inheriting it is what keeps the alternative cheaper than the
    mistake it replaces."""
    data, rec = _started()
    new = te.split_target(data, UNDERTAKING, rec["id"], label="思いつき")
    assert new["purpose"] == "9月セミナーで成約3件"


def test_split_leaves_the_origin_completely_alone():
    """The line you drew at the start has to survive the idea that arrived after."""
    data, rec = _started()
    te.add_work_item(data, UNDERTAKING, rec["id"], "構成を起こす")
    before = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    te.split_target(data, UNDERTAKING, rec["id"], label="思いつき")
    after = json.dumps(te.find_target(data, UNDERTAKING, rec["id"]),
                       ensure_ascii=False, sort_keys=True)
    assert before == after


def test_the_split_starts_at_the_first_phase():
    data, rec = _started()
    new = te.split_target(data, UNDERTAKING, rec["id"], label="思いつき")
    assert te.current_phase(new) == "not_started"


def test_split_fields_override_the_inherited_ones():
    data, rec = _started()
    new = te.split_target(data, UNDERTAKING, rec["id"], label="別目的の思いつき",
                          fields={"purpose": "来期の採用"})
    assert new["purpose"] == "来期の採用"


def test_splitting_from_an_unknown_target_raises():
    data, _ = _started()
    with pytest.raises(te.TargetEngineError):
        te.split_target(data, UNDERTAKING, "ut-9", label="x")


def test_the_provenance_is_findable_later():
    data, rec = _started()
    new = te.split_target(data, UNDERTAKING, rec["id"], label="思いつき")
    proj = te.project_target(UNDERTAKING, new)
    assert proj["detail"]["split_from"] == rec["id"]
    plain = te.project_target(UNDERTAKING, rec)
    assert plain["detail"]["split_from"] == ""


def test_cli_split(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_LABEL", "パンフレットも直したくなった")
    cmd_target.cmd_target_split()
    stored = json.loads((proj / ".beacon" / "project.json")
                        .read_text(encoding="utf-8"))["undertakings"]
    assert len(stored) == 2
    assert stored[1]["split_from"] == "ut-1"
    out = capsys.readouterr().out
    assert "切り出し" in out
    assert "変えていません" in out


def test_cli_split_requires_a_label(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_split()
