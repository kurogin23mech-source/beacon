"""ms-180 e-7004 — one door writes "this happened once" in every profession.

Before this, neither write line could take a plain single act from any profession:

  * ``record_target_entry`` records a dev milestone entry and NO-OPs on a sales
    Target — right for its own purpose, useless for sales.
  * ``add_evidence`` resolves its parent through the SALES resolvers and requires
    ``direction`` from the sales vocabulary, so ``record_evidence(data, "ms-180")``
    raised 「Communication target not found」 — even though the manifest already
    declared the dev milestone's own evidence arm. The DECLARATION was generic;
    only the write side was pinned to one profession.

``occupation.record_evidence`` dispatches on **which writer owns the declared
arm**, which is the question that actually decides the record shape. These tests
pin that, and in particular pin the mistake found while building it: ordering the
dispatch on "is there a descriptor for this kind" sent an Opportunity's record
through ``target_engine`` and wrote a target_engine-shaped row into the
``communications`` arm that ``sales_entities`` owns — two shapes in one arm.

They also pin that ``tool`` is a STRUCTURED field on every writer. Recovering
「何でやったか」 by substring-matching a description would be the loose kind of
match this repo keeps getting burned by, and a field present on one recorder
strategy but missing on its sibling silently answers for only some classes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import core             # noqa: E402
import occupation as occ  # noqa: E402
import target_engine as te  # noqa: E402

TOOL = "mcp__gmail__send_email"


def _dev(**overrides) -> dict:
    data = {"id": "p", "profession": "dev", "milestones": [
        {"id": "ms-1", "title": "T", "status": "in_progress", "progress": 0,
         "entries": []}]}
    data.update(overrides)
    return data


def _sales() -> dict:
    return {"id": "p", "profession": "sales",
            "accounts": [{"id": "acc-1", "name": "A社", "nurturings": [],
                          "communications": []}],
            "opportunities": [
                {"id": "opp-1", "label": "A社 商談", "phase": "初回",
                 "status": "in_progress",
                 "activities": [{"id": "act-1", "description": "初回打診",
                                 "status": "todo"}],
                 "communications": []}]}


# ---------------------------------------------------------------------------
# The same call works in both professions.
# ---------------------------------------------------------------------------

def test_dev_milestone_records_through_its_declared_changelog_arm():
    data = _dev()
    result = occ.record_evidence(data, "ms-1", summary="1 通送った", tool=TOOL,
                                 effect="outward")
    assert result["recorded"] and result["via"] == "changelog"
    assert result["arm"] == "entries"
    entry = data["milestones"][0]["entries"][0]
    assert entry["meta"]["tool"] == TOOL


def test_sales_opportunity_records_through_its_declared_evidence_arm():
    data = _sales()
    result = occ.record_evidence(data, "opp-1", summary="初回打診を送った",
                                 tool=TOOL, effect="outward", channel="email")
    assert result["recorded"] and result["via"] == "evidence"
    assert result["arm"] == "communications"
    comm = data["opportunities"][0]["communications"][0]
    assert comm["tool"] == TOOL
    assert comm["summary"] == "初回打診を送った"


def test_the_sales_record_keeps_its_own_shape_not_the_engine_shape():
    """The regression found while building this: descriptor-first dispatch wrote a
    ``target_engine`` row (``opp-1-ev1`` with no direction/channel) into the
    ``communications`` arm. Two record shapes in one arm is corruption, so the
    shape is pinned, not just the fact that something was written."""
    data = _sales()
    occ.record_evidence(data, "opp-1", summary="送った", tool=TOOL,
                        effect="outward", channel="email")
    comm = data["opportunities"][0]["communications"][0]
    assert comm["id"].startswith("comm-")
    assert set(comm) >= {"id", "direction", "channel", "summary", "linked_id",
                         "created_in_phase", "tool"}
    assert data["opportunities"][0].get(te.EVIDENCE_KEY) is None


def test_sales_work_item_grain_nests_under_the_work_item_it_fulfilled():
    data = _sales()
    result = occ.record_evidence(data, "act-1", summary="打診した", tool=TOOL,
                                 effect="outward")
    assert result["recorded"] and result["record"]["linked_id"] == "act-1"
    assert data["opportunities"][0]["activities"][0]["communications"]


def test_operation_falls_back_to_its_changelog_arm():
    """An Operation declares a changelog but no evidence arm. A side-log of what
    happened is where one use of a tool belongs, so it records there rather than
    no-oping."""
    data = {"id": "p", "profession": "dev", "milestones": [],
            "operations": [{"id": "op-1", "title": "O", "status": "open",
                            "entries": []}]}
    result = occ.record_evidence(data, "op-1", summary="ログを取得", tool="Bash",
                                 effect="state-change")
    assert result["recorded"] and result["via"] == "changelog"
    assert data["operations"][0]["entries"][0]["meta"]["tool"] == "Bash"


# ---------------------------------------------------------------------------
# The sales vocabulary is no longer required of a neutral caller.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("effect,expected", [
    ("read-only", "inbound"),
    ("state-change", "outbound"),
    ("outward", "outbound"),
])
def test_direction_is_derived_from_the_declared_effect(effect, expected):
    """A dev / neutral caller must not have to invent a sales ``direction``.
    Deriving it from the tool's declared effect is the same distinction under
    another name: reading a mailbox is inbound, sending anything is outbound."""
    data = _sales()
    result = occ.record_evidence(data, "opp-1", summary="s", tool=TOOL,
                                 effect=effect)
    assert result["record"]["direction"] == expected


def test_an_explicit_direction_still_wins():
    data = _sales()
    result = occ.record_evidence(data, "opp-1", summary="s", tool=TOOL,
                                 effect="outward", direction="inbound")
    assert result["record"]["direction"] == "inbound"


def test_recording_needs_no_profession_specific_arguments_at_all():
    """The minimal neutral call — summary + tool — must work in both professions."""
    for data, target in ((_dev(), "ms-1"), (_sales(), "opp-1")):
        assert occ.record_evidence(data, target, summary="やった",
                                   tool="t")["recorded"]


# ---------------------------------------------------------------------------
# Target resolution: implicit, and never a guess.
# ---------------------------------------------------------------------------

def test_no_explicit_target_uses_the_active_target():
    data = _dev()
    result = occ.record_evidence(data, summary="s", tool=TOOL)
    assert result["recorded"] and result["target"] == "ms-1"


def test_no_active_target_records_nothing_rather_than_guessing():
    data = _dev(milestones=[])
    result = occ.record_evidence(data, summary="s", tool=TOOL)
    assert result == {"recorded": False, "reason": "no-target", "target": "",
                      "arm": ""}


def test_a_class_with_no_evidence_arm_and_no_changelog_records_nothing():
    """It must NOT fall through onto some other Target — the e-4710 lesson."""
    data = _dev()
    result = occ.record_evidence(data, "tk-1", summary="s", tool=TOOL)
    assert result["recorded"] is False
    assert result["reason"] == "trek-no-evidence-arm"
    assert data["milestones"][0]["entries"] == []


def test_an_empty_summary_records_nothing():
    data = _dev()
    assert occ.record_evidence(data, "ms-1", summary="   ",
                               tool=TOOL)["reason"] == "empty-summary"
    assert data["milestones"][0]["entries"] == []


# ---------------------------------------------------------------------------
# The tool field reaches EVERY writer. A field on one strategy and missing on
# its sibling silently stops answering 「何でやったか」 for whichever classes use
# the other one.
# ---------------------------------------------------------------------------

def test_core_save_entry_stamps_the_tool_in_meta():
    data = _dev()
    core.save_entry(data, ms_id="ms-1", description="d", source="auto",
                    date="", tool=TOOL)
    assert data["milestones"][0]["entries"][0]["meta"]["tool"] == TOOL


def test_plain_changelog_recorder_stamps_the_tool_in_meta():
    data = {"id": "p", "profession": "dev", "milestones": [],
            "operations": [{"id": "op-1", "title": "O", "status": "open",
                            "entries": []}]}
    occ.record_target_entry(data, "op-1", description="d", tool=TOOL)
    assert data["operations"][0]["entries"][0]["meta"]["tool"] == TOOL


def test_sales_add_evidence_stamps_the_tool_on_the_record():
    data = _sales()
    rec = occ.add_evidence(data, "opp-1", summary="s", direction="outbound",
                           tool=TOOL)
    assert rec["tool"] == TOOL


def test_target_engine_add_evidence_stamps_the_tool_as_a_first_class_column():
    """Not smuggled through ``fields``, which is declared-keys-only by contract
    and would reject an undeclared ``tool``."""
    desc = {"kind": "thing", "collection": "things", "label": "Thing",
            "id_prefix": "thg-"}
    data = {"id": "p", "things": [{"id": "thg-1", "label": "T"}]}
    ev = te.add_evidence(data, desc, "thg-1", summary="s", tool=TOOL)
    assert ev["tool"] == TOOL


def test_every_writer_omits_the_tool_key_when_no_tool_is_given():
    """Pre-existing callers pass no tool; their records must stay byte-identical
    rather than growing an empty field."""
    dev = _dev()
    core.save_entry(dev, ms_id="ms-1", description="d", source="auto", date="")
    assert "tool" not in dev["milestones"][0]["entries"][0]["meta"]

    ops = {"id": "p", "profession": "dev", "milestones": [],
           "operations": [{"id": "op-1", "title": "O", "status": "open",
                           "entries": []}]}
    occ.record_target_entry(ops, "op-1", description="d")
    assert "tool" not in ops["operations"][0]["entries"][0]["meta"]

    sales = _sales()
    rec = occ.add_evidence(sales, "opp-1", summary="s", direction="outbound")
    assert "tool" not in rec

    desc = {"kind": "thing", "collection": "things", "label": "Thing",
            "id_prefix": "thg-"}
    engine = {"id": "p", "things": [{"id": "thg-1", "label": "T"}]}
    assert "tool" not in te.add_evidence(engine, desc, "thg-1", summary="s")


def test_the_tool_is_queryable_without_parsing_prose():
    """The point of the structured field: a mechanism can select records by tool
    without matching text, which is how a coincidental match would creep in."""
    data = _dev()
    occ.record_evidence(data, "ms-1", summary="送った", tool=TOOL,
                        effect="outward")
    occ.record_evidence(data, "ms-1", summary="読んだ",
                        tool="mcp__gmail__search_emails", effect="read-only")
    by_tool = [e for e in data["milestones"][0]["entries"]
               if (e.get("meta") or {}).get("tool") == TOOL]
    assert len(by_tool) == 1 and by_tool[0]["description"] == "送った"
