"""End-to-end pin for the Codex autonomous push-receive loop (ms-93 / e-2519 AC 7).

The idle→DM→autonomous-reply loop was only exercised by running the whole
daemon, so nothing pinned that "user away → DM arrives → app-server runs →
reply sent back" actually closes. This drives the extracted seam
``lib.codex_receive_loop.dispatch_kept_event`` with fakes (no real Codex,
no real bus, no network) so the loop is verified deterministically and
regressions are caught structurally.

The only fakes are the *edges* — the app-server child (returns canned
streamed notifications) and the reply shell-out (captures argv). The
in-between orchestration (agent-text reconstruction + reply argv building)
uses the REAL functions, so this is a genuine end-to-end of the in-process
loop, not a mock of it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "beacon"


def _load_receive_loop_module():
    path = REPO_ROOT / "lib" / "codex_receive_loop.py"
    spec = importlib.util.spec_from_file_location(
        "codex_receive_loop_e2e", path
    )
    m = importlib.util.module_from_spec(spec)
    # bus_protocol is a sibling module under lib/.
    if str(REPO_ROOT / "lib") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "lib"))
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _load_app_server_client_module():
    path = PLUGIN_ROOT / "scripts" / "beacon_codex_app_server_client.py"
    spec = importlib.util.spec_from_file_location(
        "beacon_codex_app_server_client_e2e", path
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


# The real agent-text reconstruction function (= not a mock).
def _real_agent_text_fn():
    client = _load_app_server_client_module()
    return client.agent_message_text_from_notifications


class _FakeAppServer:
    """Stands in for the ``codex app-server`` child.

    ``dispatch_dm_and_wait`` returns a response carrying streamed
    notifications shaped exactly like the real app-server would emit for a
    completed turn, so the real ``agent_message_text_from_notifications``
    can reconstruct the reply text.
    """

    def __init__(self, agent_text="hello back", raise_exc=None):
        self._agent_text = agent_text
        self._raise = raise_exc
        self.dispatched = []

    def dispatch_dm_and_wait(self, evt):
        self.dispatched.append(evt)
        if self._raise is not None:
            raise self._raise
        return {
            "_notifications": [
                {
                    "method": "item/completed",
                    "params": {
                        "item": {"type": "agentMessage", "text": self._agent_text},
                    },
                },
            ]
        }


class _ReplyCapture:
    """Captures the reply shell-out and returns a fake process result."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self._rc = returncode
        self._stderr = stderr

    def __call__(self, argv, *, sender_session_id):
        self.calls.append({"argv": list(argv), "sender": sender_session_id})

        class _Proc:
            returncode = self._rc
            stderr = self._stderr

        return _Proc()


def _make_evt(event_id="evt-1", sender="sv-other-abc"):
    return {"event_id": event_id, "sender_session_id": sender}


# ------------------------------------------------------------------ #
# The core AC 7 scenario: idle → DM → armed autonomous reply
# ------------------------------------------------------------------ #


def test_armed_dm_produces_autonomous_reply_end_to_end():
    crl = _load_receive_loop_module()
    acked = []
    app = _FakeAppServer(agent_text="got it, working on it")
    reply = _ReplyCapture(returncode=0)

    res = crl.dispatch_kept_event(
        _make_evt(event_id="evt-42", sender="sv-peer-9"),
        app_server_client=app,
        agent_text_fn=_real_agent_text_fn(),
        ack_fn=lambda eid: acked.append(eid),
        armed=True,
        sender_session_id="sv-codex-self",
        beacon_bin="/abs/beacon",
        run_reply=reply,
    )

    # The DM reached the app-server child (= idle wake fired).
    assert app.dispatched == [_make_evt(event_id="evt-42", sender="sv-peer-9")]
    # The event was acked as opened (= the AI "saw" it).
    assert acked == ["evt-42"]
    # An autonomous reply was actually sent.
    assert res["replied"] is True
    assert res["reason"] == "replied"

    # The reply is a well-formed, correctly-threaded bus send.
    assert len(reply.calls) == 1
    argv = reply.calls[0]["argv"]
    assert argv[:3] == ["/abs/beacon", "bus", "send"]
    assert "--channel" in argv and "dm" in argv
    assert argv[argv.index("--to") + 1] == "sv-peer-9"
    assert argv[argv.index("--in-reply-to") + 1] == "evt-42"
    payload = json.loads(argv[argv.index("--payload") + 1])
    assert payload["text"] == "got it, working on it"
    # The reply is attributed to this daemon's sid, not a freshly minted one.
    assert reply.calls[0]["sender"] == "sv-codex-self"


