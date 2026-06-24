"""Pin the session-close → live-directory cleanup propagation (ms-95 / e-2305).

Background
----------
Before this fix, the shutdown stamp on the cloud session document was
written ONLY by ``channel/bus.mjs`` on SIGINT / SIGTERM graceful exit.
That single path silently fails when the bridge process outlives its
Claude Code parent (= MCP stdio shutdown unreliable on some hosts), so
the directory's ``healthy_only`` filter keeps advertising the session as
receive-capable indefinitely. The koki / kyozai reproduction
(2026-06-23, DM event P8U3Mi0URSfWdBdxkGHV) saw three sessions stack up
``healthy=true`` for hours.

Fix
---
Add a second, CLI-driven shutdown stamp path: ``beacon session end`` and
``beacon session rescue`` both call ``_stamp_cloud_session_shutdown``,
which writes ``shutdown=true`` + ``last_poll_at=now`` to the cloud
session doc via ``ApiClient.upsert_session``. This converges on the same
server-side merge field bus.mjs uses (integrity-safe via Firestore
merge=True semantics) but does NOT depend on the bridge process's
liveness or its signal-delivery ordering.

Test coverage
-------------
* The helper writes shutdown=true (positive case, cloud mode).
* The helper is a no-op in local mode (no API client minted).
* ``cmd_session_end`` invokes the helper exactly once with the resolved
  session_id — proving the propagation path is wired end-to-end.
* ``cmd_session_rescue`` invokes the helper for each rescued orphan.
* Helper failures (raises during upsert) MUST NOT abort session-end —
  the local session log persistence is the primary truth source.
* Body shape matches the SessionUpsert wire schema (last_active +
  last_poll_at + shutdown), pinning the contract with server/app.py
  ``_compute_poll_health`` (which short-circuits ``healthy=False`` iff
  shutdown=True regardless of last_poll_at freshness).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import session  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs + fixtures
# ---------------------------------------------------------------------------

class _StubApiClient:
    """Captures upsert_session calls so the test can assert body shape."""

    def __init__(self):
        self.upsert_calls: list[dict] = []

    def upsert_session(self, project_id: str, session_id: str, data: dict) -> dict:
        self.upsert_calls.append({
            "project_id": project_id,
            "session_id": session_id,
            "data": dict(data),
        })
        return {"status": "ok", "session_id": session_id}


class _RaisingApiClient:
    """upsert_session raises — exercises the best-effort try/except path."""

    def __init__(self):
        self.calls = 0

    def upsert_session(self, project_id: str, session_id: str, data: dict) -> dict:
        self.calls += 1
        raise RuntimeError("simulated cloud transient failure")


@pytest.fixture
def project_dir(monkeypatch):
    """Minimal Beacon project on disk so commands.cmd_session_end runs."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-agent"}), encoding="utf-8",
        )
        (beacon_dir / "project.json").write_text(
            json.dumps({
                "name": "t",
                "milestones": [{
                    "id": "ms-95",
                    "title": "test",
                    "status": "in_progress",
                    "entries": [],
                    "progress": 0,
                }],
            }),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        # Block the actual cloud push so the test stays hermetic; the
        # local cache write is the side effect we observe.
        monkeypatch.setattr(commands, "_push_session_log_to_cloud", lambda p: False)
        try:
            yield Path(tmp)
        finally:
            os.chdir(tempfile.gettempdir())


def _wire_cloud_mode(monkeypatch, stub_client) -> object:
    """Force the helper to take the cloud-mode branch + return ``stub_client``
    from ``_get_api_client``. Also stubs load_credentials so the auth gate
    inside _stamp_cloud_session_shutdown does not require real tokens.

    Patches ``LocalStore.is_cloud`` at the class level so every
    ``get_store()`` call (which mints a fresh instance) is affected,
    without replacing get_store itself (= other call sites in
    cmd_session_end / cmd_session_rescue still get a real Store).
    """
    import store_local
    monkeypatch.setattr(store_local.LocalStore, "is_cloud", lambda self: True)
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub_client, {"project_id": "proj-1"}),
    )
    # auth.load_credentials is imported inside the helper — stub the
    # module-level symbol so the inline `from auth import load_credentials`
    # picks it up.
    import auth
    monkeypatch.setattr(auth, "load_credentials", lambda: {"token": "stub"})
    return stub_client


# ---------------------------------------------------------------------------
# Helper-level pinning
# ---------------------------------------------------------------------------


def test_stamp_helper_writes_shutdown_true_with_last_poll_at(project_dir, monkeypatch):
    """Positive case: cloud mode, valid project_id → helper PUTs a body
    containing the three fields the server's _compute_poll_health reads
    to decide ``healthy=False`` immediately (= shutdown=True short-circuit
    documented in server/app.py:4823-4827)."""
    stub = _wire_cloud_mode(monkeypatch, _StubApiClient())

    ok = commands._stamp_cloud_session_shutdown("sv-test-123")

    assert ok is True
    assert len(stub.upsert_calls) == 1
    call = stub.upsert_calls[0]
    assert call["project_id"] == "proj-1"
    assert call["session_id"] == "sv-test-123"
    body = call["data"]
    assert body["shutdown"] is True
    assert body["last_poll_at"]
    assert body["last_active"]
    # last_active and last_poll_at must come from the same moment so the
    # server-side merge writes an internally-consistent row.
    assert body["last_active"] == body["last_poll_at"]


