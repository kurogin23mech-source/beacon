"""Tests pinned from the Windows-side e-1035 part 1 design, adapted to
the merged schema (dict payload + persistent marker for runtime state).

Why these stay separate from test_session_id.py:
  * They lock in the *env-var-first* identity contract (Windows finding).
  * They use ``monkeypatch.chdir(tmp_path)`` instead of the project_dir
    fixture in test_session_id.py, so the test surface for the env var
    path is decoupled from the heartbeat / cloud-sync surface.

The original Windows assertion "no marker file should be written when
the env id is used" was relaxed during merge: we DO write a marker
(``.beacon/session.json``) even when the id comes from the env, because
runtime state (last_active heartbeat, cloud_synced_at debounce) needs a
persistent home regardless of where the id was sourced.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402


def test_env_session_id_wins(monkeypatch, tmp_path):
    """CLAUDE_CODE_SESSION_ID, when set, overrides any minted fallback."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.setenv(session.ENV_SESSION_ID, "abc-123-uuid")
    assert session.get_session_id() == "abc-123-uuid"
    # Marker IS written (merged design: heartbeat needs persistent state).
    marker = tmp_path / ".beacon" / "session.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["session_id"] == "abc-123-uuid"
    assert data["source"] == "claude_code_env"


def test_env_session_id_stripped(monkeypatch, tmp_path):
    """Whitespace around the env value is trimmed before adoption."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.setenv(session.ENV_SESSION_ID, "  spaced-id  ")
    assert session.get_session_id() == "spaced-id"


def test_minted_when_no_env(monkeypatch, tmp_path):
    """No env var → mint and persist the marker for reuse."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    sid = session.get_session_id()
    assert sid
    marker = tmp_path / ".beacon" / "session.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["source"] == "minted"


def test_minted_id_is_reused(monkeypatch, tmp_path):
    """Within the freshness window, sequential calls share the same id."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    first = session.get_session_id()
    second = session.get_session_id()
    assert first == second


def test_empty_env_falls_back_to_mint(monkeypatch, tmp_path):
    """Whitespace-only env var is treated as absent."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.setenv(session.ENV_SESSION_ID, "   ")
    sid = session.get_session_id()
    assert sid and sid.strip() == sid
    data = json.loads((tmp_path / ".beacon" / "session.json").read_text(encoding="utf-8"))
    assert data["source"] == "minted"


def test_env_id_swap_replaces_marker(monkeypatch, tmp_path):
    """If the env id changes between calls, we adopt the new one and
    overwrite the marker — the env id is authoritative when present."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.setenv(session.ENV_SESSION_ID, "first-id")
    assert session.get_session_id() == "first-id"
    monkeypatch.setenv(session.ENV_SESSION_ID, "second-id")
    assert session.get_session_id() == "second-id"
    data = json.loads((tmp_path / ".beacon" / "session.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "second-id"


def test_env_id_persists_through_heartbeat(monkeypatch, tmp_path):
    """An env-sourced id survives update_last_active calls without remint."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    monkeypatch.setenv(session.ENV_SESSION_ID, "stable-env-id")
    first = session.update_last_active()
    second = session.update_last_active()
    assert first["session_id"] == "stable-env-id"
    assert second["session_id"] == "stable-env-id"
    assert second["source"] == "claude_code_env"
