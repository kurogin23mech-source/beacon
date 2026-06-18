"""Tests for the Trek-aware trigger system (ms-75 / e-1870).

`beacon trigger fire --trek <id> <name>` posts a bus event on the
``trek-trigger`` channel with ``delivery=auto-execute`` + a T2 Trek-
scope envelope, twin of the ``operation-trigger`` path.

These tests lock in:

  * the local trigger file persists ``trek_id`` so on-disk readers can
    detect a trek-scoped trigger without hitting the bus;
  * the bus post uses channel="trek-trigger", payload includes trek_id,
    delivery="auto-execute" (= same channel/delivery contract as
    operation-trigger, exposed via a different envelope scope);
  * an envelope mint failure does NOT block the bus post — the path
    degrades gracefully without raising;
  * cloud / creds missing paths still write the local trigger file
    (best-effort, matches existing ``trigger`` channel contract);
  * an explicit non-Trek fire (= no --trek) still posts on the legacy
    ``trigger`` channel with ``delivery=propose-to-ai`` (backwards
    compatibility regression guard).
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
    """Capture every bus post + envelope mint so tests can pin the contract."""

    def __init__(self, *, mint_raises: bool = False):
        self.posts: list[dict] = []
        self.envelopes: list[dict] = []
        self._mint_raises = mint_raises

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized,
                           scope, data_class):
        if self._mint_raises:
            raise RuntimeError("simulated envelope mint failure")
        env = {
            "envelope_id": f"env-{len(self.envelopes) + 1}",
            "project_id": project_id,
            "tier": tier,
            "actions_authorized": list(actions_authorized),
            "scope": scope,
            "data_class": data_class,
        }
        self.envelopes.append(env)
        return env

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai",
                       envelope=None):
        self.posts.append({
            "project_id": project_id,
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
            "envelope": envelope,
        })
        return {"event_id": f"e-{len(self.posts)}"}


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """Cloud-mode beacon project so the mirror's cloud-path detection succeeds."""
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


def _install_stub(monkeypatch, stub):
    """Common stub plumbing — auth, _extract_token, ApiClient."""
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: {"id_token": "fake"}
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setattr(commands, "_extract_token", lambda c: "fake-token")
    fake_api = type(sys)("api_client")
    fake_api.ApiClient = lambda url, token: stub
    monkeypatch.setitem(sys.modules, "api_client", fake_api)


# ---------------------------------------------------------------------------
# On-disk: trek_id persists on the trigger file
# ---------------------------------------------------------------------------

def test_trek_fire_persists_trek_id_on_local_file(project_dir, monkeypatch):
    """The local trigger json must carry trek_id so `beacon trigger check`
    readers (= session-start, dispatch) can detect trek-scoped triggers
    without re-querying the bus event."""
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "trek-start")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "begin partial-update work")
    monkeypatch.setenv("BEACON_TRIGGER_TREK_ID", "tk-abc12345")
    # No stub installed → bus mirror silently skips on missing auth, but
    # the local write must still complete.
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: None
    monkeypatch.setitem(sys.modules, "auth", fake_auth)

    commands.cmd_trigger_fire()

    path = project_dir / ".beacon" / "triggers" / "trek-start.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["name"] == "trek-start"
    assert data["message"] == "begin partial-update work"
    assert data["trek_id"] == "tk-abc12345"


def test_legacy_fire_omits_trek_id(project_dir, monkeypatch):
    """A `beacon trigger fire <name>` without --trek must NOT add a trek_id
    key to the local file (= backwards compat for retro / release readers)."""
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "v0.41.0")
    monkeypatch.delenv("BEACON_TRIGGER_TREK_ID", raising=False)
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: None
    monkeypatch.setitem(sys.modules, "auth", fake_auth)

    commands.cmd_trigger_fire()

    data = json.loads(
        (project_dir / ".beacon" / "triggers" / "release-due.json").read_text()
    )
    assert "trek_id" not in data


# ---------------------------------------------------------------------------
# Bus post: channel + delivery + payload shape
# ---------------------------------------------------------------------------

