"""Unit tests for `beacon operation envelope verify` (ms-60 / e-1340).

Cover the AI self-check primitive: given an operation id and a requested
action, return whether the action is permitted by the current active
envelope. Mocks the api_client + cloud config helpers.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import commands  # noqa: E402
import cmd_trigger  # noqa: E402  (ms-127 e-4971: _push_operation_trigger_to_bus の canonical home)
import cmd_operation  # noqa: E402  (ms-127 e-4798: operation handlers live here)


PROJECT_ID = "test-proj"
OP_ID = "op-1"


class _FakeClient:
    def __init__(self) -> None:
        self.list_returns: list[dict] = []

    def list_operation_envelopes(self, project_id, op_id, *, status=None):
        return list(self.list_returns)


@pytest.fixture
def fake_client(monkeypatch):
    fc = _FakeClient()

    def _get_api_client():
        return fc, {"project_id": PROJECT_ID}

    monkeypatch.setattr(cmd_operation, "_get_api_client", _get_api_client)
    monkeypatch.setattr(cmd_operation, "_is_cloud_mode", lambda: True)
    return fc


def _set_envvars(monkeypatch, op_id=OP_ID, action="", json_mode=False):
    monkeypatch.setenv("BEACON_OPERATION_ID", op_id)
    monkeypatch.setenv("BEACON_ACTION", action)
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")


# ---------------------------------------------------------------------------
# Happy path: permitted
# ---------------------------------------------------------------------------

def test_verify_permitted_exact_match(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["deploy:v0.21.x", "extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is True
    assert result["active"] is True
    assert result["envelope_id"] == "env-1"


def test_verify_permitted_wildcard(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*", "task done:e-*"],
    }]
    _set_envvars(monkeypatch, action="extract:profile:user-42", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["permitted"] is True


def test_verify_permitted_prefix_wildcard(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["task done:e-*"],
    }]
    _set_envvars(monkeypatch, action="task done:e-1234", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["permitted"] is True


# ---------------------------------------------------------------------------
# Not permitted
# ---------------------------------------------------------------------------

def test_verify_rejected_outside_scope(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is False
    assert result["active"] is True
    assert "outside approved_actions" in result["reason"]


def test_verify_rejected_no_active_envelope(fake_client, monkeypatch, capsys):
    fake_client.list_returns = []  # no active envelope
    _set_envvars(monkeypatch, action="extract:profile:user-1", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["permitted"] is False
    assert result["active"] is False
    assert result["envelope_id"] is None
    assert "no active envelope" in result["reason"]


def test_verify_rejected_different_depth(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],  # 3 segments
    }]
    # Requested action has only 2 segments — must not match (matcher requires
    # equal depth).
    _set_envvars(monkeypatch, action="extract:profile", json_mode=True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Usage errors (exit code 2)
# ---------------------------------------------------------------------------

def test_verify_missing_op_id(monkeypatch, capsys):
    _set_envvars(monkeypatch, op_id="", action="x:y:z")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "operation id required" in capsys.readouterr().err


def test_verify_missing_action(monkeypatch, capsys):
    _set_envvars(monkeypatch, action="")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "action required" in capsys.readouterr().err


def test_verify_rejects_local_mode(monkeypatch, capsys):
    _set_envvars(monkeypatch, action="x:y:z")
    monkeypatch.setattr(cmd_operation, "_is_cloud_mode", lambda: False)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 2
    assert "cloud mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Human-readable output (non-JSON mode)
# ---------------------------------------------------------------------------

def test_verify_human_output_permitted(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["deploy:v0.21.x"],
    }]
    _set_envvars(monkeypatch, action="deploy:v0.21.x")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "permitted" in out
    assert "deploy:v0.21.x" in out


def test_verify_human_output_rejected(fake_client, monkeypatch, capsys):
    fake_client.list_returns = [{
        "envelope_id": "env-1",
        "status": "active",
        "approved_actions": ["extract:profile:*"],
    }]
    _set_envvars(monkeypatch, action="deploy:v1.0")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_operation_envelope_verify()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not permitted" in out
    assert "outside approved_actions" in out


# ---------------------------------------------------------------------------
# _push_operation_trigger_to_bus — best-effort, must not throw on local mode
# ---------------------------------------------------------------------------

def test_push_operation_trigger_local_mode_safe(monkeypatch):
    """When no cloud.json exists, the helper should silently return without
    raising. Local-mode users must not see errors from this autonomous-path
    plumbing (ms-60 / e-1340)."""
    # ms-108 e-5194 follow-up: patch the name in cmd_trigger — the module where
    # _push_operation_trigger_to_bus is defined and resolves _get_cloud_config_path
    # from its own globals. Patching commands._get_cloud_config_path (a re-export)
    # had no effect on the helper's lookup, so the local-mode early-return never
    # fired and the helper reached the real cwd cloud.json → posted op-1 "test"
    # events to the live bus every suite run.
    monkeypatch.setattr(
        cmd_trigger, "_get_cloud_config_path",
        lambda: "/nonexistent/cloud.json",
    )
    # Must not raise.
    commands._push_operation_trigger_to_bus(
        "op-1", "test_source", {"name": "operation_check_op-1",
                                "message": "test", "created_at": "2026-06-09"},
        spec_doc_id="doc-1",
    )


# ---------------------------------------------------------------------------
# e-1393: envelope mint contract — without an envelope the server's verify
# pipeline degrades `auto-execute` to `notify-user-only`, which the inbox
# hook never injects → autonomous loop silently breaks. Pin the T2 mint
# call AND its forwarding to post_bus_event so a future refactor can't
# regress the silent-degrade path.
# ---------------------------------------------------------------------------

class _EnvelopeCaptureClient:
    """Records `issue_bus_envelope` + `post_bus_event` calls."""
    def __init__(self):
        self.envelope_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized=None,
                           scope=None, data_class="free", **_kw):
        self.envelope_calls.append({
            "project_id": project_id,
            "tier": tier,
            "actions_authorized": list(actions_authorized or []),
            "scope": scope,
            "data_class": data_class,
        })
        return {"tier": tier, "scope": scope, "signature": "stub-sig",
                "actions_authorized": list(actions_authorized or [])}

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai",
                       envelope=None, requested_action=None):
        self.post_calls.append({
            "project_id": project_id, "channel": channel,
            "sender_session_id": sender_session_id, "payload": payload or {},
            "delivery": delivery, "envelope": envelope,
        })
        return {"event_id": f"e-{len(self.post_calls)}", "delivery": delivery}


@pytest.fixture
def cloud_mode(tmp_path, monkeypatch):
    """Bare cloud-mode project so `_push_operation_trigger_to_bus` reaches
    the post path. Caller wires the ApiClient stub via `cloud_client`."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))
    monkeypatch.setattr(commands, "_get_cloud_config_path",
                        lambda: str(beacon_dir / "cloud.json"))
    fake_auth = type(sys)("auth")
    fake_auth.load_credentials = lambda: {"token": "fake"}
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setattr(commands, "_extract_token", lambda c: "fake-token")
    # ms-127 e-4971: _push_operation_trigger_to_bus moved to cmd_trigger and
    # resolves _get_cloud_config_path / _extract_token in ITS namespace, so
    # mirror the patches (patching commands alone no longer intercepts).
    monkeypatch.setattr(cmd_trigger, "_get_cloud_config_path",
                        commands._get_cloud_config_path)
    monkeypatch.setattr(cmd_trigger, "_extract_token", commands._extract_token)
    return beacon_dir


