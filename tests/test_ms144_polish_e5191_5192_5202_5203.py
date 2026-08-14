"""ms-144 polish (PR #639 / #636 独立レビュー follow-up).

Four related cleanups on the opportunity前進ゲート / anchor surface:

  * e-5191: the mtg-/act-/nrt- prefix-dispatch + "not a work item" error live in
    ONE place (``WORK_ITEM_PREFIXES`` / ``is_work_item_id`` / ``resolve_work_item``)
    instead of being copy-pasted across anchor / firing / find paths.
  * e-5192: the two anchor entry points fold onto one bind path — the meeting-confirm
    auto path (``_anchor_open_gate_to_meeting``) now delegates to the public verb
    ``anchor_opportunity_gate`` so its ownership invariant applies there too.
  * e-5202: the「空ゲート (発火源 未紐づけ)」warning fires at EVERY verb exit where an
    empty gate is born (advance / jump / add / transition-date), predicate-based.
  * e-5203: ``opportunity list --json`` projects ``needs_transition_date`` symmetric
    to ``gate_needs_anchor`` (the cockpit reads both twins as facts).
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


def _project(tmp_path, monkeypatch, data: dict) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


# --- e-5191: single prefix-dispatch --------------------------------------

def test_work_item_prefixes_constant():
    # the value domain lives in one tuple; the opportunity-anchor subset excludes nrt-.
    assert se.WORK_ITEM_PREFIXES == ("mtg-", "act-", "nrt-")
    assert se.OPPORTUNITY_ANCHOR_PREFIXES == ("mtg-", "act-")


def test_is_work_item_id():
    assert se.is_work_item_id("mtg-1") is True
    assert se.is_work_item_id("act-9") is True
    assert se.is_work_item_id("nrt-3") is True
    assert se.is_work_item_id("opp-1") is False
    assert se.is_work_item_id("acc-1") is False
    assert se.is_work_item_id("") is False
    assert se.is_work_item_id(None) is False


def test_resolve_work_item_dispatches_by_kind():
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    act = se.activity_add(data, opp, "call", deadline="2026-09-01")
    mtg = se.meeting_schedule(data, opp, "2026-09-02T10:00:00Z", at="T1")
    # activity resolves to (owner opp, item, kind)
    owner, item, kind = se.resolve_work_item(data, act)
    assert kind == "activity" and item is not None and owner["id"] == opp
    # meeting resolves to kind meeting
    owner, item, kind = se.resolve_work_item(data, mtg)
    assert kind == "meeting" and item is not None
    # a non-work-item id → (None, None, "")
    assert se.resolve_work_item(data, opp) == (None, None, "")


def test_find_work_item_stays_act_nrt_only():
    # e-5191 invariance: find_work_item still returns (None, None) for a meeting,
    # even though resolve_work_item can resolve one (narrower contract preserved).
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    mtg = se.meeting_schedule(data, opp, "2026-09-02T10:00:00Z", at="T1")
    assert se.find_work_item(data, mtg) == (None, None)
    act = se.activity_add(data, opp, "call", deadline="2026-09-01")
    owner, item = se.find_work_item(data, act)
    assert item is not None and owner["id"] == opp


def test_anchor_gate_error_wording_unchanged():
    # the "not a work item" error still names all three prefixes.
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    gate = se.current_gate(data, opp)
    with pytest.raises(ValueError, match=r"mtg-/act-/nrt-"):
        se.anchor_gate(data, gate["id"], "opp-x")


# --- e-5192: one bind path, ownership invariant on both ------------------

def test_meeting_auto_anchor_enforces_ownership():
    """_anchor_open_gate_to_meeting now delegates to anchor_opportunity_gate, so a
    meeting that belongs to ANOTHER opportunity can no longer be anchored onto this
    deal's gate (before the fold it rode anchor_gate, which skipped ownership)."""
    data = se.build_sales_project("S", "obj")
    opp_a = se.opportunity_add(data, "A", created_at="T0")
    opp_b = se.opportunity_add(data, "B", created_at="T0")
    mtg_a = se.meeting_schedule(data, opp_a, "2026-09-02T10:00:00Z", at="T1")
    # opp_b has its own open (empty) gate; anchoring opp_a's meeting must be refused.
    with pytest.raises(ValueError, match="belongs to"):
        se._anchor_open_gate_to_meeting(data, opp_b, mtg_a, at="T2")


