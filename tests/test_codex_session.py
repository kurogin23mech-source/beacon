"""Tests for ms-93 / e-2497 Codex stable session storage.

Pins the Codex 2026-06-25 design feedback:

- session record path is sharded by project_id × stable_instance_key
  (= cwd hash + parent_pid), so multi-project / multi-Codex parallel
  runs don't collide on one sid
- ``acquire_codex_session`` reuses the existing record when present and
  not closed (= sid stability across CLI bounces inside one Codex run)
- Graceful shutdown stamps ``closed=true`` + ``last_seen`` and keeps
  the file in place for post-mortem (= Codex preferred over deletion)
- Reusing a path that holds a closed record archives the closed record
  as ``<path>.closed`` and mints a fresh sid (= no silent reuse of a
  sid the server already knows is gone)
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import codex_session as cs


# ------------------------------------------------------------------ #
# Pure helpers
# ------------------------------------------------------------------ #


class TestDeriveStableInstanceKey:
    def test_same_cwd_same_pid_same_key(self):
        assert (
            cs.derive_stable_instance_key("/tmp/foo", 1234)
            == cs.derive_stable_instance_key("/tmp/foo", 1234)
        )

    def test_different_cwd_different_key(self):
        a = cs.derive_stable_instance_key("/tmp/foo", 1234)
        b = cs.derive_stable_instance_key("/tmp/bar", 1234)
        assert a != b

    def test_different_pid_different_key(self):
        a = cs.derive_stable_instance_key("/tmp/foo", 1234)
        b = cs.derive_stable_instance_key("/tmp/foo", 5678)
        assert a != b

    def test_key_includes_pid_suffix(self):
        key = cs.derive_stable_instance_key("/tmp/foo", 1234)
        assert key.endswith("-1234")


class TestCodexSessionPath:
    def test_layout_is_sharded_by_project_and_key(self, tmp_path):
        p = cs.codex_session_path(
            project_id="proj-abc",
            stable_instance_key="0123456789abcdef-1234",
            home=str(tmp_path),
        )
        assert "codex" in p.parts
        assert "sessions" in p.parts
        assert "proj-abc" in p.parts
        assert p.name == "0123456789abcdef-1234.json"

    def test_different_projects_isolated(self, tmp_path):
        a = cs.codex_session_path("proj-a", "key-1", home=str(tmp_path))
        b = cs.codex_session_path("proj-b", "key-1", home=str(tmp_path))
        assert a != b


# ------------------------------------------------------------------ #
# acquire / heartbeat / close lifecycle
# ------------------------------------------------------------------ #


class TestAcquireCodexSession:
    def test_fresh_mint_when_no_file(self, tmp_path):
        s = cs.acquire_codex_session(
            project_id="proj-1",
            cwd="/tmp/codex-run-1",
            parent_pid=4242,
            pid=4243,
            beacon_bin="/Users/x/tools/beacon/bin/beacon",
            home=str(tmp_path),
        )
        assert s.session_id.startswith("codex-")
        assert s.project_id == "proj-1"
        assert s.parent_pid == 4242
        assert s.pid == 4243
        assert s.beacon_bin == "/Users/x/tools/beacon/bin/beacon"
        assert s.agent_kind == "codex"
        assert s.closed is False

    def test_persists_to_disk(self, tmp_path):
        s = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        assert path.is_file()
        loaded = json.loads(path.read_text())
        assert loaded["session_id"] == s.session_id

    def test_reuse_when_file_exists_and_not_closed(self, tmp_path):
        first = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        # Simulate CLI bounce: pid changes, parent_pid + cwd stay.
        second = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=3,
            home=str(tmp_path),
        )
        # Same sid — that's the whole point.
        assert second.session_id == first.session_id
        # pid updated to reflect the new loop instance.
        assert second.pid == 3

    def test_closed_file_archived_and_fresh_sid_minted(self, tmp_path):
        first = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        cs.close_codex_session(path)
        second = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=3,
            home=str(tmp_path),
        )
        assert second.session_id != first.session_id
        # Closed record archived for post-mortem.
        archive = path.with_suffix(".json.closed")
        assert archive.is_file()

    def test_cross_project_isolation(self, tmp_path):
        a = cs.acquire_codex_session(
            project_id="proj-a", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        b = cs.acquire_codex_session(
            project_id="proj-b", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        assert a.session_id != b.session_id

    def test_same_cwd_different_parent_pid_isolated(self, tmp_path):
        # Two parallel Codex processes from the same cwd must not share sid.
        a = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=10,
            home=str(tmp_path),
        )
        b = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=2, pid=20,
            home=str(tmp_path),
        )
        assert a.session_id != b.session_id


class TestHeartbeat:
    def test_updates_last_active_in_place(self, tmp_path):
        s = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        first_active = s.last_active
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        # Force a measurable timestamp delta.
        import time as _time
        _time.sleep(1.1)
        cs.heartbeat_codex_session(path)
        reloaded = cs.read_codex_session(path)
        assert reloaded.last_active != first_active
        # sid unchanged.
        assert reloaded.session_id == s.session_id

    def test_no_op_on_missing(self, tmp_path):
        # Doesn't raise — caller can heartbeat into nowhere safely.
        path = tmp_path / "does-not-exist.json"
        cs.heartbeat_codex_session(path)

    def test_no_op_on_closed(self, tmp_path):
        s = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        cs.close_codex_session(path)
        cs.heartbeat_codex_session(path)
        # Closed record stays closed; last_active unchanged.
        reloaded = cs.read_codex_session(path)
        assert reloaded.closed is True


class TestClose:
    def test_stamps_closed_and_last_seen(self, tmp_path):
        s = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        cs.close_codex_session(path)
        reloaded = cs.read_codex_session(path)
        assert reloaded.closed is True
        assert reloaded.last_seen != ""

    def test_file_not_deleted_on_close(self, tmp_path):
        # Codex feedback: keep file for post-mortem, don't unlink.
        s = cs.acquire_codex_session(
            project_id="proj-1", cwd="/tmp/run", parent_pid=1, pid=2,
            home=str(tmp_path),
        )
        path = cs.codex_session_path(
            "proj-1", cs.derive_stable_instance_key("/tmp/run", 1),
            home=str(tmp_path),
        )
        cs.close_codex_session(path)
        assert path.is_file()
