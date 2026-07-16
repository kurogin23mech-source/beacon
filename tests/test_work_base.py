"""Unit tests for work_base.py — the occupation-agnostic primitives shared by
development (core.py) and sales (sales_entities.py). ms-109 e-3558."""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import work_base


# ---------------------------------------------------------------------------
# next_suffixed_id — the shared max+1 allocator
# ---------------------------------------------------------------------------

class TestNextSuffixedId:
    def test_empty_starts_at_one(self):
        assert work_base.next_suffixed_id([], "e-") == "e-1"

    def test_max_plus_one(self):
        assert work_base.next_suffixed_id(["e-1", "e-2", "e-3"], "e-") == "e-4"

    def test_uses_max_not_count(self):
        # Gaps must not matter — it's max+1, not len+1.
        assert work_base.next_suffixed_id(["e-1", "e-5"], "e-") == "e-6"

    def test_ignores_other_prefixes(self):
        assert work_base.next_suffixed_id(["op-9", "e-2"], "e-") == "e-3"

    def test_ignores_non_integer_suffix(self):
        # Forgiving like the historical allocators: a malformed id is skipped.
        assert work_base.next_suffixed_id(["e-abc", "e-2"], "e-") == "e-3"

    def test_ignores_non_string_entries(self):
        assert work_base.next_suffixed_id([None, 5, "e-1"], "e-") == "e-2"

    def test_multi_char_prefix(self):
        assert work_base.next_suffixed_id(["comm-1", "comm-2"], "comm-") == "comm-3"
        assert work_base.next_suffixed_id(["nrt-7"], "nrt-") == "nrt-8"

    def test_empty_prefix_rejected(self):
        with pytest.raises(ValueError):
            work_base.next_suffixed_id(["e-1"], "")

    def test_parity_with_dev_and_sales_prefixes(self):
        # The single primitive must serve every occupation prefix identically.
        for prefix in ("e-", "op-", "acc-", "opp-", "act-", "nrt-", "comm-"):
            assert work_base.next_suffixed_id([], prefix) == f"{prefix}1"
            got = work_base.next_suffixed_id([f"{prefix}4"], prefix)
            assert got == f"{prefix}5"


# ---------------------------------------------------------------------------
# now_iso / current_actor
# ---------------------------------------------------------------------------

class TestTimeAndActor:
    def test_now_iso_shape(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                            work_base.now_iso())

    def test_actor_claude_under_env(self, monkeypatch):
        monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
        assert work_base.current_actor() == "claude"

    def test_actor_falls_back_without_env(self, monkeypatch):
        monkeypatch.delenv("BEACON_CLAUDE_CODE", raising=False)
        # Whatever it resolves to (git email / OS user / "unknown"), it's a
        # non-empty string and never the claude literal here.
        actor = work_base.current_actor()
        assert isinstance(actor, str) and actor != "claude" and actor


# ---------------------------------------------------------------------------
# stamp_cancel — soft-cancel vocabulary (append-only)
# ---------------------------------------------------------------------------

class TestStampCancel:
    def test_sets_cancelled_status_and_meta(self, monkeypatch):
        monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
        rec = {"id": "act-1", "status": "todo"}
        out = work_base.stamp_cancel(rec, reason="wrong deal")
        assert out is rec  # in place
        assert rec["status"] == "cancelled"
        assert rec["meta"]["cancelled_by"] == "claude"
        assert rec["meta"]["cancel_reason"] == "wrong deal"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*Z", rec["meta"]["cancelled_at"])

    def test_no_reason_omits_reason_key(self):
        rec = {"id": "e-1", "status": "todo"}
        work_base.stamp_cancel(rec)
        assert "cancel_reason" not in rec["meta"]

    def test_explicit_actor_and_at_win(self):
        rec = {"id": "e-1", "status": "todo"}
        work_base.stamp_cancel(rec, actor="alice", at="2026-01-01T00:00:00Z")
        assert rec["meta"]["cancelled_by"] == "alice"
        assert rec["meta"]["cancelled_at"] == "2026-01-01T00:00:00Z"

    def test_never_removes_fields(self):
        # Cancel is a soft flag: the record's own data survives (immutability).
        rec = {"id": "act-1", "status": "done", "description": "keep me"}
        work_base.stamp_cancel(rec, reason="r")
        assert rec["description"] == "keep me"

    def test_preserves_existing_meta(self):
        rec = {"id": "e-1", "status": "todo", "meta": {"author": "bob"}}
        work_base.stamp_cancel(rec)
        assert rec["meta"]["author"] == "bob"
        assert rec["meta"]["cancelled_by"]


# ---------------------------------------------------------------------------
# record_audit_event — append-only event rows
# ---------------------------------------------------------------------------

class TestRecordAuditEvent:
    def test_appends_row_with_stamps(self, monkeypatch):
        monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
        log = []
        ev = work_base.record_audit_event(log, kind="phase_change",
                                          reason="advanced", to_phase="提案準備")
        assert log == [ev]
        assert ev["kind"] == "phase_change"
        assert ev["actor"] == "claude"
        assert ev["reason"] == "advanced"
        assert ev["to_phase"] == "提案準備"  # occupation-specific field stored verbatim
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*Z", ev["at"])

    def test_append_only(self):
        log = [{"kind": "old"}]
        work_base.record_audit_event(log, kind="new")
        assert len(log) == 2
        assert log[0] == {"kind": "old"}

    def test_kind_required(self):
        with pytest.raises(ValueError):
            work_base.record_audit_event([], kind="")

    def test_no_reason_omits_reason_key(self):
        log = []
        ev = work_base.record_audit_event(log, kind="x")
        assert "reason" not in ev