def test_trek_fire_posts_to_trek_trigger_channel(project_dir, monkeypatch):
    """Trek-aware fire posts on the dedicated ``trek-trigger`` channel with
    ``delivery=auto-execute`` and a T2 Trek-scope envelope. Twin of the
    operation-trigger contract."""
    stub = _StubApiClient()
    _install_stub(monkeypatch, stub)

    monkeypatch.setenv("BEACON_TRIGGER_NAME", "trek-start")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "begin partial-update work")
    monkeypatch.setenv("BEACON_TRIGGER_TREK_ID", "tk-abc12345")

    commands.cmd_trigger_fire()

    assert len(stub.posts) == 1
    post = stub.posts[0]
    # Channel separation from operation-trigger lets receivers opt in
    # per-scope.
    assert post["channel"] == "trek-trigger"
    # auto-execute is the autonomous path — Skill side honors the opt-in
    # allowlist (default OFF) so this is safe.
    assert post["delivery"] == "auto-execute"
    # System events are sender-less.
    assert post["sender_session_id"] == ""
    # Payload carries trek_id so the inbox hook can lift it into the
    # "TREK ACTION" block without inspecting the envelope.
    assert post["payload"]["trek_id"] == "tk-abc12345"
    assert post["payload"]["trigger_name"] == "trek-start"
    assert post["payload"]["message"] == "begin partial-update work"
    assert post["payload"]["created_at"]
    # Envelope is T2 Trek-scope per SPEC e-1393 reasoning (server-side
    # verify pipeline preserves auto-execute when envelope is present).
    env = post["envelope"]
    assert env is not None
    assert env["tier"] == "T2"
    assert env["scope"] == "trek:tk-abc12345"
    assert env["actions_authorized"] == ["trek.trigger.fire"]


def test_legacy_fire_still_uses_trigger_channel(project_dir, monkeypatch):
    """Backwards compat: --trek omitted ⇒ legacy ``trigger`` channel +
    ``propose-to-ai`` delivery, no envelope."""
    stub = _StubApiClient()
    _install_stub(monkeypatch, stub)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "release-due")
    monkeypatch.setenv("BEACON_TRIGGER_MESSAGE", "v0.41.0")
    monkeypatch.delenv("BEACON_TRIGGER_TREK_ID", raising=False)

    commands.cmd_trigger_fire()

    assert len(stub.posts) == 1
    post = stub.posts[0]
    assert post["channel"] == "trigger"
    assert post["delivery"] == "propose-to-ai"
    # No envelope on the legacy path — the trigger channel is system-level
    # and does not need a tiered envelope.
    assert post["envelope"] is None


# ---------------------------------------------------------------------------
# Best-effort: failures don't break the local trigger file write
# ---------------------------------------------------------------------------

def test_trek_fire_survives_envelope_mint_failure(project_dir, monkeypatch):
    """If the envelope mint raises, the bus post must still happen — the
    server falls back to the legacy verify path (= delivery downgrade
    handled there). The local trigger file is unaffected."""
    stub = _StubApiClient(mint_raises=True)
    _install_stub(monkeypatch, stub)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "trek-resume")
    monkeypatch.setenv("BEACON_TRIGGER_TREK_ID", "tk-fail0000")

    commands.cmd_trigger_fire()

    # Bus post still attempted, just without an envelope.
    assert len(stub.posts) == 1
    post = stub.posts[0]
    assert post["channel"] == "trek-trigger"
    assert post["envelope"] is None
    # Local trigger file still written.
    path = project_dir / ".beacon" / "triggers" / "trek-resume.json"
    assert path.exists()
    assert json.loads(path.read_text())["trek_id"] == "tk-fail0000"


def test_trek_fire_in_local_mode_skips_bus_quietly(tmp_path, monkeypatch):
    """Local-mode (= no cloud.json) projects must still write the trigger
    file. The bus mirror silently no-ops since there's no project_id."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "local", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "trek-local")
    monkeypatch.setenv("BEACON_TRIGGER_TREK_ID", "tk-local0000")

    commands.cmd_trigger_fire()

    path = beacon_dir / "triggers" / "trek-local.json"
    assert path.exists()
    assert json.loads(path.read_text())["trek_id"] == "tk-local0000"


def test_trek_fire_idempotent_on_existing_file(project_dir, monkeypatch):
    """trigger_fire short-circuits if the file already exists. The bus
    mirror inherits that idempotency so a polling caller cannot spam the
    trek-trigger channel by re-firing the same name."""
    stub = _StubApiClient()
    _install_stub(monkeypatch, stub)
    monkeypatch.setenv("BEACON_TRIGGER_NAME", "trek-resume")
    monkeypatch.setenv("BEACON_TRIGGER_TREK_ID", "tk-once00000")

    commands.cmd_trigger_fire()
    commands.cmd_trigger_fire()  # no-op
    commands.cmd_trigger_fire()  # no-op

    assert len(stub.posts) == 1
