"""ms-109 e-3591 — registry-driven Target decomposition for row-oriented
backends (SPEC F7mdrDA4djd3byyDbZAv).

Pins the pure decompose/assemble core WITHOUT a DB:
  1. the occupation registry declares the fat arms + child tables;
  2. the generic ``decompose_project_targets`` reproduces the existing
     milestone-specific ``_v3_decompose`` byte-for-byte (SPEC AC2: dev is one
     instance of the generic mechanism, not a special case);
  3. dev and sales projects round-trip (decompose → assemble == original),
     including the D2 sk rule (2-seg for milestones, 3-seg for sales) and
     inline nesting of a communication under an activity.

The live atomic I/O path (apply/get/save_project_v3) is NOT switched over here
— that needs the child tables + a MySQL integration harness (Phase 2d).
"""

from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import occupation  # noqa: E402
import mysql_client as mc  # noqa: E402


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_registry_declares_sales_and_dev_arms():
    d = occupation.TARGET_DECOMPOSITION
    assert d["milestones"]["arms"] == ("entries",)  # single arm
    assert "activities" in d["opportunities"]["arms"]
    assert "communications" in d["opportunities"]["arms"]
    assert "nurturings" in d["accounts"]["arms"]


def test_child_tables_union_dedups_communications():
    tables = occupation.target_child_tables()
    # communications is shared by opportunity + account → appears once
    assert tables.count("communications") == 1
    assert set(tables) == {"entries", "activities", "communications", "nurturings"}


# ---------------------------------------------------------------------------
# generic reproduces the milestone-specific split (SPEC AC2)
# ---------------------------------------------------------------------------

def _dev_project():
    return {
        "project_id": "p1", "name": "Dev", "profession": "dev",
        "milestones": [
            {"id": "ms-2", "title": "Second", "status": "todo", "entries": [
                {"id": "e-9", "type": "task", "created_at": "2026-05-03T00:00:00Z",
                 "description": "t"}]},
            {"id": "ms-1", "title": "First", "status": "done", "entries": [
                {"id": "e-1", "type": "task", "created_at": "2026-05-01T00:00:00Z",
                 "description": "task",
                 "entries": [  # depth-2 commit nested under the task, stays inline
                     {"id": "e-2", "type": "commit",
                      "created_at": "2026-05-02T00:00:00Z", "description": "c"}]},
            ]},
        ],
    }


def test_generic_reproduces_milestone_decompose():
    data = _dev_project()
    old_meta, old_ms, old_entry = mc._v3_decompose(dict(data))
    meta, tmaps, cmaps = mc.decompose_project_targets(dict(data))
    # milestone target rows identical
    assert tmaps["milestones"] == old_ms
    # entry child rows identical (same 2-seg sk, same inline commit)
    assert cmaps["entries"] == old_entry
    # 2-segment sk preserved for the single-arm milestone collection
    assert "ms-1#e-1" in cmaps["entries"]
    # the depth-2 commit rode inline in its task's entry row (not its own row)
    assert cmaps["entries"]["ms-1#e-1"]["entries"][0]["id"] == "e-2"
    assert meta["schema_version"] == mc.SCHEMA_V3_ENTRY


def test_dev_round_trips_and_orders_milestones():
    data = _dev_project()
    meta, tmaps, cmaps = mc.decompose_project_targets(dict(data))
    rebuilt = mc.assemble_project_targets(meta, tmaps, cmaps)
    ms_ids = [m["id"] for m in rebuilt["milestones"]]
    assert ms_ids == ["ms-1", "ms-2"]  # numeric order restored
    ms1 = next(m for m in rebuilt["milestones"] if m["id"] == "ms-1")
    assert [e["id"] for e in ms1["entries"]] == ["e-1"]
    assert ms1["entries"][0]["entries"][0]["id"] == "e-2"  # inline commit intact


# ---------------------------------------------------------------------------
# sales decomposition + round-trip
# ---------------------------------------------------------------------------

