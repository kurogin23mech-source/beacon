"""ms-146 e-5339: the mechanism surfaces "そろそろ切り上げでは？" where the owner
actually looks — `beacon status` (human + JSON, the latter is what
/beacon-session-start reads) and `beacon target instances`.

The signal has to reach the person who cannot stop, and that person is precisely
the one who will not go looking for it. So it rides the status screen they open
anyway — but ONLY when something actually trips, because a permanently-present
"signals" section is one the reader learns to skim past.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_milestone  # noqa: E402
import cmd_target  # noqa: E402
import occupation  # noqa: E402
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
    "phases": [{"key": "started", "label": "着手", "fields": [
                   {"key": "time_budget_h", "label": "時間予算",
                    "type": "number"}]},
               {"key": "enough", "label": "十分やった", "terminal": True}],
    "work_item_fields": [{"key": "budget_h", "label": "予算",
                          "type": "number"}],
    "evidence_fields": [
        {"key": "moved", "label": "効いたか", "type": "string",
         "choices": ["効いた", "効いてない", "わからない"]},
        {"key": "spent_h", "label": "消費時間", "type": "number"}],
    "budget_tracking": {"target_budget_field": "time_budget_h",
                        "work_item_budget_field": "budget_h",
                        "evidence_spend_field": "spent_h"},
    "stall_signal": {"evidence_field": "moved", "value": "効いてない",
                     "threshold": 2},
}


def _project(undertakings):
    return {"name": "t", "milestones": [], "target_classes": [UNDERTAKING],
            "undertakings": undertakings}


def _rec(**over):
    base = {"id": "ut-1", "label": "提案書を書く", "kind": "undertaking",
            "phase": "started", "status": "todo", "time_budget_h": "3",
            "work_items": [], "evidence": [], "phase_history": []}
    base.update(over)
    return base


def _spend(amount, moved="効いた", linked=""):
    return {"id": f"ut-1-ev{amount}", "spent_h": str(amount), "moved": moved,
            "linked_id": linked}


# ---------------------------------------------------------------------------
# The aggregate read.
# ---------------------------------------------------------------------------

def test_no_rows_when_nothing_trips():
    """Silence is the normal state — that is what makes a row meaningful."""
    data = _project([_rec(evidence=[_spend(1)])])
    assert occupation.stop_signal_rows(data) == []


def test_a_target_over_its_budget_shows_up():
    data = _project([_rec(evidence=[_spend(5)])])
    rows = occupation.stop_signal_rows(data)
    assert len(rows) == 1
    assert rows[0]["id"] == "ut-1"
    assert rows[0]["label"] == "提案書を書く"
    assert {s["kind"] for s in rows[0]["signals"]} == {"budget_target"}


def test_a_stalled_target_shows_up():
    data = _project([_rec(evidence=[
        _spend(1, "効いてない"), _spend(2, "効いてない")])])
    rows = occupation.stop_signal_rows(data)
    assert any(s["kind"] == "stall" for s in rows[0]["signals"])


def test_a_finished_target_is_not_told_to_stop():
    """Telling someone to stop work they already stopped is noise."""
    data = _project([_rec(status="done", evidence=[_spend(9)])])
    assert occupation.stop_signal_rows(data) == []


def test_a_cancelled_target_is_not_told_to_stop():
    data = _project([_rec(status="cancelled", evidence=[_spend(9)])])
    assert occupation.stop_signal_rows(data) == []


def test_built_in_classes_contribute_nothing():
    """A milestone declares no budget / stall tracking, so it is not force-fitted."""
    data = {"name": "t", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                            "entries": []}]}
    assert occupation.stop_signal_rows(data) == []


# ---------------------------------------------------------------------------
# The projection carries it, so every renderer reads one shape.
# ---------------------------------------------------------------------------

def test_the_projection_carries_the_signals():
    rec = _rec(evidence=[_spend(5)])
    detail = te.project_target(UNDERTAKING, rec)["detail"]
    assert [s["kind"] for s in detail["stop_signals"]] == ["budget_target"]


def test_the_projection_carries_an_empty_list_when_quiet():
    rec = _rec(evidence=[_spend(1)])
    assert te.project_target(UNDERTAKING, rec)["detail"]["stop_signals"] == []


# ---------------------------------------------------------------------------
# CLI: status (human + JSON) and instances.
# ---------------------------------------------------------------------------

@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(_project([_rec(
            work_items=[{"id": "ut-1-w2", "description": "打ち手を書く",
                         "status": "todo", "budget_h": "2"}],
            evidence=[_spend(4, "効いてない", "ut-1-w2"),
                      _spend(2, "効いてない", "ut-1-w2")])]),
            ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in ("BEACON_JSON", "BEACON_TARGET_CLASS", "BEACON_MS_IDS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def quiet_proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(_project([_rec(evidence=[_spend(1)])]), ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in ("BEACON_JSON", "BEACON_TARGET_CLASS", "BEACON_MS_IDS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_status_prints_the_section_when_something_trips(proj, capsys):
    cmd_milestone.cmd_milestone_list()
    out = capsys.readouterr().out
    assert "そろそろ切り上げでは？" in out
    assert "ut-1" in out and "提案書を書く" in out
    assert "時間予算" in out
    assert "効いてない" in out


def test_status_stays_silent_when_nothing_trips(quiet_proj, capsys):
    """A section that is always there is a section the reader stops seeing."""
    cmd_milestone.cmd_milestone_list()
    assert "そろそろ切り上げでは？" not in capsys.readouterr().out


def test_status_never_moves_or_closes_the_target(proj):
    """設計方針2: the mechanism reports, the human decides. Read-only."""
    before = (proj / ".beacon" / "project.json").read_text(encoding="utf-8")
    cmd_milestone.cmd_milestone_list()
    assert (proj / ".beacon" / "project.json").read_text(encoding="utf-8") \
        == before


def test_status_json_carries_the_signals_for_session_start(proj, monkeypatch,
                                                           capsys):
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_milestone.cmd_milestone_list()
    payload = json.loads(capsys.readouterr().out)
    assert "stop_signals" in payload
    assert payload["stop_signals"][0]["id"] == "ut-1"
    kinds = {s["kind"] for s in payload["stop_signals"][0]["signals"]}
    assert "stall" in kinds


def test_status_json_key_is_present_but_empty_when_quiet(quiet_proj,
                                                         monkeypatch, capsys):
    """The key always exists so a reader never has to guess between "quiet" and
    "this version does not report signals"."""
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_milestone.cmd_milestone_list()
    assert json.loads(capsys.readouterr().out)["stop_signals"] == []


def test_instances_shows_the_signals_next_to_the_target(proj, monkeypatch,
                                                        capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    cmd_target.cmd_target_instances()
    out = capsys.readouterr().out
    assert "⚠" in out
    assert "効いてない" in out