def test_reply_reconstructs_text_from_streamed_deltas():
    """Even without a completed item, deltas alone must form the reply."""
    crl = _load_receive_loop_module()

    class _DeltaApp:
        def dispatch_dm_and_wait(self, evt):
            return {
                "_notifications": [
                    {"method": "item/agentMessage/delta", "params": {"delta": "par"}},
                    {"method": "item/agentMessage/delta", "params": {"delta": "tial"}},
                ]
            }

    reply = _ReplyCapture()
    res = crl.dispatch_kept_event(
        _make_evt(),
        app_server_client=_DeltaApp(),
        agent_text_fn=_real_agent_text_fn(),
        ack_fn=lambda eid: None,
        armed=True,
        sender_session_id="sv-self",
        run_reply=reply,
    )
    assert res["replied"] is True
    payload = json.loads(reply.calls[0]["argv"][reply.calls[0]["argv"].index("--payload") + 1])
    assert payload["text"] == "partial"


# ------------------------------------------------------------------ #
# The armed gate: no reply unless explicitly armed (= e-2519 AC 6)
# ------------------------------------------------------------------ #


def test_unarmed_dm_dispatches_and_acks_but_never_replies():
    crl = _load_receive_loop_module()
    acked = []
    app = _FakeAppServer(agent_text="I would reply if armed")
    reply = _ReplyCapture()

    res = crl.dispatch_kept_event(
        _make_evt(),
        app_server_client=app,
        agent_text_fn=_real_agent_text_fn(),
        ack_fn=lambda eid: acked.append(eid),
        armed=False,
        sender_session_id="sv-self",
        run_reply=reply,
    )

    # Dispatch + ack still happen (= pull-on-prompt semantics preserved).
    assert app.dispatched  # the DM was seen
    assert acked == ["evt-1"]
    # But no autonomous reply is built or sent without explicit arming.
    assert res["replied"] is False
    assert res["reply_argv"] is None
    assert res["reason"] == "not-armed"
    assert reply.calls == []


# ------------------------------------------------------------------ #
# Spam / safety guards
# ------------------------------------------------------------------ #


def test_armed_but_empty_agent_text_skips_reply():
    """An empty agent turn must not send a noise reply (= spam guard)."""
    crl = _load_receive_loop_module()
    reply = _ReplyCapture()
    res = crl.dispatch_kept_event(
        _make_evt(),
        app_server_client=_FakeAppServer(agent_text="   "),
        agent_text_fn=_real_agent_text_fn(),
        ack_fn=lambda eid: None,
        armed=True,
        sender_session_id="sv-self",
        run_reply=reply,
    )
    assert res["replied"] is False
    assert res["reply_argv"] is None
    assert res["reason"] == "reply-skipped-empty-or-unaddressable"
    assert reply.calls == []


def test_armed_reply_failure_is_reported_not_raised():
    """A non-zero reply send must be surfaced in the result, not crash."""
    crl = _load_receive_loop_module()
    logs = []
    reply = _ReplyCapture(returncode=1, stderr="budget exhausted")
    res = crl.dispatch_kept_event(
        _make_evt(),
        app_server_client=_FakeAppServer(agent_text="hi"),
        agent_text_fn=_real_agent_text_fn(),
        ack_fn=lambda eid: None,
        armed=True,
        sender_session_id="sv-self",
        run_reply=reply,
        log=lambda msg, err=False: logs.append((msg, err)),
    )
    assert res["replied"] is False
    assert res["reason"] == "reply-failed-rc-1"
    # The failure was logged to the error stream for the user to re-grant budget.
    assert any(err and "reply failed" in msg for msg, err in logs)


# ------------------------------------------------------------------ #
# Transport failure propagates (= daemon reconnect contract)
# ------------------------------------------------------------------ #


def test_dispatch_transport_error_propagates_for_reconnect():
    """A dead app-server transport must raise so the daemon reconnects.

    Swallowing it here would poison every later DM (the bug the daemon's
    reconnect handler exists to prevent).
    """
    crl = _load_receive_loop_module()
    app = _FakeAppServer(raise_exc=ConnectionError("websocket closed"))
    with pytest.raises(ConnectionError):
        crl.dispatch_kept_event(
            _make_evt(),
            app_server_client=app,
            agent_text_fn=_real_agent_text_fn(),
            ack_fn=lambda eid: None,
            armed=True,
            sender_session_id="sv-self",
            run_reply=_ReplyCapture(),
        )
