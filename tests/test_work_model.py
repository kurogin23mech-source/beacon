"""Unit tests for work_model.py — the occupation-agnostic canonical shape for
Target / WorkItem / Evidence shared by development (core.py) and sales
(sales_entities.py). ms-109 e-3559 (expand step of the field unification)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import work_base
import work_model


# ---------------------------------------------------------------------------
# Target label — canonical `label` with tolerant fallback to `title` / `name`
# ---------------------------------------------------------------------------

class TestTargetLabel:
    def test_reads_canonical_label(self):
        assert work_model.target_label({"label": "Q3 商談"}) == "Q3 商談"

    def test_falls_back_to_title(self):
        # milestone / opportunity legacy key
        assert work_model.target_label({"title": "MVP"}) == "MVP"

    def test_falls_back_to_name(self):
        # account legacy key
        assert work_model.target_label({"name": "Acme 社"}) == "Acme 社"

    def test_canonical_wins_over_legacy(self):
        # During dual-write both may be present; canonical is authoritative.
        assert work_model.target_label({"label": "new", "title": "old"}) == "new"

    def test_title_preferred_over_name_when_both_legacy(self):
        # _LEGACY_LABEL_KEYS order is (title, name).
        assert work_model.target_label({"title": "T", "name": "N"}) == "T"

    def test_empty_when_no_label_key(self):
        assert work_model.target_label({"id": "ms-1"}) == ""

    def test_empty_string_label_falls_through(self):
        # An empty canonical value should not shadow a real legacy value.
        assert work_model.target_label({"label": "", "title": "real"}) == "real"

    def test_non_dict_returns_empty(self):
        assert work_model.target_label(None) == ""
        assert work_model.target_label("nope") == ""


class TestPresentLegacyLabelKey:
    def test_finds_title(self):
        assert work_model.present_legacy_label_key({"title": "x"}) == "title"

    def test_finds_name(self):
        assert work_model.present_legacy_label_key({"name": "x"}) == "name"

    def test_title_preferred(self):
        assert work_model.present_legacy_label_key({"title": "t", "name": "n"}) == "title"

    def test_none_present(self):
        assert work_model.present_legacy_label_key({"label": "only-canonical"}) == ""

    def test_non_dict(self):
        assert work_model.present_legacy_label_key(None) == ""


class TestSetTargetLabel:
    def test_canonical_only_by_default(self):
        t = {"title": "old"}
        work_model.set_target_label(t, "new")
        assert t["label"] == "new"
        # legacy untouched when not dual-writing
        assert t["title"] == "old"

    def test_dual_write_mirrors_present_legacy_key(self):
        t = {"name": "old"}  # account
        work_model.set_target_label(t, "new", dual_write=True)
        assert t["label"] == "new"
        assert t["name"] == "new"  # mirrored to the present legacy key

    def test_dual_write_explicit_legacy_key(self):
        t = {}
        work_model.set_target_label(t, "new", dual_write=True, legacy_key="name")
        assert t["label"] == "new"
        assert t["name"] == "new"

    def test_dual_write_defaults_to_title_when_no_legacy(self):
        t = {}
        work_model.set_target_label(t, "new", dual_write=True)
        assert t["label"] == "new"
        assert t["title"] == "new"

    def test_returns_target(self):
        t = {}
        assert work_model.set_target_label(t, "x") is t


# ---------------------------------------------------------------------------
# WorkItem lifecycle
# ---------------------------------------------------------------------------

class TestWorkItemStatus:
    def test_status_read(self):
        assert work_model.work_item_status({"status": "todo"}) == "todo"

    def test_status_absent(self):
        assert work_model.work_item_status({}) == ""

    def test_status_non_dict(self):
        assert work_model.work_item_status(None) == ""

    def test_is_done(self):
        assert work_model.is_done({"status": "done"})
        assert not work_model.is_done({"status": "todo"})

    def test_is_cancelled(self):
        assert work_model.is_cancelled({"status": work_base.CANCELLED_STATUS})
        assert not work_model.is_cancelled({"status": "todo"})

    def test_is_open(self):
        assert work_model.is_open({"status": "todo"})
        assert work_model.is_open({"status": "in_progress"})
        assert not work_model.is_open({"status": "done"})
        assert not work_model.is_open({"status": "cancelled"})


class TestMarkDone:
    def test_sets_status_and_done_at(self):
        item = {"status": "todo"}
        work_model.mark_done(item, at="2026-07-17T00:00:00Z")
        assert item["status"] == "done"
        assert item["done_at"] == "2026-07-17T00:00:00Z"

    def test_done_at_defaults_to_now(self):
        item = {"status": "todo"}
        work_model.mark_done(item)
        # now_iso format: ...Z
        assert item["done_at"].endswith("Z")
        assert item["status"] == "done"

    def test_actor_and_reason_recorded_in_meta(self):
        item = {"status": "todo"}
        work_model.mark_done(item, actor="claude", reason="PR merged")
        assert item["meta"]["done_by"] == "claude"
        assert item["meta"]["done_reason"] == "PR merged"

    def test_no_meta_when_no_actor_reason(self):
        item = {"status": "todo"}
        work_model.mark_done(item, at="2026-07-17T00:00:00Z")
        assert "meta" not in item

    def test_returns_item(self):
        item = {"status": "todo"}
        assert work_model.mark_done(item) is item


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class TestEvidenceLinkedId:
    def test_reads_linked_id(self):
        assert work_model.evidence_linked_id({"linked_id": "act-3"}) == "act-3"

    def test_absent_is_empty(self):
        # development commit expresses close by nesting, not a field yet (e-3560)
        assert work_model.evidence_linked_id({"id": "commit-1"}) == ""

    def test_non_dict(self):
        assert work_model.evidence_linked_id(None) == ""


# ---------------------------------------------------------------------------
# Skeleton constructors
# ---------------------------------------------------------------------------

class TestNewTarget:
    def test_generic_skeleton(self):
        t = work_model.new_target("ms-1", "MVP", created_at="2026-01-01T00:00:00Z",
                                  created_by="claude")
        assert t["id"] == "ms-1"
        assert t["label"] == "MVP"
        assert t["status"] == "todo"
        assert t["created_at"] == "2026-01-01T00:00:00Z"
        assert t["created_by"] == "claude"
        assert t["assignee"] == ""

    def test_extra_merges_occupation_fields(self):
        t = work_model.new_target("opp-1", "Acme deal", phase="lead", account_id="acc-1")
        assert t["label"] == "Acme deal"
        assert t["phase"] == "lead"
        assert t["account_id"] == "acc-1"

    def test_defaults_fill_from_work_base(self):
        t = work_model.new_target("ms-2", "x")
        assert t["created_at"].endswith("Z")
        assert t["created_by"]  # non-empty (actor)


class TestNewWorkItem:
    def test_generic_skeleton(self):
        w = work_model.new_work_item("e-1", "do the thing",
                                     created_at="2026-01-01T00:00:00Z")
        assert w["id"] == "e-1"
        assert w["description"] == "do the thing"
        assert w["status"] == "todo"
        assert w["created_at"] == "2026-01-01T00:00:00Z"

    def test_extra_merges(self):
        w = work_model.new_work_item("act-1", "call them", deadline="2026-02-01",
                                     who_has_the_ball="self")
        assert w["deadline"] == "2026-02-01"
        assert w["who_has_the_ball"] == "self"


class TestNewEvidence:
    def test_generic_skeleton(self):
        e = work_model.new_evidence("comm-1", linked_id="act-1",
                                    created_at="2026-01-01T00:00:00Z")
        assert e["id"] == "comm-1"
        assert e["linked_id"] == "act-1"
        assert e["created_at"] == "2026-01-01T00:00:00Z"

    def test_extra_merges(self):
        e = work_model.new_evidence("comm-2", direction="outbound", channel="email")
        assert e["direction"] == "outbound"
        assert e["channel"] == "email"


# ---------------------------------------------------------------------------
# Generic collection helpers
# ---------------------------------------------------------------------------

class TestFindById:
    def test_finds(self):
        recs = [{"id": "a"}, {"id": "b"}]
        assert work_model.find_by_id(recs, "b") == {"id": "b"}

    def test_missing_returns_none(self):
        assert work_model.find_by_id([{"id": "a"}], "z") is None

    def test_skips_non_dict(self):
        assert work_model.find_by_id([None, "x", {"id": "a"}], "a") == {"id": "a"}

    def test_empty_and_none(self):
        assert work_model.find_by_id([], "a") is None
        assert work_model.find_by_id(None, "a") is None


class TestCollectIds:
    def test_collects(self):
        assert work_model.collect_ids([{"id": "a"}, {"id": "b"}]) == ["a", "b"]

    def test_skips_idless_and_non_dict(self):
        assert work_model.collect_ids([{"id": "a"}, {}, None, {"id": "c"}]) == ["a", "c"]

    def test_feeds_next_suffixed_id(self):
        ids = work_model.collect_ids([{"id": "e-1"}, {"id": "e-4"}])
        assert work_base.next_suffixed_id(ids, "e-") == "e-5"

    def test_empty_and_none(self):
        assert work_model.collect_ids([]) == []
        assert work_model.collect_ids(None) == []


# ---------------------------------------------------------------------------
# ensure_target_label — idempotent backfill (migrate step, e-3625)
# ---------------------------------------------------------------------------

class TestEnsureTargetLabel:
    def test_backfills_from_title(self):
        t = {"id": "ms-1", "title": "Ship it"}
        assert work_model.ensure_target_label(t) is True
        assert t["label"] == "Ship it"
        assert t["title"] == "Ship it"  # legacy key untouched (additive)

    def test_backfills_from_name(self):
        t = {"id": "a-1", "name": "Acme"}
        assert work_model.ensure_target_label(t) is True
        assert t["label"] == "Acme"
        assert t["name"] == "Acme"

    def test_noop_when_label_present(self):
        t = {"id": "ms-1", "label": "Canon", "title": "Legacy"}
        assert work_model.ensure_target_label(t) is False
        assert t["label"] == "Canon"

    def test_idempotent(self):
        t = {"id": "ms-1", "title": "Ship it"}
        assert work_model.ensure_target_label(t) is True
        assert work_model.ensure_target_label(t) is False  # second run writes nothing

    def test_noop_when_no_label_anywhere(self):
        t = {"id": "ms-1"}
        assert work_model.ensure_target_label(t) is False
        assert "label" not in t

    def test_non_dict(self):
        assert work_model.ensure_target_label(None) is False
        assert work_model.ensure_target_label("x") is False

    def test_empty_title_not_backfilled(self):
        t = {"id": "ms-1", "title": ""}
        assert work_model.ensure_target_label(t) is False
        assert "label" not in t


# ---------------------------------------------------------------------------
# evidence-close — link_evidence / close_work_item_with_evidence (e-3560)
# ---------------------------------------------------------------------------

class TestEvidenceClose:
    def test_link_evidence_stamps_canonical_key(self):
        ev = {"id": "commit-1"}
        work_model.link_evidence(ev, "e-42")
        assert ev["linked_id"] == "e-42"
        assert work_model.evidence_linked_id(ev) == "e-42"

    def test_link_evidence_noop_on_empty_id(self):
        ev = {"id": "commit-1"}
        work_model.link_evidence(ev, "")
        assert "linked_id" not in ev  # closes nothing → no stamp

    def test_dev_and_sales_read_through_same_accessor(self):
        commit = work_model.link_evidence({"id": "c1"}, "e-10")   # dev
        comm = {"id": "comm-1", "linked_id": "act-3"}             # sales
        assert work_model.evidence_linked_id(commit) == "e-10"
        assert work_model.evidence_linked_id(comm) == "act-3"

    def test_close_work_item_with_evidence_links_and_marks_done(self):
        wi = {"id": "e-7", "status": "todo"}
        ev = {"id": "commit-9"}
        rwi, rev = work_model.close_work_item_with_evidence(
            wi, ev, at="2026-07-18T00:00:00Z", actor="claude")
        assert rev["linked_id"] == "e-7"
        assert rwi["status"] == "done"
        assert rwi["done_at"] == "2026-07-18T00:00:00Z"
        assert rwi["meta"]["done_by"] == "claude"
