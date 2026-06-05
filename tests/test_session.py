"""Tests for lib/session.get_session_id (ms-57 e-1035).

Pins the session-identity contract:
  - CLAUDE_CODE_SESSION_ID wins when set (harness-provided, stable).
  - Otherwise a per-project marker is minted once and reused.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402


def test_env_session_id_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENV_SESSION_ID, "abc-123-uuid")
    assert session.get_session_id(str(tmp_path)) == "abc-123-uuid"
    # No marker file should be written when the env id is used.
    assert not (tmp_path / ".beacon" / "session.json").exists()


def test_env_session_id_stripped(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENV_SESSION_ID, "  spaced-id  ")
    assert session.get_session_id(str(tmp_path)) == "spaced-id"


def test_minted_when_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    sid = session.get_session_id(str(tmp_path))
    assert sid  # non-empty
    marker = tmp_path / ".beacon" / "session.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["id"] == sid


def test_minted_id_is_reused(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    first = session.get_session_id(str(tmp_path))
    second = session.get_session_id(str(tmp_path))
    assert first == second  # same marker reused across calls


def test_empty_env_falls_back_to_mint(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENV_SESSION_ID, "   ")  # whitespace only
    sid = session.get_session_id(str(tmp_path))
    assert sid and sid.strip() == sid
    assert (tmp_path / ".beacon" / "session.json").exists()
