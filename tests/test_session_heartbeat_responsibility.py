"""Heartbeat responsibility separation tests (ms-54 / e-1319).

Background
----------
Pre-e-1319 the heartbeat picture had two truth sources:

  1. **Bridge** (PR #111 / commit 78048b6, e-1318): per-poll-iteration write
     of ``last_active`` + ``last_poll_at`` from channel/bus-heartbeat.mjs.
     Structural — if the poll loop dies, both stamps stop advancing.

  2. **CLI** (ms-57 e-1035): every ``beacon <subcmd>`` invocation called
     ``session.update_last_active()`` from the main dispatcher, which
     bumped ``last_active`` and pushed to the cloud ``sessions/`` subcollection.

Two writes, one signal — a session could heartbeat ``last_active`` from
the CLI while its bridge poll loop was dead, leaving the directory
advertising a receiver that couldn't actually receive. The dogfood incident
that motivated e-1319: DMs accumulated server-side while the AI inbox stayed
empty, because ``last_active`` looked fine.

The cleanup
-----------
Split the responsibilities so the contract is structural, not by convention:

  * **mint + heartbeat = bridge**  (poll-gated, owns last_active + last_poll_at)
  * **resolve = CLI**              (`beacon session id` is a pure getter)
  * **lifecycle close = `beacon session end`** (graceful shutdown stamp)

This file pins the new contract:

  (a) ``session.get_session_id()`` is read-only — does not mutate
      ``last_active`` on subsequent calls and never cloud-syncs.
  (b) ``beacon session id`` CLI command goes through the pure-getter path
      (does not call ``session.update_last_active``).
  (c) ``/beacon-session-start`` Step 0b is no-op — no longer invokes the
      CLI heartbeat (verified by reading the SKILL.md ships in the repo).
  (d) ``list_sessions`` response carries a structural ``bridge: bool``
      field so directory consumers can filter on "has a poll loop" without
      re-implementing the ``last_poll_at`` presence check.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import session  # noqa: E402


# ---------------------------------------------------------------------------
# (a) get_session_id is a pure getter
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """tmp project root with .beacon/ present so session.json mints cleanly."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    return tmp_path


def _read_session_file(root: Path) -> dict:
    p = root / ".beacon" / "session.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_get_session_id_does_not_mutate_last_active_on_reuse(project_dir):
    """First call mints (writes session.json); subsequent calls must not
    bump last_active. The bridge owns that signal post e-1319."""
    sid = session.get_session_id()
    first = _read_session_file(project_dir)
    assert first["session_id"] == sid

    # Several subsequent reads.
    for _ in range(5):
        assert session.get_session_id() == sid

    second = _read_session_file(project_dir)
    # The file MUST be byte-for-byte stable across reads (no last_active bump).
    assert first == second, (
        "get_session_id mutated session.json on a pure read — "
        "responsibility separation broken; heartbeat belongs to the bridge"
    )


def test_get_session_id_does_not_cloud_sync(project_dir, monkeypatch):
    """Even in cloud mode, get_session_id must not push to the
    ``sessions/`` subcollection. The bridge does that via
    PUT /sessions/{id} per poll iteration (e-1318)."""
    # Force cloud mode without writing real cloud.json (avoids needing auth).
    monkeypatch.setenv("BEACON_CLOUD", "1")
    calls = []
    monkeypatch.setattr(session, "_cloud_sync", lambda payload: calls.append(payload) or True)

    for _ in range(3):
        session.get_session_id()

    assert calls == [], (
        "get_session_id triggered a cloud sync — that path now belongs to the bridge"
    )


# ---------------------------------------------------------------------------
# (b) `beacon session id` CLI uses the pure-getter path
# ---------------------------------------------------------------------------

def test_cmd_session_id_uses_pure_getter(project_dir, monkeypatch, capsys):
    """cmd_session_id must not call update_last_active — that's the
    deprecated heartbeat path. It should use get_session_id (pure)."""
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    import commands  # noqa: E402

    # Track which API the command actually uses.
    update_calls = []
    real_update = session.update_last_active

    def _spy_update():
        update_calls.append(1)
        return real_update()

    monkeypatch.setattr(session, "update_last_active", _spy_update)

    # cmd_session_id is sys.exit on error; happy path prints + returns.
    commands.cmd_session_id()
    out = capsys.readouterr().out.strip()
    assert out, "cmd_session_id printed nothing"
    assert update_calls == [], (
        "cmd_session_id called the deprecated update_last_active path — "
        "post e-1319 it should resolve via the pure getter only"
    )


def test_cmd_session_id_does_not_bump_last_active(project_dir, capsys):
    """End-to-end: invoking cmd_session_id twice must not advance
    last_active in session.json (proof the CLI path is pure read)."""
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    import commands  # noqa: E402

    commands.cmd_session_id()
    capsys.readouterr()
    snapshot_a = _read_session_file(project_dir)

    # Second invocation right after — last_active must be byte-identical.
    commands.cmd_session_id()
    capsys.readouterr()
    snapshot_b = _read_session_file(project_dir)

    assert snapshot_a == snapshot_b, (
        "two cmd_session_id calls mutated session.json — pure-getter contract broken"
    )


# ---------------------------------------------------------------------------
# (c) /beacon-session-start Step 0b is no-op (SKILL.md content check)
# ---------------------------------------------------------------------------