def _sales_project():
    return {
        "project_id": "s1", "name": "Sales", "profession": "sales",
        "opportunities": [
            {"id": "opp-1", "title": "Acme deal", "phase": "lead", "status": "open",
             "gates": [{"phase": "lead", "at": "T0"}],  # bounded arm → inline
             "activities": [
                 {"id": "act-1", "description": "初回面談", "status": "done",
                  "created_at": "2026-07-01T00:00:00Z",
                  # a communication nested under the activity stays inline
                  "communications": [
                      {"id": "comm-1", "linked_id": "act-1", "summary": "sent"}]},
             ],
             "communications": [  # opp-level (unlinked) communication → own row
                 {"id": "comm-2", "linked_id": "", "summary": "cold email",
                  "created_at": "2026-07-02T00:00:00Z"}]},
        ],
        "accounts": [
            {"id": "acc-1", "name": "Acme", "phase": "リード",
             "contacts": [{"name": "Tanaka"}],  # bounded → inline
             "nurturings": [
                 {"id": "nrt-1", "description": "季節挨拶",
                  "created_at": "2026-07-01T00:00:00Z"}],
             "communications": [
                 {"id": "comm-3", "linked_id": "nrt-1", "summary": "call",
                  "created_at": "2026-07-03T00:00:00Z"}]},
        ],
    }


def test_sales_decompose_uses_arm_qualified_sk():
    data = _sales_project()
    meta, tmaps, cmaps = mc.decompose_project_targets(dict(data))
    # opportunity/account become their own target rows
    assert "opp-1" in tmaps["opportunities"]
    assert "acc-1" in tmaps["accounts"]
    # bounded arms stayed inline on the target row
    assert tmaps["opportunities"]["opp-1"]["gates"][0]["phase"] == "lead"
    assert tmaps["accounts"]["acc-1"]["contacts"][0]["name"] == "Tanaka"
    # fat arms were removed from the target row
    assert "activities" not in tmaps["opportunities"]["opp-1"]
    assert "communications" not in tmaps["opportunities"]["opp-1"]
    # 3-segment arm-qualified sk for multi-arm sales targets
    assert "opp-1#activities#act-1" in cmaps["activities"]
    assert "opp-1#communications#comm-2" in cmaps["communications"]
    assert "acc-1#nurturings#nrt-1" in cmaps["nurturings"]
    assert "acc-1#communications#comm-3" in cmaps["communications"]
    # the communication nested under the activity rode inline (not its own row)
    assert cmaps["activities"]["opp-1#activities#act-1"]["communications"][0]["id"] == "comm-1"
    assert "opp-1#communications#comm-1" not in cmaps["communications"]


def test_sales_round_trips_to_original():
    data = _sales_project()
    meta, tmaps, cmaps = mc.decompose_project_targets(dict(data))
    rebuilt = mc.assemble_project_targets(meta, tmaps, cmaps)
    # opportunity reassembled with all arms
    opp = rebuilt["opportunities"][0]
    assert opp["id"] == "opp-1"
    assert [a["id"] for a in opp["activities"]] == ["act-1"]
    assert [c["id"] for c in opp["communications"]] == ["comm-2"]
    assert opp["gates"][0]["phase"] == "lead"
    assert opp["activities"][0]["communications"][0]["id"] == "comm-1"  # inline intact
    # account reassembled
    acc = rebuilt["accounts"][0]
    assert [n["id"] for n in acc["nurturings"]] == ["nrt-1"]
    assert [c["id"] for c in acc["communications"]] == ["comm-3"]


def test_communications_table_shared_but_routed_by_target_prefix():
    # opp-level comm-2 and account-level comm-3 share the communications table;
    # assemble must route each back to the right target by its id prefix.
    data = _sales_project()
    meta, tmaps, cmaps = mc.decompose_project_targets(dict(data))
    assert set(cmaps["communications"].keys()) == {
        "opp-1#communications#comm-2", "acc-1#communications#comm-3"}
    rebuilt = mc.assemble_project_targets(meta, tmaps, cmaps)
    opp_comms = {c["id"] for c in rebuilt["opportunities"][0]["communications"]}
    acc_comms = {c["id"] for c in rebuilt["accounts"][0]["communications"]}
    assert opp_comms == {"comm-2"}
    assert acc_comms == {"comm-3"}


# ms-109 e-3591 — drift guard: every collection/child table the registry
# decomposes must be a declared MySQL entity, else create_mysql_tables would
# not create it and reads would hit a missing table.
def test_mysql_entities_cover_registry():
    import mysql_client as mc
    entities = set(mc.ENTITIES)
    for coll in occupation.TARGET_DECOMPOSITION:
        assert coll in entities, f"{coll} missing from mysql_client.ENTITIES"
    for tbl in occupation.target_child_tables():
        assert tbl in entities, f"{tbl} missing from mysql_client.ENTITIES"
