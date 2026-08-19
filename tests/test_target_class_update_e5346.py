"""ms-146 e-5346: an already-declared target-class can gain new field
declarations — additive only.

WHY the additive-only shape is the subject of most of these tests: a declaration
is not a schema over an empty database, it is a promise about records that
already exist. Renaming a key orphans every value written under the old name;
removing one orphans the values themselves. Both are data loss wearing the
costume of an edit. So the tests pin (a) adding works on every arm, (b) the
destructive verbs are refused BY NAME with the reason, and (c) a newly-required
field does not retroactively invalidate older records — and says so.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_target  # noqa: E402
import target_descriptor as td  # noqa: E402


def _class():
    return {
        "kind": "undertaking",
        "label": "やること",
        "profession": "dev",
        "type": "single-shot",
        "id_prefix": "ut-",
        "collection": "undertakings",
        "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
        "fields": [{"key": "purpose", "label": "上位目的", "type": "string"}],
        "phases": [{"key": "not_started", "label": "やってない"},
                   {"key": "started", "label": "着手"},
                   {"key": "enough", "label": "十分やった", "terminal": True}],
    }


_ENV = ("BEACON_TC_KIND", "BEACON_TC_FIELDS", "BEACON_TC_REQUIRED_FIELDS",
        "BEACON_TC_WI_FIELDS", "BEACON_TC_REQUIRED_WI_FIELDS",
        "BEACON_TC_EV_FIELDS", "BEACON_TC_REQUIRED_EV_FIELDS",
        "BEACON_TC_PHASE_FIELDS", "BEACON_TC_REQUIRED_PHASE_FIELDS",
        "BEACON_TC_REMOVE_FIELDS", "BEACON_TC_RENAME_FIELDS")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "milestones": [],
        "target_classes": [_class()],
        "undertakings": [{
            "id": "ut-1", "label": "セミナー準備", "kind": "undertaking",
            "phase": "started", "status": "todo",
            "work_items": [{"id": "ut-1-w1", "description": "原稿",
                            "status": "todo"}],
            "evidence": [{"id": "ut-1-ev1", "summary": "通し1本"}],
            "phase_history": []}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _desc(proj_path):
    data = json.loads((proj_path / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))
    return data["target_classes"][0]


# ---------------------------------------------------------------------------
# Pure layer.
# ---------------------------------------------------------------------------

def test_add_field_appends_to_each_arm():
    d = _class()
    assert td.add_field(d, {"key": "a", "label": "A"},
                        arm=td.FIELD_ARM_BASE) == []
    assert td.add_field(d, {"key": "budget_h", "label": "予算",
                            "type": "number"},
                        arm=td.FIELD_ARM_WORK_ITEM) == []
    assert td.add_field(d, {"key": "moved", "label": "効いたか"},
                        arm=td.FIELD_ARM_EVIDENCE) == []
    assert td.add_field(d, {"key": "enough_line", "label": "十分ライン",
                            "type": "text"},
                        arm=td.FIELD_ARM_PHASE, phase_key="started") == []
    assert [f["key"] for f in td.base_fields(d)] == ["purpose", "a"]
    assert [f["key"] for f in td.work_item_fields(d)] == ["budget_h"]
    assert [f["key"] for f in td.evidence_fields(d)] == ["moved"]
    # fields_at_phase merges base + that phase's own extension, in that order
    assert [f["key"] for f in td.fields_at_phase(d, "started")] \
        == ["purpose", "a", "enough_line"]


def test_add_field_refuses_a_duplicate_key_on_the_same_arm():
    """Re-declaring is a redefine in disguise: the new declaration would win for
    readers while records written under the old one keep the old meaning."""
    d = _class()
    assert td.add_field(d, {"key": "purpose", "label": "again"}) != []
    assert len(td.base_fields(d)) == 1, "nothing may be written on refusal"


def test_a_phase_field_may_not_shadow_a_base_field():
    d = _class()
    problems = td.add_field(d, {"key": "purpose", "label": "x"},
                            arm=td.FIELD_ARM_PHASE, phase_key="started")
    assert problems
    assert td.get_phase(d, "started").get("fields") in (None, [])


def test_add_field_refuses_an_unknown_phase():
    d = _class()
    assert td.add_field(d, {"key": "x", "label": "X"},
                        arm=td.FIELD_ARM_PHASE, phase_key="nope") != []


def test_add_field_refuses_an_unknown_type():
    d = _class()
    assert td.add_field(d, {"key": "x", "label": "X", "type": "blob"}) != []
    assert len(td.base_fields(d)) == 1


def test_records_missing_field_counts_targets_and_children():
    d = _class()
    data = {"undertakings": [
        {"id": "ut-1", "purpose": "成約",
         "work_items": [{"id": "w1"}, {"id": "w2", "budget_h": "2"}],
         "evidence": [{"id": "ev1"}]},
        {"id": "ut-2", "work_items": [], "evidence": []},
    ]}
    assert td.records_missing_field(data, d, "purpose") == 1
    assert td.records_missing_field(data, d, "budget_h",
                                    arm=td.FIELD_ARM_WORK_ITEM) == 1
    assert td.records_missing_field(data, d, "moved",
                                    arm=td.FIELD_ARM_EVIDENCE) == 1


# ---------------------------------------------------------------------------
# CLI layer.
# ---------------------------------------------------------------------------

def test_cli_adds_a_required_work_item_field(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_REQUIRED_WI_FIELDS",
                       "budget_h:時間予算(時間):number\n")
    cmd_target.cmd_target_class_update()
    assert [f["key"] for f in td.work_item_fields(_desc(proj))] == ["budget_h"]
    out = capsys.readouterr().out
    assert "budget_h" in out and "[必須]" in out
    # the retroactivity consequence is STATED, not discovered later
    assert "既存 1 件" in out and "遡って無効にはしません" in out


def test_cli_adds_a_phase_field(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_REQUIRED_PHASE_FIELDS",
                       "started:enough_line:十分ライン:text\n")
    cmd_target.cmd_target_class_update()
    keys = [f["key"] for f in td.fields_at_phase(_desc(proj), "started")]
    assert "enough_line" in keys
    assert "phase 'started'" in capsys.readouterr().out


def test_cli_applies_all_or_nothing(proj, monkeypatch):
    """A typo in the second flag must not leave the class half-updated."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_WI_FIELDS",
                       "budget_h:予算:number\nbad_one:X:blob\n")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_class_update()
    assert e.value.code != 0
    assert td.work_item_fields(_desc(proj)) == [], \
        "the first field must not survive a later failure"


