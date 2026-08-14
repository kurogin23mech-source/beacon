"""ms-142 e-5261 — occupation.find_target's optional ``kind`` confines resolution.

find_target moved profession-named verbs (opportunity_* / milestone_*) off their
class-specific resolvers (find_opportunity / find_target_milestone) onto this
all-Target resolver. The name then promised "an opportunity" while the resolver
spanned every Target — a mistyped id of another kind could grab a foreign record.
Passing ``kind`` confines resolution to that class's collection, so a wrong-kind or
unknown id returns None instead. Omitting ``kind`` keeps the generic span-all
behaviour unchanged (the generic occupation-claim / set_target_state path relies on it).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation  # noqa: E402
import sales_entities as se  # noqa: E402


def _mixed_project():
    """A sales project carrying BOTH an opportunity and a milestone record, so a
    cross-kind mistyped id has somewhere wrong to (previously) resolve."""
    data = se.build_sales_project("S", "obj")
    opp_id = se.opportunity_add(data, "Deal", created_at="T0")
    data.setdefault("milestones", []).append(
        {"id": "ms-1", "title": "M", "status": "in_progress", "entries": []})
    return data, opp_id, "ms-1"


def test_kind_confines_to_its_collection():
    data, opp_id, ms_id = _mixed_project()
    assert occupation.find_target(data, opp_id, kind="opportunity")["id"] == opp_id
    assert occupation.find_target(data, ms_id, kind="milestone")["id"] == ms_id


def test_wrong_kind_returns_none_not_foreign_record():
    data, opp_id, ms_id = _mixed_project()
    # the crux: an opportunity id looked up AS a milestone finds nothing (not the opp),
    # and vice versa — the resolver no longer spans every Target for a kinded caller.
    assert occupation.find_target(data, opp_id, kind="milestone") is None
    assert occupation.find_target(data, ms_id, kind="opportunity") is None


def test_unknown_id_with_kind_returns_none():
    data, _opp, _ms = _mixed_project()
    assert occupation.find_target(data, "opp-999", kind="opportunity") is None


def test_unknown_kind_returns_none():
    data, opp_id, _ms = _mixed_project()
    assert occupation.find_target(data, opp_id, kind="no-such-class") is None


def test_no_kind_spans_all_unchanged():
    # the generic (span-all) contract is unchanged when kind is omitted.
    data, opp_id, ms_id = _mixed_project()
    assert occupation.find_target(data, opp_id)["id"] == opp_id
    assert occupation.find_target(data, ms_id)["id"] == ms_id
    assert occupation.find_target(data, "nope-1") is None