def test_stamp_helper_is_noop_in_local_mode(project_dir, monkeypatch):
    """Local-mode projects have no cloud session subcollection to stamp.
    Helper returns False without raising and without minting an ApiClient.

    The project_dir fixture creates only ``.beacon/project.json`` (no
    ``cloud.json``), so the real LocalStore reports ``is_cloud()=False``
    naturally — no patching needed.
    """
    # If the helper falls through to _get_api_client, it would exit(1)
    # because cloud.json doesn't exist. We assert the helper never gets
    # there by patching _get_api_client to fail loudly if invoked.
    def _fail(*_a, **_kw):
        raise AssertionError("local mode must not call _get_api_client")
    monkeypatch.setattr(commands, "_get_api_client", _fail)

    ok = commands._stamp_cloud_session_shutdown("sv-test-123")
    assert ok is False


def test_stamp_helper_swallows_transport_failure(project_dir, monkeypatch):
    """Cloud transient blip → helper logs nothing visible, returns False,
    does NOT raise. Session-end MUST keep working even when the cloud is
    unreachable (the local session log is the primary truth source)."""
    stub = _wire_cloud_mode(monkeypatch, _RaisingApiClient())

    ok = commands._stamp_cloud_session_shutdown("sv-test-123")

    assert ok is False
    assert stub.calls == 1  # Helper attempted the call before swallowing.


def test_stamp_helper_returns_false_on_empty_session_id(project_dir, monkeypatch):
    """Guard rail: an empty session_id is a programming error, not a cloud
    write target. Helper returns False without minting anything (the empty-id
    check short-circuits BEFORE the is_cloud branch)."""
    import store_local
    monkeypatch.setattr(store_local.LocalStore, "is_cloud", lambda self: True)

    def _fail(*_a, **_kw):
        raise AssertionError("empty sid must short-circuit before client mint")
    monkeypatch.setattr(commands, "_get_api_client", _fail)

    assert commands._stamp_cloud_session_shutdown("") is False


# ---------------------------------------------------------------------------
# Integration: cmd_session_end / cmd_session_rescue propagation
# ---------------------------------------------------------------------------


def test_session_end_stamps_shutdown_for_current_session(project_dir, monkeypatch):
    """AC #1 + #2 of e-2305: ``beacon session end`` must propagate a
    shutdown stamp to the cloud session doc so the directory's
    healthy_only filter drops this row immediately. Without the fix the
    bridge's natural ``last_poll_at`` aging is the only signal, which
    silently fails when the bridge outlives Claude Code (e-2305 motivation)."""
    sid = session.get_session_id()
    stub = _wire_cloud_mode(monkeypatch, _StubApiClient())
    monkeypatch.setenv("BEACON_JSON", "1")

    with redirect_stdout(io.StringIO()):
        commands.cmd_session_end()

    assert len(stub.upsert_calls) == 1, stub.upsert_calls
    call = stub.upsert_calls[0]
    assert call["session_id"] == sid
    assert call["data"]["shutdown"] is True


def test_session_end_succeeds_when_cloud_stamp_fails(project_dir, monkeypatch):
    """Best-effort contract: a cloud transient blip must NOT abort
    session-end. The local session log (= primary truth source for
    next-session restore) must still be written."""
    sid = session.get_session_id()
    _wire_cloud_mode(monkeypatch, _RaisingApiClient())
    monkeypatch.setenv("BEACON_JSON", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_end()
    payload = json.loads(buf.getvalue())
    assert payload["session_id"] == sid

    # Local cache must exist regardless of cloud-stamp outcome.
    cache_path = project_dir / ".beacon" / "session_logs" / f"{sid}.json"
    assert cache_path.exists()


def test_session_rescue_stamps_shutdown_for_each_orphan(project_dir, monkeypatch):
    """Rescued sessions are by definition orphan (= original bclaude tab
    is no longer driving them). The directory should not keep
    advertising them as receive-capable. Pins that cmd_session_rescue
    stamps shutdown=true on every orphan it aggregates."""
    # Seed two orphan sessions by writing commits attributed to them.
    pf = project_dir / ".beacon" / "project.json"
    data = json.loads(pf.read_text(encoding="utf-8"))
    data["milestones"][0]["entries"].extend([
        {"id": "e-101", "type": "commit", "description": "x",
         "status": "done",
         "meta": {"hash": "aaa1111", "session_id": "orphan-A"}},
        {"id": "e-102", "type": "commit", "description": "y",
         "status": "done",
         "meta": {"hash": "bbb2222", "session_id": "orphan-B"}},
    ])
    pf.write_text(json.dumps(data), encoding="utf-8")

    stub = _wire_cloud_mode(monkeypatch, _StubApiClient())
    monkeypatch.setenv("BEACON_JSON", "1")
    with redirect_stdout(io.StringIO()):
        commands.cmd_session_rescue()

    stamped_sids = sorted(c["session_id"] for c in stub.upsert_calls)
    assert "orphan-A" in stamped_sids
    assert "orphan-B" in stamped_sids
    # Every stamp must carry shutdown=true.
    assert all(c["data"]["shutdown"] is True for c in stub.upsert_calls)


def test_session_end_in_local_mode_does_not_attempt_cloud_call(project_dir, monkeypatch):
    """A local-mode project (no cloud.json, no auth) MUST NOT mint an
    ApiClient inside session-end's shutdown-stamp path. The helper's
    is_cloud short-circuit protects this; pin it at the command level so
    a future refactor doesn't accidentally regress to an always-on
    cloud call.

    project_dir creates no cloud.json, so the real LocalStore reports
    is_cloud()=False — no patching needed beyond the _get_api_client
    tripwire below.
    """
    def _fail(*_a, **_kw):
        raise AssertionError("local mode must not call _get_api_client")
    monkeypatch.setattr(commands, "_get_api_client", _fail)

    monkeypatch.setenv("BEACON_JSON", "1")
    with redirect_stdout(io.StringIO()):
        commands.cmd_session_end()  # MUST NOT raise.
