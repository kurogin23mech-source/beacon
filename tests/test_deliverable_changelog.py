"""Unit tests for deliverable_changelog — the root target's append-type log of
produced value (ms-161 e-5821 / SPEC 方針1-2, 受入条件1/5).

Pure-function tests (no I/O, no CLI): hand the schema/append/read seams a plain
project dict and assert (a) the abstract entry schema validates + defaults, (b)
append is in-memory-only with deterministic ids and injectable provenance, and
(c) read filters return non-mutating copies. Profession-independence (受入条件5)
is asserted structurally: the storage key and schema carry no milestone/dev
vocabulary.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import deliverable_changelog as dc  # noqa: E402


def _entry(**over):
    base = {
        "source": {"target_id": "ms-42", "kind": "milestone"},
        "category": "capability",
        "title": "DM 受信の idle-wake",
        "summary": "他セッションからの DM で AI が起きる",
    }
    base.update(over)
    return base


# --- schema: required fields (方針2) ----------------------------------------

def test_normalize_keeps_required_and_defaults_optional():
    out = dc.normalize_deliverable_entry(_entry())
    assert out["source"] == {"target_id": "ms-42", "kind": "milestone"}
    assert out["category"] == "capability"
    assert out["title"] == "DM 受信の idle-wake"
    assert out["summary"] == "他セッションからの DM で AI が起きる"
    # optional fields defaulted
    assert out["ref"] == ""
    assert out["tags"] == []
    assert out["status"] == dc.STATUS_ACTIVE
    assert out["supersedes"] is None


def test_normalize_strips_whitespace():
    out = dc.normalize_deliverable_entry(_entry(title="  spaced  "))
    assert out["title"] == "spaced"


@pytest.mark.parametrize("missing", ["category", "title", "summary"])
def test_normalize_requires_description_fields(missing):
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(**{missing: "  "}))


def test_normalize_requires_source_with_both_keys():
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(source={"target_id": "ms-1"}))
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(source={"kind": "milestone"}))
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(source="ms-1"))


def test_normalize_source_is_trimmed_to_two_keys():
    out = dc.normalize_deliverable_entry(
        _entry(source={"target_id": "ms-1", "kind": "milestone", "junk": 1}))
    assert out["source"] == {"target_id": "ms-1", "kind": "milestone"}


def test_normalize_tags_coerced_to_clean_list():
    out = dc.normalize_deliverable_entry(_entry(tags=["cli", "", " bus "]))
    assert out["tags"] == ["cli", "bus"]


def test_normalize_rejects_non_list_tags():
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(tags="cli"))


def test_normalize_status_vocabulary_enforced():
    for s in (dc.STATUS_ACTIVE, dc.STATUS_SUPERSEDED, dc.STATUS_RETIRED):
        assert dc.normalize_deliverable_entry(_entry(status=s))["status"] == s
    with pytest.raises(dc.DeliverableValidationError):
        dc.normalize_deliverable_entry(_entry(status="actve"))


def test_normalize_does_not_stamp_provenance():
    """id/at/actor are append-time, not part of pure normalization."""
    out = dc.normalize_deliverable_entry(_entry())
    assert "id" not in out and "at" not in out and "actor" not in out


def test_normalize_does_not_mutate_input():
    raw = _entry()
    snapshot = copy.deepcopy(raw)
    dc.normalize_deliverable_entry(raw)
    assert raw == snapshot


# --- append: root arm write (受入条件1) --------------------------------------

def test_append_stamps_id_time_actor_and_returns_entry():
    data = {"name": "P", "milestones": []}
    stored = dc.append_deliverable(data, _entry(), at="2026-08-29T00:00:00Z",
                                   actor="claude")
    assert stored["id"] == "dlv-1"
    assert stored["at"] == "2026-08-29T00:00:00Z"
    assert stored["actor"] == "claude"
    assert stored["title"] == "DM 受信の idle-wake"


def test_append_is_in_memory_on_the_project_dict():
    data = {"name": "P", "milestones": []}
    dc.append_deliverable(data, _entry())
    assert dc.CHANGELOG_KEY in data
    assert len(data[dc.CHANGELOG_KEY]) == 1


def test_append_mints_sequential_ids():
    data = {}
    a = dc.append_deliverable(data, _entry())
    b = dc.append_deliverable(data, _entry(title="second"))
    c = dc.append_deliverable(data, _entry(title="third"))
    assert [a["id"], b["id"], c["id"]] == ["dlv-1", "dlv-2", "dlv-3"]


def test_append_id_continues_past_existing_max():
    data = {dc.CHANGELOG_KEY: [{"id": "dlv-7"}, {"id": "dlv-3"}]}
    stored = dc.append_deliverable(data, _entry())
    assert stored["id"] == "dlv-8"


def test_append_defaults_time_and_actor_when_absent():
    data = {}
    stored = dc.append_deliverable(data, _entry())
    # shape only — real clock/actor, just assert they are populated strings
    assert stored["at"].endswith("Z") and stored["at"]
    assert isinstance(stored["actor"], str) and stored["actor"]


def test_append_validates_before_mutating():
    data = {}
    with pytest.raises(dc.DeliverableValidationError):
        dc.append_deliverable(data, _entry(title=""))
    # all-or-nothing: no half-written log
    assert dc.CHANGELOG_KEY not in data


def test_append_preserves_lifecycle_fields():
    data = {}
    stored = dc.append_deliverable(
        data, _entry(status=dc.STATUS_SUPERSEDED, supersedes="dlv-1",
                     ref="app-map", tags=["surface"]))
    assert stored["status"] == dc.STATUS_SUPERSEDED
    assert stored["supersedes"] == "dlv-1"
    assert stored["ref"] == "app-map"
    assert stored["tags"] == ["surface"]


# --- read: filters + non-mutating copies (受入条件1 read) ---------------------

def _log3():
    data = {}
    dc.append_deliverable(data, _entry(source={"target_id": "ms-1", "kind": "milestone"},
                                       category="capability", title="a"))
    dc.append_deliverable(data, _entry(source={"target_id": "ms-2", "kind": "milestone"},
                                       category="surface", title="b",
                                       status=dc.STATUS_RETIRED))
    dc.append_deliverable(data, _entry(source={"target_id": "ms-1", "kind": "milestone"},
                                       category="capability", title="c"))
    return data


def test_read_returns_all_in_insertion_order():
    titles = [e["title"] for e in dc.read_deliverables(_log3())]
    assert titles == ["a", "b", "c"]


def test_read_filters_by_status():
    active = dc.read_deliverables(_log3(), status=dc.STATUS_ACTIVE)
    assert [e["title"] for e in active] == ["a", "c"]


def test_read_filters_by_category_and_source():
    data = _log3()
    assert [e["title"] for e in dc.read_deliverables(data, category="capability")] \
        == ["a", "c"]
    assert [e["title"] for e in dc.read_deliverables(data, source_target="ms-2")] \
        == ["b"]


def test_read_rejects_unknown_status_filter():
    with pytest.raises(dc.DeliverableValidationError):
        dc.read_deliverables(_log3(), status="bogus")


def test_read_returns_copies_not_live_rows():
    data = _log3()
    rows = dc.read_deliverables(data)
    rows[0]["title"] = "MUTATED"
    rows[0]["source"]["target_id"] = "HACKED"
    rows[0]["tags"].append("x")
    # the stored log is untouched
    stored = data[dc.CHANGELOG_KEY][0]
    assert stored["title"] == "a"
    assert stored["source"]["target_id"] == "ms-1"
    assert stored["tags"] == []


def test_read_empty_or_absent_log_is_empty_list():
    assert dc.read_deliverables({}) == []
    assert dc.read_deliverables({dc.CHANGELOG_KEY: "corrupt"}) == []


# --- profession independence (受入条件5) -------------------------------------

def test_storage_key_and_schema_carry_no_dev_vocabulary():
    """The log key and schema fields must not name a dev collection/concept, so
    the same log serves sales / back-office without a dev-shaped bias."""
    assert "milestone" not in dc.CHANGELOG_KEY
    out = dc.normalize_deliverable_entry(
        _entry(source={"target_id": "opp-3", "kind": "opportunity"},
               category="outcome", title="成約: Acme"))
    # a non-dev source is a first-class citizen
    assert out["source"]["kind"] == "opportunity"
