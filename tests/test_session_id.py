"""Unit tests for ``lib/session.py`` — session identification (ms-57 / e-1035).

Locks down the contract that the rest of ms-57 builds on:

* mint shape: ``{agent_slug}-{epoch_ms}-{8 hex nonce}``
* freshness reuse: a recent .beacon/session.json is reused as-is
* stale → new mint: an aged-out session is replaced
* missing / corrupt session.json is treated as "no session yet"
* heartbeat bumps ``last_active`` on every call but never re-mints inside
  the freshness window
* ``BEACON_SESSION_FRESH_SECONDS`` env override is honored

We do NOT freeze the clock for the format test — instead we assert against
the regex shape, which keeps the test robust to second-level skew.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402
import agent  # noqa: E402  (used to confirm actor flows through)


SESSION_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{13}-[0-9a-f]{8}$")


@pytest.fixture
def project_dir(monkeypatch):
    """CWD = a temp dir with an empty .beacon/ folder.

    Each test starts with no session.json and a clean env so we can drive
    the lifecycle (mint → reuse → stale-mint) deterministically.
    """
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".beacon").mkdir()
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_SESSION_FRESH_SECONDS", raising=False)
        monkeypatch.delenv("BEACON_AGENT_PARENT", raising=False)
        monkeypatch.delenv("BEACON_AGENT_CHILD_ID", raising=False)
        # ms-57 e-1035 merge: ensure the env-var-first path is disabled in
        # tests that assert the mint format / freshness reuse path.
        # env-var-first behaviour is covered in tests/test_session.py.
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        try:
            yield Path(tmp)
        finally:
            # Windows can't remove a directory that is the process CWD, so the
            # TemporaryDirectory cleanup would raise WinError 32. Step out of the
            # temp project before cleanup (no-op on POSIX). ms-57.
            os.chdir(tempfile.gettempdir())


def _read_session_file(project_dir: Path) -> dict:
    p = project_dir / ".beacon" / "session.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Mint format
# ---------------------------------------------------------------------------

def test_mint_session_id_matches_documented_shape():
    sid = session._mint_session_id({"agent": "mac-claude.local", "machine": "macbook"})
    assert SESSION_ID_RE.match(sid), sid


def test_mint_session_id_handles_empty_actor():
    """Empty / missing actor still produces a non-empty, well-formed id."""
    sid = session._mint_session_id({})
    assert SESSION_ID_RE.match(sid), sid
    assert sid.startswith("unknown-"), sid


def test_slugify_collapses_non_alnum_and_runs():
    assert session._slugify("Mac-Claude.local") == "mac-claude-local"
    assert session._slugify("  weird!!  name  ") == "weird-name"
    assert session._slugify("") == "unknown"
    assert session._slugify("---") == "unknown"


# ---------------------------------------------------------------------------
# get_or_mint_session: lifecycle
# ---------------------------------------------------------------------------

def test_first_call_mints_and_persists(project_dir):
    s = session.get_or_mint_session()
    assert s["minted"] is True
    assert SESSION_ID_RE.match(s["session_id"])
    assert s["actor"]["agent"]
    assert s["actor"]["machine"]
    assert s["created_at"]
    assert s["last_active"] == s["created_at"]
    assert s["harness"]
    # Persisted to disk
    on_disk = _read_session_file(project_dir)
    assert on_disk["session_id"] == s["session_id"]
    assert "minted" not in on_disk  # transient flag must not leak


def test_second_call_within_freshness_reuses(project_dir):
    first = session.get_or_mint_session()
    second = session.get_or_mint_session()
    assert second["minted"] is False
    assert second["session_id"] == first["session_id"]


def test_stale_session_triggers_new_mint(project_dir, monkeypatch):
    """When last_active is older than the freshness threshold, mint anew."""
    monkeypatch.setenv("BEACON_SESSION_FRESH_SECONDS", "60")
    first = session.get_or_mint_session()

    # Backdate the on-disk last_active by 2 hours.
    stale_iso = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = _read_session_file(project_dir)
    stored["last_active"] = stale_iso
    session.write_session(stored)

    second = session.get_or_mint_session()
    assert second["minted"] is True
    assert second["session_id"] != first["session_id"]


def test_missing_session_json_is_first_mint(project_dir):
    """No file == no session yet."""
    assert session.read_session() == {}
    s = session.get_or_mint_session()
    assert s["minted"] is True


def test_corrupt_session_json_is_replaced(project_dir):
    (project_dir / ".beacon" / "session.json").write_text("not json", encoding="utf-8")
    assert session.read_session() == {}
    s = session.get_or_mint_session()
    assert s["minted"] is True
    # The corrupt file must have been overwritten with a valid mint.
    assert _read_session_file(project_dir)["session_id"] == s["session_id"]


# ---------------------------------------------------------------------------
# Heartbeat (deprecated path — see test_session_heartbeat_responsibility.py
# for the post-e-1319 pure-getter contract)
# ---------------------------------------------------------------------------

def test_update_last_active_does_not_bump_last_active(project_dir):
    """ms-54 e-1319: update_last_active is now a deprecated no-op shim.

    Pre-e-1319 it bumped ``last_active`` per call so the directory could see
    a CLI session as live. Post Option C (PR #111) the bridge owns that
    signal — a CLI bump only creates ambiguity. The function is retained as
    a shim so external scripts importing the symbol keep working, but it
    deliberately does NOT mutate last_active anymore.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        first = session.update_last_active()
        time.sleep(1.05)  # ensure second-precision ISO timestamp would advance
        second = session.update_last_active()
    assert second["session_id"] == first["session_id"]
    # last_active must NOT advance — bridge is the truth source now.
    assert second["last_active"] == first["last_active"]


def test_update_last_active_does_not_re_mint_within_window(project_dir):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        first_id = session.update_last_active()["session_id"]
        for _ in range(3):
            assert session.update_last_active()["session_id"] == first_id


def test_update_last_active_persists_actor_snapshot(project_dir, monkeypatch):
    """Once minted, the actor recorded in session.json stays stable across calls.

    Even if the environment that determines actor identity changes mid-session
    (e.g. agent.json is rewritten), subsequent resolves must not silently
    rewrite the actor — that would defeat the purpose of session-level
    attribution. Post e-1319 update_last_active is a shim around the same
    get_or_mint_session path, so the invariant is unchanged.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        first = session.update_last_active()
        original_actor = dict(first["actor"])
        # Simulate agent.json mutation mid-session: rewrite actor file.
        (project_dir / ".beacon" / "agent.json").write_text(
            json.dumps({"name": "different-agent"}), encoding="utf-8"
        )
        second = session.update_last_active()
    assert second["session_id"] == first["session_id"]
    assert second["actor"] == original_actor


# ---------------------------------------------------------------------------
# get_session_id convenience
# ---------------------------------------------------------------------------

def test_get_session_id_mints_when_needed(project_dir):
    sid = session.get_session_id()
    assert SESSION_ID_RE.match(sid)
    assert sid == _read_session_file(project_dir)["session_id"]


def test_get_session_id_reuses_existing(project_dir):
    first = session.get_session_id()
    second = session.get_session_id()
    assert first == second


# ---------------------------------------------------------------------------
# _is_fresh (pure)
# ---------------------------------------------------------------------------

def test_is_fresh_handles_missing_input():
    assert session._is_fresh("", "2026-06-05T15:00:00Z", 60) is False


def test_is_fresh_handles_corrupt_input():
    assert session._is_fresh("not-iso", "2026-06-05T15:00:00Z", 60) is False


def test_is_fresh_within_window():
    assert session._is_fresh(
        "2026-06-05T14:59:30Z", "2026-06-05T15:00:00Z", 60
    ) is True


def test_is_fresh_outside_window():
    assert session._is_fresh(
        "2026-06-05T14:50:00Z", "2026-06-05T15:00:00Z", 60
    ) is False


def test_is_fresh_future_timestamp_is_not_fresh():
    """A last_active in the future must not count as fresh — that would
    mean a clock skew or tamper made dead sessions look alive."""
    assert session._is_fresh(
        "2026-06-05T15:10:00Z", "2026-06-05T15:00:00Z", 60
    ) is False


# ---------------------------------------------------------------------------
# Freshness threshold env override
# ---------------------------------------------------------------------------

def test_freshness_threshold_default():
    assert session._freshness_threshold() == 3600


def test_freshness_threshold_env_override(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_FRESH_SECONDS", "300")
    assert session._freshness_threshold() == 300


def test_freshness_threshold_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_FRESH_SECONDS", "garbage")
    assert session._freshness_threshold() == 3600


def test_freshness_threshold_negative_env_falls_back(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_FRESH_SECONDS", "-1")
    assert session._freshness_threshold() == 3600


# ---------------------------------------------------------------------------
# Cloud sync (ms-57 / e-1063) — slice 2
# ---------------------------------------------------------------------------

def _write_cloud_config(project_dir: Path, mode: str = "cloud") -> None:
    """Switch the project into cloud mode by writing .beacon/cloud.json.

    e-1861 (ms-61): cloud.json existence is the sole source of truth for
    cloud mode. The legacy config.json ``{"mode": "cloud"}`` write was
    retired (silent-drift attack surface). ``mode`` arg kept for back-compat
    with existing call sites — only ``"cloud"`` materialises cloud.json;
    any other value is treated as a no-op (= still local).
    """
    if mode == "cloud":
        (project_dir / ".beacon" / "cloud.json").write_text(
            json.dumps({"project_id": "test-pid", "api_url": "https://example.test"}),
            encoding="utf-8",
        )


def test_is_cloud_mode_detects_cloud_json(project_dir):
    """e-1861 (ms-61): cloud.json existence triggers cloud mode."""
    assert session._is_cloud_mode() is False
    _write_cloud_config(project_dir)
    assert session._is_cloud_mode() is True


def test_is_cloud_mode_honors_env(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_CLOUD", "1")
    assert session._is_cloud_mode() is True


def test_is_cloud_mode_ignores_legacy_config_mode_field(project_dir):
    """e-1861 (ms-61) reproduction: silent-drift incident prevention.

    On 2026-06-15 a sub-agent overwrote ``.beacon/config.json`` to
    ``{"mode": "local"}`` and the CLI silently flipped off cloud, causing
    a data-loss panic. After e-1861 the config.json ``mode`` field is
    structurally ignored — only cloud.json existence matters.

    This test pins that contract: even with config.json set to
    ``{"mode": "local"}``, as long as cloud.json exists, cloud mode stays
    on. Reverse case (cloud.json absent + config.json ``{"mode": "cloud"}``)
    must NOT trigger cloud mode either.
    """
    # Case 1: cloud.json present + hostile config.json => still cloud
    _write_cloud_config(project_dir)
    (project_dir / ".beacon" / "config.json").write_text(
        json.dumps({"mode": "local"}), encoding="utf-8"
    )
    assert session._is_cloud_mode() is True, (
        "Silent-drift regression: a hostile config.json mode=local must not "
        "disable cloud mode when cloud.json is present."
    )
    # Case 2: cloud.json absent + ghost config.json => not cloud
    (project_dir / ".beacon" / "cloud.json").unlink()
    (project_dir / ".beacon" / "config.json").write_text(
        json.dumps({"mode": "cloud"}), encoding="utf-8"
    )
    assert session._is_cloud_mode() is False, (
        "Phantom-cloud regression: a config.json mode=cloud must not enable "
        "cloud mode when cloud.json is absent (would crash on first cloud op)."
    )


def test_should_cloud_sync_when_never_synced():
    assert session._should_cloud_sync("") is True


def test_should_cloud_sync_when_corrupt():
    assert session._should_cloud_sync("not-iso") is True


def test_should_cloud_sync_debounce_recent(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", "60")
    recent = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert session._should_cloud_sync(recent) is False


def test_should_cloud_sync_debounce_elapsed(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", "60")
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert session._should_cloud_sync(old) is True


def test_cloud_debounce_default():
    assert session._cloud_debounce_seconds() == 30


def test_cloud_debounce_env_override(monkeypatch):
    monkeypatch.setenv("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", "0")
    assert session._cloud_debounce_seconds() == 0
    monkeypatch.setenv("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", "120")
    assert session._cloud_debounce_seconds() == 120


def test_update_last_active_no_cloud_in_local_mode(project_dir, monkeypatch):
    """Local mode: update_last_active must never call _cloud_sync.

    Post e-1319 this is now stronger than before: _cloud_sync is unreachable
    from update_last_active in *any* mode (the heartbeat-side cloud sync was
    moved into the bridge in PR #111). The test stays as a regression net —
    if a future change accidentally re-introduces a CLI-side cloud write,
    this catches it immediately.
    """
    sentinel = {"called": 0}

    def _fake_sync(_payload):
        sentinel["called"] += 1
        return True

    monkeypatch.setattr(session, "_cloud_sync", _fake_sync)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session.update_last_active()
        session.update_last_active()
    assert sentinel["called"] == 0


def test_update_last_active_does_not_cloud_sync_post_e1319(project_dir, monkeypatch):
    """ms-54 e-1319: cloud sync moved out of update_last_active.

    Pre-e-1319 update_last_active triggered _cloud_sync in cloud mode; now
    the bridge does that via PUT /sessions/{id} on every poll iteration.
    This test pins the new contract: even with cloud.json present and
    BEACON_CLOUD=1 forced, update_last_active calls zero _cloud_sync
    invocations.
    """
    _write_cloud_config(project_dir)
    monkeypatch.setenv("BEACON_CLOUD", "1")
    calls = []
    monkeypatch.setattr(session, "_cloud_sync", lambda p: (calls.append(1) or True))

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session.update_last_active()
        session.update_last_active()
        session.update_last_active()
    assert calls == [], (
        "update_last_active must not cloud-sync post e-1319 — bridge owns that path"
    )


# ---------------------------------------------------------------------------
# Bridge claim (e-1331 quick fix)
#
# When a bus.mjs bridge is running in this cwd, it writes its session_id to
# .beacon/bridge.json so CLI commands can target the SAME session_id the
# bridge uses for DM delivery. These tests pin the resolution order:
#
#   * live claim ⇒ resolve_active_session_id returns the bridge's value
#   * stale claim (dead pid) ⇒ fall through to the existing mint path
#   * absent claim ⇒ identical to get_session_id (back-compat)
# ---------------------------------------------------------------------------

def _write_bridge_claim(project_dir: Path, session_id: str, pid: int) -> None:
    (project_dir / ".beacon" / "bridge.json").write_text(json.dumps({
        "session_id": session_id,
        "pid": pid,
        "cwd": str(project_dir),
        "started_at": "2026-06-09T07:00:00.000000Z",
    }), encoding="utf-8")


def test_resolve_active_session_id_prefers_live_bridge_claim(
        project_dir, monkeypatch):
    """A bus.mjs writing .beacon/bridge.json with a live pid is the
    authoritative source. The CLI must target that session_id, not mint a
    new one from CLAUDE_CODE_SESSION_ID."""
    # An env var that WOULD override the existing session.json without
    # this fix.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-env-mismatch")
    _write_bridge_claim(project_dir, "bridge-owned-sid", os.getpid())
    assert session.resolve_active_session_id() == "bridge-owned-sid"


def test_resolve_active_session_id_ignores_dead_pid_claim(
        project_dir, monkeypatch):
    """A bridge crash leaves a stale .beacon/bridge.json behind. The pid
    liveness check drops it so the CLI doesn't address a dead session."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_bridge_claim(project_dir, "ghost-bridge-sid", pid=2 ** 30)
    sid = session.resolve_active_session_id()
    assert sid != "ghost-bridge-sid"
    # Falls through to the mint path → produces a fresh session_id.
    assert SESSION_ID_RE.match(sid), sid


def test_resolve_active_session_id_absent_claim_falls_through(project_dir):
    """No bridge.json → behaves exactly like get_session_id, preserving
    the existing mint/reuse contract for users who haven't run bus.mjs."""
    assert not (project_dir / ".beacon" / "bridge.json").exists()
    sid = session.resolve_active_session_id()
    assert sid == session.get_session_id()


def test_read_bridge_session_returns_empty_on_malformed_json(
        project_dir):
    """A torn write (partial JSON) must degrade to 'no claim' rather than
    raise — the CLI should never blow up on a transient FS state."""
    (project_dir / ".beacon" / "bridge.json").write_text("{not json")
    assert session.read_bridge_session() == {}
    sid = session.resolve_active_session_id()
    assert SESSION_ID_RE.match(sid), sid


def test_read_bridge_session_returns_empty_when_pid_field_missing(
        project_dir):
    """A claim without a pid is treated as alive (can't refute liveness).
    This makes the file backward-compatible if a future bridge omits the
    field; the CLI still uses the session_id."""
    (project_dir / ".beacon" / "bridge.json").write_text(json.dumps({
        "session_id": "no-pid-sid",
    }))
    assert session.read_bridge_session().get("session_id") == "no-pid-sid"
    assert session.resolve_active_session_id() == "no-pid-sid"


# ---------------------------------------------------------------------------
# e-2531: Codex receive-loop session pointer adoption in resolve_active_session_id
# ---------------------------------------------------------------------------

def _write_codex_pointer(project_dir: Path, sid: str) -> None:
    d = project_dir / ".beacon" / "codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "receive-loop.session.json").write_text(
        json.dumps({"session_id": sid, "project_id": "p-x"}),
        encoding="utf-8",
    )


def test_read_codex_session_pointer_missing_returns_empty(project_dir):
    assert session.read_codex_session_pointer() == ""


def test_read_codex_session_pointer_reads_sid(project_dir):
    _write_codex_pointer(project_dir, "codex-123-abc")
    assert session.read_codex_session_pointer() == "codex-123-abc"


def test_read_codex_session_pointer_corrupt_returns_empty(project_dir):
    d = project_dir / ".beacon" / "codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "receive-loop.session.json").write_text("{ not json", encoding="utf-8")
    assert session.read_codex_session_pointer() == ""


def test_resolve_adopts_codex_pointer_over_fresh_mint(project_dir, monkeypatch):
    """A Codex send with no BEACON_BUS_SENDER must adopt the daemon's stable
    codex- sid instead of minting a fresh sv- (the e-2531 churn).

    e-3091: the pointer adoption is now gated to EXCLUDE claude-code callers,
    so this test must represent a genuine non-claude-code caller. We clear the
    ambient CLAUDECODE / BEACON_BUS_SENDER (the pytest process inherits
    CLAUDECODE=1 when the suite is run from a Claude Code shell) so
    _caller_agent_kind() resolves "" (unknown) — which still adopts the pointer.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("BEACON_BUS_SENDER", raising=False)
    monkeypatch.setattr(session, "read_bridge_session", lambda: {})
    _write_codex_pointer(project_dir, "codex-STABLE-1")
    assert session.resolve_active_session_id() == "codex-STABLE-1"


def test_resolve_prefers_bridge_claim_over_codex_pointer(project_dir, monkeypatch):
    """A Claude send matches the pid-tree bridge claim first; the codex pointer
    (if any lingers in the cwd) must not override it."""
    monkeypatch.setattr(session, "read_bridge_session",
                        lambda: {"session_id": "sv-CLAUDE-CLAIM"})
    _write_codex_pointer(project_dir, "codex-SHOULD-NOT-WIN")
    assert session.resolve_active_session_id() == "sv-CLAUDE-CLAIM"


def test_resolve_falls_through_to_mint_when_no_pointer(project_dir, monkeypatch):
    """No bridge claim, no codex pointer → normal mint chain (non-empty sv-)."""
    monkeypatch.setattr(session, "read_bridge_session", lambda: {})
    sid = session.resolve_active_session_id()
    assert sid and not sid.startswith("codex-")
