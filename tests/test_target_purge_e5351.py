"""ms-146 e-5351: `beacon target purge` physically removes a data-defined target
instance.

WHY a hard delete exists at all, when the repo's default is "never lose the
trail": purge is for records that should never have lived here. Data migrated to
another project, or personal records sitting inside a tool's own repository, are
not made safer by leaving a tombstone — for those, keeping the evidence is the
opposite of the goal. So the trail purge leaves is the operator's REASON, which
the CLI requires, not the payload.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_target  # noqa: E402
import target_engine as te  # noqa: E402


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
}


def _rec(rid, label, items=0, evs=0):
    return {"id": rid, "label": label, "kind": "undertaking",
            "phase": "started", "status": "todo", "phase_history": [],
            "work_items": [{"id": f"{rid}-w{i}", "description": f"w{i}",
                            "status": "todo"} for i in range(1, items + 1)],
            "evidence": [{"id": f"{rid}-ev{i}", "summary": f"e{i}"}
                         for i in range(1, evs + 1)]}


def _data():
    return {"name": "t", "milestones": [], "target_classes": [UNDERTAKING],
            "undertakings": [_rec("ut-1", "セミナー準備", 3, 1),
                             _rec("ut-2", "セミナー資料を作る", 6, 7),
                             _rec("ut-3", "本番の通し練習を30回やる", 5, 4)]}


# ---------------------------------------------------------------------------
# Engine.
# ---------------------------------------------------------------------------

def test_purge_removes_the_record_from_the_collection():
    data = _data()
    removed = te.purge_target(data, UNDERTAKING, "ut-2")
    assert removed["id"] == "ut-2"
    assert [r["id"] for r in data["undertakings"]] == ["ut-1", "ut-3"]


def test_purge_takes_the_children_with_it():
    """The work items and evidence live inside the record, so they go too —
    that is the whole point when the data has moved elsewhere."""
    data = _data()
    removed = te.purge_target(data, UNDERTAKING, "ut-2")
    assert len(te.list_work_items(removed)) == 6
    assert len(te.list_evidence(removed)) == 7
    assert all("ut-2" not in json.dumps(r, ensure_ascii=False)
               for r in data["undertakings"])


def test_purge_leaves_the_siblings_untouched():
    data = _data()
    te.purge_target(data, UNDERTAKING, "ut-1")
    assert len(te.list_work_items(data["undertakings"][0])) == 6  # ut-2
    assert len(te.list_evidence(data["undertakings"][1])) == 4    # ut-3


def test_purge_does_not_touch_the_class_declaration():
    """e-5334 (社長クラスをデータで宣言) の成果物であるクラス定義は残す。
    Purging instances must never take the declaration with them."""
    data = _data()
    te.purge_target(data, UNDERTAKING, "ut-1")
    te.purge_target(data, UNDERTAKING, "ut-2")
    te.purge_target(data, UNDERTAKING, "ut-3")
    assert data["undertakings"] == []
    assert [t["kind"] for t in data["target_classes"]] == ["undertaking"]


def test_purging_an_unknown_id_raises_and_changes_nothing():
    data = _data()
    with pytest.raises(te.TargetEngineError) as e:
        te.purge_target(data, UNDERTAKING, "ut-9")
    assert "ut-9" in str(e.value)
    assert len(data["undertakings"]) == 3


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

_ENV = ("BEACON_TARGET_CLASS", "BEACON_TARGET_ID", "BEACON_REASON",
        "BEACON_JSON")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(_data(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _stored(proj_path):
    return json.loads((proj_path / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))


def test_cli_purges_with_a_reason(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-2")
    monkeypatch.setenv("BEACON_REASON", "president-28777a へ移設済み")
    cmd_target.cmd_target_purge()
    assert [r["id"] for r in _stored(proj)["undertakings"]] == ["ut-1", "ut-3"]
    out = capsys.readouterr().out
    assert "物理削除" in out
    assert "業務 6 件" in out and "証跡 7 件" in out
    assert "president-28777a" in out


def test_cli_refuses_without_a_reason(proj, monkeypatch, capsys):
    """A physical delete leaves nothing behind to explain itself, so the
    explanation has to be captured at the moment it happens."""
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-2")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_purge()
    assert e.value.code != 0
    assert "--reason" in capsys.readouterr().err
    assert len(_stored(proj)["undertakings"]) == 3, "nothing may be removed"


def test_cli_refuses_an_unknown_id_and_writes_nothing(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-9")
    monkeypatch.setenv("BEACON_REASON", "x")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_purge()
    assert len(_stored(proj)["undertakings"]) == 3


def test_cli_requires_a_target_id(proj, monkeypatch):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_REASON", "x")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_purge()


def test_cli_json_mode(proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-3")
    monkeypatch.setenv("BEACON_REASON", "移設済み")
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_target.cmd_target_purge()
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "ut-3", "kind": "undertaking", "purged": True,
                       "work_items": 5, "evidence": 4, "reason": "移設済み"}


def test_the_audit_row_keeps_the_reason_but_not_the_payload(proj, monkeypatch):
    """Copying a purged target's contents into the changelog would leave behind
    exactly what the operator asked to remove."""
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_REASON", "president-28777a へ移設済み")
    cmd_target.cmd_target_purge()
    log = (proj / ".beacon" / "changelog.jsonl").read_text(encoding="utf-8")
    row = json.loads([ln for ln in log.splitlines() if ln.strip()][-1])
    assert row["op"] == "target_purge"
    assert row["target_id"] == "ut-1"
    assert row["reason"] == "president-28777a へ移設済み"
    assert "セミナー準備" not in log, "the purged label must not survive"
    assert "ut-1-w1" not in log, "the purged children must not survive"
