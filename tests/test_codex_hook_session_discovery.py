"""Tests for ms-93 / e-2502 hook ↔ daemon session discovery.

Codex 2026-06-26 dogfood (= DM ``MbZWuaiLRms2U7XF9Pum``) caught the
hook failing to find the active receive-loop session because the
hook's parent_pid is different from the daemon's parent_pid when the
hook is invoked manually (= different shell) or from a Codex hook
runner that uses its own shell wrapper.

Fix: the daemon publishes a cwd-level pointer file
``<cwd>/.beacon/codex/receive-loop.session.json`` with the active sid +
project_id + beacon_bin; the hook reads this first and falls back to
the original parent_pid scheme only when the pointer is absent.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


# Load the inbox-hook script as a module so we can call its helpers
# without exec()ing the CLI.
_HOOK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "codex-inbox-hook.py"
_spec = importlib.util.spec_from_file_location("codex_inbox_hook", _HOOK_PATH)
hook_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook_mod)


def _write_pointer(cwd: Path, *, sid: str, project_id: str) -> None:
    pointer = cwd / ".beacon" / "codex" / "receive-loop.session.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({
        "session_id": sid,
        "project_id": project_id,
        "beacon_bin": "/fake/beacon",
        "written_at": "2026-06-26T00:00:00Z",
    }))


class TestResolveCodexSession:
    def test_pointer_file_wins(self, tmp_path):
        _write_pointer(tmp_path, sid="codex-from-pointer", project_id="proj-1")
        # Even with no cs_module, pointer path should resolve.
        sid, pid = hook_mod._resolve_codex_session(None, str(tmp_path))
        assert sid == "codex-from-pointer"
        assert pid == "proj-1"

    def test_pointer_with_missing_fields_falls_through(self, tmp_path):
        # Malformed pointer (no session_id) → fall through to fallback.
        pointer = tmp_path / ".beacon" / "codex" / "receive-loop.session.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"project_id": "proj-1"}))  # no sid
        sid, pid = hook_mod._resolve_codex_session(None, str(tmp_path))
        assert (sid, pid) == ("", "")

    def test_no_pointer_no_fallback_returns_empty(self, tmp_path):
        sid, pid = hook_mod._resolve_codex_session(None, str(tmp_path))
        assert (sid, pid) == ("", "")

    def test_pointer_overrides_parent_pid_match(self, tmp_path):
        # Even if the legacy parent_pid scheme would resolve to a
        # different sid, the pointer wins. We simulate by setting up
        # the cs_module fallback path (cloud.json + a session file)
        # AND a pointer with a different sid; pointer must win.
        cloud = tmp_path / ".beacon" / "cloud.json"
        cloud.parent.mkdir(parents=True, exist_ok=True)
        cloud.write_text(json.dumps({"project_id": "proj-1"}))
        _write_pointer(tmp_path, sid="codex-from-pointer", project_id="proj-1")
        # Fake cs_module that would return a different sid via fallback —
        # we should never reach it.
        class _FakeCS:
            class _Rec:
                session_id = "codex-from-fallback"
            @staticmethod
            def derive_stable_instance_key(_cwd, _ppid): return "k"
            @staticmethod
            def codex_session_path(_pid, _key): return Path("/dev/null")
            @staticmethod
            def read_codex_session(_p): return _FakeCS._Rec
        sid, pid = hook_mod._resolve_codex_session(_FakeCS(), str(tmp_path))
        assert sid == "codex-from-pointer"


class TestHookOutputShape:
    def test_hook_output_wraps_additional_context(self):
        payload = hook_mod._hook_output("hello")
        assert payload == {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "hello",
            }
        }


class TestAppServerDispatchHookNoOp:
    """When the hook fires inside the app-server DM-dispatch child (marked by
    ``BEACON_CODEX_APP_SERVER_DISPATCH=1``), it must NOT read or archive the
    shared foreground inbox — otherwise it drains the DM before the human's
    foreground TUI surfaces it (= ms-93 / e-3156 2nd-layer hook-archive race).
    """

    def _seed_inbox(self, cwd: Path, event_id: str) -> Path:
        inbox = cwd / ".beacon" / "codex" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / f"{event_id}.json"
        f.write_text(json.dumps({
            "event_id": event_id,
            "channel": "dm",
            "sender_session_id": "other",
            "payload": {"text": "hi"},
            "created_at": "2026-07-10T00:00:00Z",
        }))
        return f

    def test_marker_no_ops_and_leaves_inbox_intact(self, tmp_path, monkeypatch, capsys):
        f = self._seed_inbox(tmp_path, "evt-race")
        monkeypatch.setenv("BEACON_CODEX_APP_SERVER_DISPATCH", "1")
        monkeypatch.setattr(sys, "argv", ["codex-inbox-hook", "--cwd", str(tmp_path)])
        rc = hook_mod.main()
        assert rc == 0
        # Empty additionalContext (no injection in the worker child).
        assert capsys.readouterr().out.strip() == "{}"
        # The foreground inbox event is NOT consumed / archived.
        assert f.exists()
        assert not (
            tmp_path / ".beacon" / "codex" / "inbox" / ".read" / "evt-race.json"
        ).exists()

    def test_without_marker_the_dm_is_rendered(self, tmp_path, monkeypatch, capsys):
        # The foreground hook (no marker) still surfaces the DM as before.
        self._seed_inbox(tmp_path, "evt-fg")
        monkeypatch.delenv("BEACON_CODEX_APP_SERVER_DISPATCH", raising=False)
        monkeypatch.setattr(sys, "argv", ["codex-inbox-hook", "--cwd", str(tmp_path)])
        rc = hook_mod.main()
        assert rc == 0
        assert "BEACON BUS INBOX" in capsys.readouterr().out
