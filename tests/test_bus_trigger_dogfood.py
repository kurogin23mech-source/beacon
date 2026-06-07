"""Tests for the trigger → bus mirror (ms-54 / e-1136 UC2 dogfood).

`beacon trigger fire` now posts a propose-to-ai bus event on the side so
every cloud-connected session subscribing through the inbox hook sees the
trigger naturally. These tests lock in three contracts:

  * the mirror **never** breaks the trigger file write (best-effort) — if
    cloud creds are missing or the API raises, the local trigger still
    lands intact;
  * the posted event uses the conventional channel/schema (channel="trigger",
    payload={trigger_name, message, created_at}, delivery=propose-to-ai,
    no sender — these are system events);
  * idempotency is preserved at the trigger layer: re-firing the same
    trigger name does NOT re-post (because the file-exists guard short-
    circuits the whole path).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


class _StubApiClient:
    def __init__(self):
        self.posts: list[dict] = []

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai"):
        self.posts.append({
            "project_id": project_id,
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
        })
        return {"event_id": f"e-{len(self.posts)}"}


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A bare cloud-mode beacon project so the mirror's cloud-path detection
    succeeds. Local-mode projects (no cloud.json) are tested separately."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "test", "milestones": []}))
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def stub_client(monkeypatch):
    stub = _StubApiClient()

    # Patch the auth + api_client lookup that _push_trigger_to_bus uses. We
    # don't go through _get_api_client because the mirror constructs its own
    # client to avoid pulling in the heavy CLI config plumbing.
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: {"id_token": "fake"}
    monkeypatch.setitem(sys.modules, "auth", fake_auth)

    monkeypatch.setattr(commands, "_extract_token", lambda creds: "fake-token")

    fake_api = type(sys)("api_client")
    fake_api.ApiClient = lambda url, token: stub
    monkeypatch.setitem(sys.modules, "api_client", fake_api)

    return stub


# ---------------------------------------------------------------------------
# Local trigger file is the source of truth — bus is additive
# ---------------------------------------------------------------------------

def test_fire_writes_local_trigger_file(project_dir, monkeypatch, stub_client):
    """Sanity: the existing file-creation behavior is unchanged. The bus
    mirror must layer on without disturbing the local source of truth."""
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "v0.17.0 candidate")
    commands.cmd_trigger_fire()
    path = project_dir / ".beacon" / "triggers" / "release-due.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["name"] == "release-due"
    assert data["message"] == "v0.17.0 candidate"


def test_fire_posts_to_bus_with_conventional_schema(project_dir, monkeypatch,
                                                     stub_client):
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "v0.17.0 candidate")
    commands.cmd_trigger_fire()
    assert len(stub_client.posts) == 1
    post = stub_client.posts[0]
    assert post["project_id"] == "proj-1"
    # Channel "trigger" lets receivers filter system events from session DMs.
    assert post["channel"] == "trigger"
    # System events are sender-less so the inbox hook can render "from=system".
    assert post["sender_session_id"] == ""
    # propose-to-ai is the conservative default — never auto-execute a
    # release just because the trigger fired.
    assert post["delivery"] == "propose-to-ai"
    payload = post["payload"]
    assert payload["trigger_name"] == "release-due"
    assert payload["message"] == "v0.17.0 candidate"
    assert payload["created_at"]


# ---------------------------------------------------------------------------
# Idempotency at the trigger layer cascades to the bus mirror
# ---------------------------------------------------------------------------

def test_re_firing_same_trigger_does_not_re_post(project_dir, monkeypatch,
                                                  stub_client):
    """trigger_fire short-circuits if the file already exists. The bus mirror
    must inherit that idempotency — otherwise a polling cron could spam the
    bus with duplicate release-due events every minute."""
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "v0.17.0")
    commands.cmd_trigger_fire()
    commands.cmd_trigger_fire()  # second fire is a no-op
    commands.cmd_trigger_fire()
    assert len(stub_client.posts) == 1


# ---------------------------------------------------------------------------
# Best-effort: every failure path must preserve the trigger file
# ---------------------------------------------------------------------------

def test_fire_succeeds_in_local_mode_without_cloud_json(tmp_path, monkeypatch):
    """Local-mode projects (no cloud.json) must still fire triggers. The bus
    mirror is silently skipped."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "local", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "retro-day")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "weekly")
    commands.cmd_trigger_fire()
    assert (beacon_dir / "triggers" / "retro-day.json").exists()


def test_fire_succeeds_when_credentials_are_missing(project_dir, monkeypatch):
    """A logged-out user must still be able to fire triggers locally. The
    mirror is best-effort, not load-bearing."""
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: None  # logged out
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    commands.cmd_trigger_fire()
    assert (project_dir / ".beacon" / "triggers" / "release-due.json").exists()


def test_fire_succeeds_when_cloud_api_raises(project_dir, monkeypatch):
    """The mirror's cloud call must not propagate — a 5xx, timeout, or DNS
    failure leaves the local trigger file intact."""
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: {"id_token": "fake"}
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setattr(commands, "_extract_token", lambda c: "tok")

    class _BoomClient:
        def __init__(self, *_a, **_kw):
            pass

        def post_bus_event(self, *_a, **_kw):
            raise RuntimeError("simulated 503")

    fake_api = type(sys)("api_client")
    fake_api.ApiClient = _BoomClient
    monkeypatch.setitem(sys.modules, "api_client", fake_api)

    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    commands.cmd_trigger_fire()
    assert (project_dir / ".beacon" / "triggers" / "release-due.json").exists()


# ---------------------------------------------------------------------------
# Receiver-side picture (handled by inbox hook) — payload shape contract
# ---------------------------------------------------------------------------

def test_payload_carries_enough_for_inbox_hook_to_render(project_dir,
                                                          monkeypatch,
                                                          stub_client):
    """The inbox hook prints `channel`, `delivery`, `from`, and `payload` for
    each event. The trigger payload must surface the trigger name + message
    so the AI's reading of the inject is self-contained."""
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "operation-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "service health: review run")
    commands.cmd_trigger_fire()
    payload = stub_client.posts[0]["payload"]
    # name + message together give the AI enough to suggest what to do
    # ("run /beacon-operation-review for op-X") without a second round trip.
    assert "operation-due" in payload["trigger_name"]
    assert "service health" in payload["message"]