def test_meeting_auto_anchor_silent_when_no_open_gate():
    # the auto path keeps its one documented difference: no open gate → silent no-op
    # (not a raise like the public verb), because it is a passive side-effect.
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.jump_transition(data, opp, "不成立", at="T1")  # terminal → no open gate
    mtg = se.meeting_schedule(data, opp, "2026-09-02T10:00:00Z", at="T2")
    # must not raise
    se._anchor_open_gate_to_meeting(data, opp, mtg, at="T3")


def test_meeting_schedule_set_transition_anchors_own_gate():
    # regression: the normal owned-meeting auto-anchor path still binds the gate.
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    mtg = se.meeting_schedule(data, opp, "2026-09-02T10:00:00Z",
                              set_transition=True, at="T1")
    gate = se.current_gate(data, opp)
    assert gate["anchor"] == mtg
    assert se.gate_needs_anchor(data, opp) is False


# --- e-5203: symmetric twin projection -----------------------------------

def test_list_json_projects_needs_transition_date_symmetric(tmp_path, monkeypatch, capsys):
    data = se.build_sales_project("S", "obj")
    dateless = se.opportunity_add(data, "Dateless", created_at="T0")
    dated = se.opportunity_add(data, "Dated", created_at="T0")
    se.set_transition_date(data, dated, "2026-09-01", at="T1")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_JSON", "1")

    commands.cmd_opportunity_list()
    rows = json.loads(capsys.readouterr().out)
    by_id = {r["id"]: r for r in rows}
    # both twins are present on every row (symmetry with gate_needs_anchor).
    for r in rows:
        assert "needs_transition_date" in r and "gate_needs_anchor" in r
    assert by_id[dateless]["needs_transition_date"] is True
    assert by_id[dated]["needs_transition_date"] is False


# --- e-5202: empty-gate warning on all born-empty-gate exits -------------

def test_advance_warns_unanchored_gate(tmp_path, monkeypatch, capsys):
    # Advancing opens a fresh gate for the new phase. When the new phase's template
    # has no meeting to auto-anchor, that gate is born empty → the prompt must fire on
    # the advance exit (e-5202). We advance until we land on a phase whose fresh gate
    # is genuinely unanchored, then assert the warning appeared on THAT advance.
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    act = se.activity_add(data, opp, "call", deadline="2026-08-20")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_JUDGE_DECISION", "advance")
    monkeypatch.setenv("BEACON_JUDGE_ARG", "")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "adv")

    warned = False
    for _ in range(4):  # walk forward through the funnel
        data_now = commands.load_project()
        if se.opportunity_phase_is_terminal(data_now, se.find_opportunity(data_now, opp).get("phase", "")):
            break
        commands.cmd_opportunity_judge()
        err = capsys.readouterr().err
        reloaded = commands.load_project()
        if se.gate_needs_anchor(reloaded, opp):
            # this advance birthed an empty gate → the prompt must have fired
            assert "発火源 未紐づけ" in err
            warned = True
            break
        # else the new phase auto-anchored a seeded meeting; anchor is set, advance on
    assert warned, "expected at least one advance into a no-anchor phase to warn"


def test_warn_helper_is_predicate_based(tmp_path, monkeypatch, capsys):
    """The add exit (and every other) rides ``_warn_gate_unanchored``, which warns
    IFF ``gate_needs_anchor`` — silent once anchored. Tested on the helper directly
    so it is deterministic regardless of a funnel's entry-phase anchor template."""
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    # unanchored open gate → warns
    assert se.gate_needs_anchor(data, opp) is True
    commands._warn_gate_unanchored(data, opp)
    assert "発火源 未紐づけ" in capsys.readouterr().err
    # once anchored → silent
    act = se.activity_add(data, opp, "call", deadline="2026-08-20")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    commands._warn_gate_unanchored(data, opp)
    assert "発火源 未紐づけ" not in capsys.readouterr().err


def test_jump_warns_unanchored_gate(tmp_path, monkeypatch, capsys):
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    names = [p["name"] for p in se.opportunity_phases(data)
             if not se.opportunity_phase_is_terminal(data, p["name"])]
    target = names[-1] if names else ""
    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_PHASE", target)
    monkeypatch.setenv("BEACON_PHASE_NOTE", "jump")
    commands.cmd_opportunity_phase()
    err = capsys.readouterr().err
    assert "発火源 未紐づけ" in err