def test_cli_refuses_remove_by_name_with_the_reason(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_REMOVE_FIELDS", "purpose\n")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    err = capsys.readouterr().err
    assert "--remove-field は提供していません" in err
    assert "データ消失" in err, "the refusal must carry its reason"


def test_cli_refuses_rename_by_name_with_the_reason(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_RENAME_FIELDS", "purpose:goal\n")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    assert "--rename-field は提供していません" in capsys.readouterr().err


def test_cli_refuses_an_unknown_class_and_names_the_declared_ones(
        proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_KIND", "nope")
    monkeypatch.setenv("BEACON_TC_FIELDS", "a:A:string\n")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_class_update()
    err = capsys.readouterr().err
    assert "undertaking" in err


def test_cli_refuses_an_empty_update(proj, monkeypatch):
    """Nothing to add is a mistake, not a successful no-op."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_class_update()
    assert e.value.code != 0


def test_existing_records_survive_the_update(proj, monkeypatch):
    """The whole point of e-5346: updating must not orphan what already exists."""
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_REQUIRED_WI_FIELDS", "budget_h:予算:number\n")
    cmd_target.cmd_target_class_update()
    data = json.loads((proj / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))
    rec = data["undertakings"][0]
    assert rec["id"] == "ut-1"
    assert rec["work_items"][0]["id"] == "ut-1-w1"
    assert rec["evidence"][0]["id"] == "ut-1-ev1"
