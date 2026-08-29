"""bus directory human picker surfaces session intent (ms-160 e-5809 / e-5813).

attention_required (= 「人間の判断待ち」旗) and the AI-authored focus intent are
stored in each session row's ``intent`` block and already returned by the
directory endpoints, but the human picker printed only sid/health. These pin
that the picker now shows both (pure client-side display).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import cmd_bus  # noqa: E402


class _FakeClient:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_user_sessions(self, **kwargs):
        return self._sessions


@pytest.fixture
def _clean_env(monkeypatch):
    # default (cross-project) path, no filters
    for k in ("BEACON_DIR_CWD_ONLY", "BEACON_BUS_PROJECT_ID", "BEACON_DIR_USER",
              "BEACON_DIR_MACHINE", "BEACON_DIR_AGENT", "BEACON_DIR_LIVE",
              "BEACON_DIR_HEALTHY", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def _run(monkeypatch, sessions, capsys) -> str:
    monkeypatch.setattr(cmd_bus, "_get_api_client", lambda: (_FakeClient(sessions), {}))
    cmd_bus.cmd_bus_directory()
    return capsys.readouterr().out


def test_attention_required_flag_and_focus_shown(_clean_env, monkeypatch, capsys):
    out = _run(monkeypatch, [{
        "session_id": "sv-1", "actor": {"machine": "M1"},
        "last_active": "2026-08-30T01:00:00Z", "project_name": "P",
        "intent": {"text": "ms-160 の pause/resume を実装中", "attention_required": True},
    }], capsys)
    assert "⚠ATTN" in out
    assert 'focus="ms-160 の pause/resume を実装中"' in out


def test_no_intent_prints_plain_row(_clean_env, monkeypatch, capsys):
    out = _run(monkeypatch, [{
        "session_id": "sv-2", "actor": {"machine": "M2"},
        "last_active": "2026-08-30T01:00:00Z", "project_name": "P",
    }], capsys)
    assert "sv-2" in out
    assert "⚠ATTN" not in out
    assert "focus=" not in out


def test_focus_without_attention_shows_focus_only(_clean_env, monkeypatch, capsys):
    out = _run(monkeypatch, [{
        "session_id": "sv-3", "actor": {"machine": "M3"},
        "last_active": "2026-08-30T01:00:00Z", "project_name": "P",
        "intent": {"text": "调查中", "attention_required": False},
    }], capsys)
    assert "focus=" in out
    assert "⚠ATTN" not in out
