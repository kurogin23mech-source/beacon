"""CLI cloud-mode dispatch tests for trek commands (ms-69 / e-1681).

These pin the contract:

    cmd_trek_*  (when `_is_cloud_mode()` is True)
        ──>  ApiClient.<trek method>(...)
        ──>  HTTP /api/treks/*

We do NOT spin up the real ApiClient or the network here. Instead we patch
``_is_cloud_mode`` to True and ``_get_api_client`` to return a FakeApiClient
that records each call. Each test asserts the right method was invoked with
the right arguments.

Local-mode behavior (trek_store backed, no cloud) is covered separately by
``tests/test_trek_cli.py`` (the existing CLI smoke). This file is
specifically for the cloud branch added in e-1681.
"""

from __future__ import annotations

import json
import os
import sys
from io import StringIO

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Fake ApiClient — records every trek-related call as (name, kwargs).
# ---------------------------------------------------------------------------

class FakeApiClient:
    """Records each trek method call; returns canned trek docs."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_trek = self._make_trek("tk-fake01", "Hello", "active")

    @staticmethod
    def _make_trek(trek_id: str, title: str, status: str = "active") -> dict:
        return {
            "trek_id": trek_id,
            "title": title,
            "description": "",
            "type": "persistent",
            "status": status,
            "creator_actor": {"user_id": "uid-a", "email": "a@x"},
            "leader_session_id": "sv-leader",
            "members": [
                {"user_id": "uid-a", "email": "a@x", "role": "leader",
                 "invited_at": "2026-06-14T00:00:00Z",
                 "joined_at": "2026-06-14T00:00:00Z", "invited_by": "uid-a"},
            ],
            "scope": [],
            "halt": None,
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "archived_at": None,
        }

    def _record(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        return self._next_trek

    def create_trek(self, **kwargs):
        return self._record("create_trek", **kwargs)

    def list_treks(self, **kwargs):
        self.calls.append(("list_treks", kwargs))
        return [self._next_trek]

    def get_trek(self, trek_id):
        return self._record("get_trek", trek_id=trek_id)

    def patch_trek(self, trek_id, **kwargs):
        return self._record("patch_trek", trek_id=trek_id, **kwargs)

    def archive_trek(self, trek_id):
        return self._record("archive_trek", trek_id=trek_id)

    def start_trek(self, trek_id):
        return self._record("start_trek", trek_id=trek_id)

    def invite_trek_member(self, trek_id, email):
        return self._record("invite_trek_member",
                            trek_id=trek_id, email=email)

    def join_trek(self, trek_id):
        return self._record("join_trek", trek_id=trek_id)

    def leave_trek(self, trek_id):
        return self._record("leave_trek", trek_id=trek_id)

    def add_trek_scope(self, trek_id, **kwargs):
        return self._record("add_trek_scope", trek_id=trek_id, **kwargs)

    def remove_trek_scope(self, trek_id, **kwargs):
        return self._record("remove_trek_scope", trek_id=trek_id, **kwargs)

    def set_trek_halt(self, trek_id, **kwargs):
        return self._record("set_trek_halt", trek_id=trek_id, **kwargs)

    def clear_trek_halt(self, trek_id):
        return self._record("clear_trek_halt", trek_id=trek_id)

    def transfer_trek_leader(self, trek_id, **kwargs):
        return self._record("transfer_trek_leader",
                            trek_id=trek_id, **kwargs)


@pytest.fixture
def fake_client(monkeypatch):
    """Patch _is_cloud_mode → True, _get_api_client → fake, get_store → StoreApi(fake).

    ms-84 Phase 2: cmd_trek_show が Store.get_trek 経由になったため、 legacy
    _is_cloud_mode + _get_api_client patch だけでは新経路を捕えられない。
    fake_client を wrap した StoreApi を get_store にも仕込んで、 旧経路 (=
    まだ Store 化していない trek subcommand) と新経路 (= cmd_trek_show) の
    両方が同じ fake に届くようにする。
    """
    from store_api import StoreApi
    fake = FakeApiClient()
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (fake, {"project_id": "fake-project"})
    )
    store_api = StoreApi.__new__(StoreApi)
    store_api._client = fake
    store_api._project_id = "fake-project"
    monkeypatch.setattr(commands, "get_store", lambda: store_api)
    return fake


@pytest.fixture(autouse=True)
def clear_trek_env(monkeypatch):
    """Strip BEACON_TREK_* / BEACON_USER_* / BEACON_SESSION_ID across tests."""
    for key in list(os.environ):
        if key.startswith(("BEACON_TREK_", "BEACON_USER_",
                           "BEACON_SESSION_", "BEACON_JSON")):
            monkeypatch.delenv(key, raising=False)
    # Default identity so _resolve_creator_identity has something
    monkeypatch.setenv("BEACON_USER_ID", "uid-a")
    monkeypatch.setenv("BEACON_USER_EMAIL", "a@x")
    monkeypatch.setenv("BEACON_SESSION_ID", "sv-caller")


def _capture_stdout(monkeypatch):
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


# ---------------------------------------------------------------------------
# Cloud dispatch tests
# ---------------------------------------------------------------------------

class TestCreateCloudDispatch:
    def test_create_routes_to_api_client(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_TITLE", "Hello")
        monkeypatch.setenv("BEACON_TREK_TYPE", "temporary")
        monkeypatch.setenv("BEACON_TREK_DESCRIPTION", "test desc")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_create()
        assert len(fake_client.calls) == 1
        name, kwargs = fake_client.calls[0]
        assert name == "create_trek"
        assert kwargs["title"] == "Hello"
        assert kwargs["type_"] == "temporary"
        assert kwargs["description"] == "test desc"
        assert kwargs["creator_session_id"] == "sv-caller"


class TestListCloudDispatch:
    def test_list_routes_to_api_client(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_JSON", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_list()
        assert fake_client.calls[0][0] == "list_treks"
        assert fake_client.calls[0][1]["status"] == ""
        assert fake_client.calls[0][1]["include_archived"] is False
        assert fake_client.calls[0][1]["all_actors"] is False

    def test_list_passes_status_filter(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_STATUS", "active")
        monkeypatch.setenv("BEACON_JSON", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_list()
        assert fake_client.calls[0][1]["status"] == "active"

    def test_list_passes_include_archived(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_INCLUDE_ARCHIVED", "1")
        monkeypatch.setenv("BEACON_JSON", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_list()
        assert fake_client.calls[0][1]["include_archived"] is True

    def test_list_passes_all_actors(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ALL_ACTORS", "1")
        monkeypatch.setenv("BEACON_JSON", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_list()
        assert fake_client.calls[0][1]["all_actors"] is True


class TestShowCloudDispatch:
    def test_show_routes_to_get_trek(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_JSON", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_show()
        assert fake_client.calls[0] == ("get_trek", {"trek_id": "tk-fake01"})


class TestStartArchiveCloudDispatch:
    def test_start_routes_to_start_trek(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_start()
        assert fake_client.calls[0] == ("start_trek", {"trek_id": "tk-fake01"})

    def test_archive_routes_to_archive_trek(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_archive()
        assert fake_client.calls[0] == ("archive_trek", {"trek_id": "tk-fake01"})


class TestInviteJoinLeaveCloudDispatch:
    def test_invite_routes_to_invite_trek_member(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_ACTOR", "b@x")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_invite()
        assert fake_client.calls[0] == (
            "invite_trek_member",
            {"trek_id": "tk-fake01", "email": "b@x"},
        )

    def test_join_routes_to_join_trek(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        # ms-88 / e-2090 — bypass typed-ack gate (= the rest of this test
        # is about cloud routing, not the consent gate).
        monkeypatch.setenv("BEACON_TREK_CONSENT_ACK", "1")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_join()
        assert fake_client.calls[0] == ("join_trek", {"trek_id": "tk-fake01"})

    def test_leave_routes_to_leave_trek(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_leave()
        assert fake_client.calls[0] == ("leave_trek", {"trek_id": "tk-fake01"})


class TestScopeCloudDispatch:
    def test_add_scope_with_milestone_ref(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_SCOPE_ADD", "beacon:ms-69")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_plan()
        name, kwargs = fake_client.calls[0]
        assert name == "add_trek_scope"
        assert kwargs == {
            "trek_id": "tk-fake01", "project": "beacon",
            "milestone": "ms-69", "operation": "", "task": "",
        }

    def test_add_scope_project_only(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_SCOPE_ADD", "beacon")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_plan()
        name, kwargs = fake_client.calls[0]
        assert name == "add_trek_scope"
        assert kwargs["milestone"] == ""

    def test_add_scope_task_ref(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_SCOPE_ADD", "beacon:e-1656")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_plan()
        kwargs = fake_client.calls[0][1]
        assert kwargs["task"] == "e-1656"

    def test_remove_scope_routes_to_remove_trek_scope(self, fake_client,
                                                     monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_SCOPE_REMOVE", "beacon:op-2")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_plan()
        name, kwargs = fake_client.calls[0]
        assert name == "remove_trek_scope"
        assert kwargs["operation"] == "op-2"


class TestHaltCloudDispatch:
    def test_stop_routes_to_set_trek_halt(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_REASON", "found bug")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_stop()
        name, kwargs = fake_client.calls[0]
        assert name == "set_trek_halt"
        assert kwargs == {
            "trek_id": "tk-fake01",
            "issued_by_session_id": "sv-caller",
            "reason": "found bug",
        }

    def test_resume_routes_to_clear_trek_halt(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_resume()
        assert fake_client.calls[0] == (
            "clear_trek_halt", {"trek_id": "tk-fake01"},
        )


class TestTransferLeaderCloudDispatch:
    def test_transfer_routes_with_session_pair(self, fake_client, monkeypatch):
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_TO", "sv-target")
        monkeypatch.setenv("BEACON_SESSION_ID", "sv-caller")
        _capture_stdout(monkeypatch)
        commands.cmd_trek_transfer_leader()
        name, kwargs = fake_client.calls[0]
        assert name == "transfer_trek_leader"
        assert kwargs == {
            "trek_id": "tk-fake01",
            "from_session_id": "sv-caller",
            "to_session_id": "sv-target",
        }

    def test_transfer_requires_session_id_in_cloud(self, fake_client,
                                                   monkeypatch):
        monkeypatch.delenv("BEACON_SESSION_ID", raising=False)
        monkeypatch.setenv("BEACON_TREK_ID", "tk-fake01")
        monkeypatch.setenv("BEACON_TREK_TO", "sv-target")
        _capture_stdout(monkeypatch)
        with pytest.raises(SystemExit):
            commands.cmd_trek_transfer_leader()
        # Should NOT have called ApiClient — failed before dispatch.
        assert fake_client.calls == []


# ---------------------------------------------------------------------------
# ApiClient method signature smoke tests (no HTTP — verify URL + body shape)
# ---------------------------------------------------------------------------

class TestApiClientTrekMethodPaths:
    """Pin the HTTP path/method/body shape each ApiClient trek method emits."""

    def _make_client(self):
        from api_client import ApiClient
        client = ApiClient("http://fake.local")
        recorded: list[tuple[str, str, dict | None]] = []

        def fake_request(method, path, body=None):
            recorded.append((method, path, body))
            return {"trek_id": "tk-x"}

        client._request = fake_request
        return client, recorded

    def test_create_trek_posts_to_root(self):
        client, recorded = self._make_client()
        client.create_trek(title="T", creator_session_id="sv",
                           description="d", type_="temporary")
        assert recorded[0][0] == "POST"
        assert recorded[0][1] == "/api/treks"
        assert recorded[0][2] == {
            "title": "T", "description": "d", "type": "temporary",
            "creator_session_id": "sv",
        }

    def test_list_treks_builds_qs(self):
        client, recorded = self._make_client()
        client.list_treks(status="active", include_archived=True,
                          all_actors=True)
        method, path, body = recorded[0]
        assert method == "GET"
        assert "status=active" in path
        assert "include_archived=true" in path
        assert "all_actors=true" in path

    def test_get_trek_path(self):
        client, recorded = self._make_client()
        client.get_trek("tk-abc")
        assert recorded[0] == ("GET", "/api/treks/tk-abc", None)

    def test_patch_trek_only_includes_set_fields(self):
        client, recorded = self._make_client()
        client.patch_trek("tk-abc", title="new title")
        assert recorded[0] == ("PATCH", "/api/treks/tk-abc",
                               {"title": "new title"})

    def test_archive_trek_uses_delete(self):
        client, recorded = self._make_client()
        client.archive_trek("tk-abc")
        assert recorded[0] == ("DELETE", "/api/treks/tk-abc", None)

    def test_start_trek_posts(self):
        client, recorded = self._make_client()
        client.start_trek("tk-abc")
        assert recorded[0] == ("POST", "/api/treks/tk-abc/start", None)

    def test_invite_trek_member_body(self):
        client, recorded = self._make_client()
        client.invite_trek_member("tk-abc", "x@y")
        assert recorded[0] == ("POST", "/api/treks/tk-abc/members",
                               {"email": "x@y"})

    def test_join_trek_path(self):
        client, recorded = self._make_client()
        client.join_trek("tk-abc")
        assert recorded[0] == ("POST", "/api/treks/tk-abc/members/join", None)

    def test_leave_trek_uses_delete_me(self):
        client, recorded = self._make_client()
        client.leave_trek("tk-abc")
        assert recorded[0] == ("DELETE", "/api/treks/tk-abc/members/me", None)

    def test_add_trek_scope_uses_put_with_optional_fields(self):
        client, recorded = self._make_client()
        client.add_trek_scope("tk-abc", project="p", milestone="ms-1")
        assert recorded[0] == (
            "PUT", "/api/treks/tk-abc/scope",
            {"project": "p", "milestone": "ms-1"},
        )

    def test_remove_trek_scope_uses_delete_with_body(self):
        client, recorded = self._make_client()
        client.remove_trek_scope("tk-abc", project="p", task="e-9")
        assert recorded[0] == (
            "DELETE", "/api/treks/tk-abc/scope",
            {"project": "p", "task": "e-9"},
        )

    def test_set_trek_halt_uses_put(self):
        client, recorded = self._make_client()
        client.set_trek_halt("tk-abc", issued_by_session_id="sv-x",
                             reason="r")
        assert recorded[0] == (
            "PUT", "/api/treks/tk-abc/halt",
            {"issued_by_session_id": "sv-x", "reason": "r"},
        )

    def test_clear_trek_halt_uses_delete(self):
        client, recorded = self._make_client()
        client.clear_trek_halt("tk-abc")
        assert recorded[0] == ("DELETE", "/api/treks/tk-abc/halt", None)

    def test_transfer_trek_leader_body(self):
        client, recorded = self._make_client()
        client.transfer_trek_leader("tk-abc", from_session_id="sv-1",
                                    to_session_id="sv-2")
        assert recorded[0] == (
            "POST", "/api/treks/tk-abc/transfer-leader",
            {"from_session_id": "sv-1", "to_session_id": "sv-2"},
        )

    def test_get_trek_summary_path(self):
        client, recorded = self._make_client()
        client.get_trek_summary("tk-abc")
        assert recorded[0] == ("GET", "/api/treks/tk-abc/summary", None)
