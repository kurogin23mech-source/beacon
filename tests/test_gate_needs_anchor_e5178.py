"""ms-144 e-5178 — 空ゲート (発火源 未紐づけ) の可視化.

Two surfaces make an unanchored前進ゲート visible instead of silently stuck (the
cairn opp-3/opp-4 症状 where a判定日 was placed but nothing fires the judgement):

  1. ``sales_entities.gate_needs_anchor`` — the derived「open gate かつ anchor 空」
     fact (twin of ``needs_transition_date``);
  2. ``opportunity list --json`` carries it per opp as ``gate_needs_anchor``;
  3. ``opportunity transition-date`` warns to stderr (never blocks) when a date is
     placed on a gate that still has no発火源.
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


# --- 1. gate_needs_anchor unit -------------------------------------------

def test_gate_needs_anchor_true_when_open_gate_unanchored():
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    assert se.gate_needs_anchor(data, opp) is True


def test_gate_needs_anchor_false_once_anchored():
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    act = se.activity_add(data, opp, "call", deadline="2026-09-01")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    assert se.gate_needs_anchor(data, opp) is False


def test_gate_needs_anchor_false_when_no_open_gate():
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.jump_transition(data, opp, "不成立", at="T1")  # terminal → no gate
    assert se.gate_needs_anchor(data, opp) is False


def test_gate_needs_anchor_false_for_unknown_or_non_opp():
    data = se.build_sales_project("S", "obj")
    assert se.gate_needs_anchor(data, "opp-999") is False
    assert se.gate_needs_anchor(data, "acc-1") is False


# --- 2. opportunity list --json carries the flag -------------------------

def test_list_json_includes_gate_needs_anchor(tmp_path, monkeypatch, capsys):
    data = se.build_sales_project("S", "obj")
    unanchored = se.opportunity_add(data, "Unanchored", created_at="T0")
    anchored = se.opportunity_add(data, "Anchored", created_at="T0")
    act = se.activity_add(data, anchored, "call", deadline="2026-09-01")
    se.anchor_opportunity_gate(data, anchored, act, at="T1")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_JSON", "1")

    commands.cmd_opportunity_list()
    rows = json.loads(capsys.readouterr().out)
    flags = {r["id"]: r["gate_needs_anchor"] for r in rows}
    assert flags[unanchored] is True
    assert flags[anchored] is False


# --- 3. transition-date warns (non-blocking) on an unanchored gate -------

def test_transition_date_warns_when_gate_unanchored(tmp_path, monkeypatch, capsys):
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_TRANSITION_DATE", "2026-09-01")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "")

    commands.cmd_opportunity_transition_date()
    captured = capsys.readouterr()
    # date is still set (permissive — not blocked) but a warning points to anchor.
    assert "2026-09-01" in captured.out
    assert "発火源 未紐づけ" in captured.err
    assert "beacon opportunity anchor" in captured.err


def test_transition_date_no_warning_once_anchored(tmp_path, monkeypatch, capsys):
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    act = se.activity_add(data, opp, "call", deadline="2026-08-20")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_TRANSITION_DATE", "2026-09-01")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "")

    commands.cmd_opportunity_transition_date()
    captured = capsys.readouterr()
    assert "発火源 未紐づけ" not in captured.err


def test_list_text_gate_display_single_source(tmp_path, monkeypatch, capsys):
    """The human-readable list builds「空 (発火源 未紐づけ)」/「確定」off the one
    gate_needs_anchor predicate + the shared label constant (e-5178 maint-a/c),
    not a second inline anchor check."""
    data = se.build_sales_project("S", "obj")
    se.opportunity_add(data, "Unanchored", created_at="T0")
    anchored = se.opportunity_add(data, "Anchored", created_at="T0")
    act = se.activity_add(data, anchored, "call", deadline="2026-09-01")
    se.anchor_opportunity_gate(data, anchored, act, at="T1")
    _project(tmp_path, monkeypatch, data)

    commands.cmd_opportunity_list()
    out = capsys.readouterr().out
    assert f"空 ({se.GATE_UNANCHORED_LABEL})" in out
    assert f"確定 (発火源 {act})" in out


def test_transition_date_clear_does_not_warn(tmp_path, monkeypatch, capsys):
    """Clearing a date (not placing one) never warns — the warning is about
    placing a judgement date on a gate that can't fire it."""
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_TRANSITION_DATE", "")  # clear
    monkeypatch.setenv("BEACON_PHASE_NOTE", "")

    commands.cmd_opportunity_transition_date()
    captured = capsys.readouterr()
    assert "発火源 未紐づけ" not in captured.err