@pytest.fixture
def cloud_client(monkeypatch, cloud_mode):
    stub = _EnvelopeCaptureClient()
    fake_api = type(sys)("api_client")
    fake_api.ApiClient = lambda url, token: stub
    monkeypatch.setitem(sys.modules, "api_client", fake_api)
    return stub


def test_push_operation_trigger_mints_t2_envelope(cloud_client):
    """e-1393: the fire-time bus event must carry a T2 envelope scoped to
    the op_id. Without it the server degrades delivery to
    `notify-user-only` and the autonomous loop never starts."""
    commands._push_operation_trigger_to_bus(
        "op-2", "envelope-tests",
        {"name": "operation_check_op-2", "message": "scheduled",
         "created_at": "2026-06-09"},
        spec_doc_id="doc-abc",
    )
    assert len(cloud_client.envelope_calls) == 1, \
        "envelope must be minted before posting"
    env_call = cloud_client.envelope_calls[0]
    assert env_call["tier"] == "T2"
    assert env_call["scope"] == "op:op-2", \
        "T2 envelope requires scope=op:<op_id> per server/envelope.py:299"
    assert "operation.trigger.fire" in env_call["actions_authorized"]


def test_push_operation_trigger_forwards_envelope_to_post(cloud_client):
    """The minted envelope must be passed to post_bus_event(envelope=...)
    so the server's verify pipeline keeps delivery=auto-execute. A future
    refactor that drops the keyword would silently regress to
    notify-user-only without breaking any other test."""
    commands._push_operation_trigger_to_bus(
        "op-2", "envelope-tests",
        {"name": "operation_check_op-2", "message": "scheduled",
         "created_at": "2026-06-09"},
        spec_doc_id="doc-abc",
    )
    assert len(cloud_client.post_calls) == 1
    post = cloud_client.post_calls[0]
    assert post["channel"] == "operation-trigger"
    assert post["delivery"] == "auto-execute"
    assert post["envelope"] is not None
    assert post["envelope"]["tier"] == "T2"
    assert post["envelope"]["scope"] == "op:op-2"


def test_push_operation_trigger_falls_back_when_envelope_mint_fails(
        cloud_mode, monkeypatch, capsys):
    """If envelope issuance raises (e.g. server unreachable, older API),
    the post still goes out — without an envelope, so the server will
    degrade delivery. This is the safe failure mode: noisy stderr +
    notify-user-only (still surfaces via .beacon/bus-inbox.log) beats
    crashing the trigger fire path."""
    posts: list[dict] = []

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        def issue_bus_envelope(self, *_a, **_kw):
            raise RuntimeError("API error 404: legacy server")

        def post_bus_event(self, project_id, channel, *,
                           sender_session_id="", payload=None,
                           delivery="propose-to-ai", envelope=None,
                           requested_action=None):
            posts.append({"envelope": envelope, "delivery": delivery})
            return {"event_id": "e-1", "delivery": "notify-user-only"}

    fake_api = type(sys)("api_client")
    fake_api.ApiClient = _Client
    monkeypatch.setitem(sys.modules, "api_client", fake_api)
    commands._push_operation_trigger_to_bus(
        "op-3", "x", {"name": "n", "message": "m", "created_at": "2026-06-09"},
        spec_doc_id="d",
    )
    assert len(posts) == 1
    assert posts[0]["envelope"] is None  # fell through
    assert posts[0]["delivery"] == "auto-execute"  # request stays as intent
    err = capsys.readouterr().err
    assert "envelope mint failed" in err