def test_session_start_skill_step_0b_is_no_op():
    """The repo's copy of the session-start SKILL doc must declare Step 0b
    as retired (no `beacon session id` invocation in the step body).

    We assert structural properties of the doc rather than running the
    Skill — the Skill is interpreted by Claude Code at runtime, so the
    canonical contract is "the instructions do not tell the agent to call
    the CLI heartbeat any more."
    """
    skill = REPO_ROOT / "skills" / "beacon-session-start.md"
    assert skill.exists(), "session-start Skill doc missing from repo"
    body = skill.read_text(encoding="utf-8")

    # The retirement marker is the source of truth — if this string is
    # gone, someone reverted the step without updating the test.
    assert "Step 0b: bus heartbeat — 廃止" in body, (
        "Step 0b retirement marker not found in SKILL.md — the doc was "
        "reverted without updating this test"
    )

    # Defense in depth: no `beacon session id` Bash invocation inside
    # the doc body. We allow the symbol to appear in prose (it's mentioned
    # in the migration note) but not as a code-fenced command.
    # Heuristic: a line containing only `beacon session id ...` inside a
    # ```bash block would be the regressing shape.
    import re
    bash_blocks = re.findall(r"```bash\n(.*?)\n```", body, re.DOTALL)
    for block in bash_blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("beacon session id"):
                pytest.fail(
                    f"SKILL.md still contains a `beacon session id` bash "
                    f"invocation: {stripped!r} — Step 0b should be a no-op"
                )


# ---------------------------------------------------------------------------
# (d) /api/projects/{pid}/sessions response carries a structural `bridge` field
# ---------------------------------------------------------------------------

def test_list_sessions_attaches_bridge_field():
    """server.app.list_sessions must stamp `bridge: bool` on every row so
    directory consumers can filter on "has a poll loop wired" without
    duplicating the last_poll_at presence check.

    We exercise the in-memory composition (the import is happy-path only —
    DB access is faked via a stub) rather than spin up a real server.
    """
    sys.path.insert(0, str(REPO_ROOT / "server"))

    # The app module imports a `db` symbol at the top — give it a stub so
    # the import doesn't try to open a real backend.
    import types
    fake_db = types.SimpleNamespace(
        list_sessions=lambda pid: [
            # Bridge-tagged session: has last_poll_at.
            {"session_id": "with-bridge", "actor": {},
             "last_active": "2026-06-09T00:00:00Z",
             "last_poll_at": "2026-06-09T00:00:00Z",
             "poll_interval_ms": 2000},
            # Legacy session: never polled.
            {"session_id": "no-bridge", "actor": {},
             "last_active": "2026-06-09T00:00:00Z"},
        ],
    )

    # ms-127 e-4871 PR2: list_sessions moved out of app.py into the
    # /api/projects/* collab router (nested inside make_collab_router), so it is
    # no longer importable as app.list_sessions. Build that router with a stub
    # _load and the *real* session-liveness helpers (the bridge/poll_health
    # composition under test), extract the GET /sessions endpoint, and call it
    # directly. The handler reads its own module-level `db` (= store_router), so
    # we patch store_router.list_sessions (not app.db) to feed the fake rows.
    import server.app as app_module  # type: ignore
    import routers_projects as rp  # type: ignore
    import store_router  # type: ignore

    class _StubUser(dict):
        pass

    real_store_list = store_router.list_sessions
    store_router.list_sessions = fake_db.list_sessions

    def _find_endpoint(router, path, method):
        for r in router.routes:
            if getattr(r, "path", None) == path and method in getattr(
                    r, "methods", set()):
                return r.endpoint
        raise LookupError(path)

    try:
        router = rp.make_collab_router(
            app_module.require_auth,
            _load=lambda pid, user=None: None,
            _load_meta_only=lambda *a, **k: None,
            _require_project_role=lambda *a, **k: None,
            _require_write=lambda *a, **k: None,
            _save=lambda *a, **k: None,
            _apply_op_and_broadcast=lambda *a, **k: None,
            _load_org_for_member=lambda *a, **k: None,
            # Real helpers — the bridge / poll_health composition is what we test.
            _session_is_live=app_module._session_is_live,
            _stamp_session_liveness=app_module._stamp_session_liveness,
            is_auth_enabled=lambda: False,
        )
        list_sessions = _find_endpoint(
            router, "/api/projects/{project_id}/sessions", "GET")
        rows = list_sessions(
            project_id="proj-x",
            user_id="",
            machine="",
            agent="",
            live_only=False,
            since_minutes=5,
            healthy_only=False,
            user=_StubUser(),
        )
    finally:
        store_router.list_sessions = real_store_list

    by_id = {r["session_id"]: r for r in rows}

    assert by_id["with-bridge"]["bridge"] is True
    assert by_id["no-bridge"]["bridge"] is False

    # Both rows must also carry poll_health (composition, not replacement).
    assert "poll_health" in by_id["with-bridge"]
    assert "poll_health" in by_id["no-bridge"]


def test_session_upsert_accepts_and_forwards_transport():
    """ms-145 / e-5378: the heartbeat carries receive-transport observability
    (ws_state / effective_poll_ms / counters). The SessionUpsert schema must
    accept it and the endpoint's `body.model_dump()` must forward it, so the
    directory can show which sessions run the WS accelerator vs poll-only and
    why. Pydantic default is extra='ignore', so without the explicit field the
    server would silently drop it (= the diagnostic signal would vanish)."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    import routers_projects as rp  # type: ignore

    transport = {"ws_state": "reconnecting", "effective_poll_ms": 5000,
                 "ws_opens": 0, "ws_closes": 3, "ws_last_close_code": 1006}
    body = rp.SessionUpsert(last_active="2026-09-06T00:00:00Z", transport=transport)
    assert body.transport == transport
    # The endpoint forwards non-None fields via model_dump(); transport must ride.
    forwarded = {k: v for k, v in body.model_dump().items() if v is not None}
    assert forwarded["transport"] == transport
