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
    """Switch the project into cloud mode by writing .beacon/config.json."""
    (project_dir / ".beacon" / "config.json").write_text(
        json.dumps({"mode": mode}), encoding="utf-8"
    )


def test_is_cloud_mode_detects_config(project_dir):
    assert session._is_cloud_mode() is False
    _write_cloud_config(project_dir)
    assert session._is_cloud_mode() is True


def test_is_cloud_mode_honors_env(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_CLOUD", "1")
    assert session._is_cloud_mode() is True


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
