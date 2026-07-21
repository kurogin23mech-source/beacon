"""ms-116 e-3823: funnel edits survive the cloud storage decomposition.

A funnel edit persists through ``save_project`` like any sales mutation. In
cloud mode (MySQL v3) the project doc is split into a projects root row + child
rows by ``decompose_project_targets`` before storage, then reassembled on read.
``account_phases`` / ``opportunity_phases`` are top-level config fields (not
decomposed Target collections), so they must ride in the root row and come back
byte-for-byte after an edit — otherwise a funnel edit would reflect locally but
be dropped in the cloud (Web UI would never see the new stage). These pin that
round-trip so a future decomposition change can't silently strip the funnel.

The Web UI half (the Account tab filter reads ``p.account_phases`` from this
payload) is covered by ms-115 and observed live in the e-3824 dogfood; here we
lock the storage layer that feeds it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "server"))

import sales_entities as se  # noqa: E402
import mysql_client as mc  # noqa: E402


def _roundtrip(data: dict) -> dict:
    meta, tmaps, cmaps = mc.decompose_project_targets(data)
    return mc.assemble_project_targets(meta, tmaps, cmaps)


def test_funnel_edit_survives_decompose_assemble():
    data = se.build_sales_project("Cloud Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    # edit the funnel: insert 最優先 at head, rename リード→見込み, add a stage
    se.insert_phase(data, "account", "最優先", index=0)
    se.rename_phase(data, "account", "リード", "見込み")
    se.add_phase(data, "opportunity", "内諾", probability=60)

    before_acc = [p["name"] for p in data["account_phases"]]
    before_opp = [p["name"] for p in data["opportunity_phases"]]

    restored = _roundtrip(data)

    # funnel definitions come back identical (root row preserved them)
    assert [p["name"] for p in restored["account_phases"]] == before_acc
    assert [p["name"] for p in restored["opportunity_phases"]] == before_opp
    # the renamed reference on the account also survived (it lives on the Target row)
    assert restored["accounts"][0]["phase"] == "見込み"
    # the added opportunity stage kept its attrs
    naidaku = se._find_phase_def(restored["opportunity_phases"], "内諾")
    assert naidaku is not None and naidaku["probability"] == 60


def test_account_phases_ride_in_root_meta_not_a_child_table():
    # account_phases must NOT be treated as a decomposed collection, else it
    # would be stripped from the root row and lost on reassembly.
    import occupation
    assert "account_phases" not in occupation.TARGET_DECOMPOSITION
    assert "opportunity_phases" not in occupation.TARGET_DECOMPOSITION

    data = se.build_sales_project("S", "o")
    meta, _tmaps, _cmaps = mc.decompose_project_targets(data)
    assert "account_phases" in meta and "opportunity_phases" in meta
