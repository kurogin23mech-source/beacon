"""The Codex inbox hook must show the FULL sender session_id (ms-93 / e-3201).

2026-07-10 Codex dogfood: replies to a DM failed with unknown-recipient.
Root cause: ``_format_entry`` rendered ``from={sender[:32]}`` but session_ids
are 34 chars (e.g. ``sv-77e81553-1783403082006-4649aa42``), so the last 2
chars were silently dropped. The receiving AI replies to the displayed value,
and a truncated id is rejected by the recipient gate. The id IS the reply
target, so it must be exact.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "codex-inbox-hook.py"
_spec = importlib.util.spec_from_file_location("codex_inbox_hook", _HOOK_PATH)
codex_inbox_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codex_inbox_hook)


# A real-shape 34-char session id — the exact length that broke under [:32].
FULL_SID = "sv-77e81553-1783403082006-4649aa42"


def _entry(sid: str) -> dict:
    return {
        "event": {
            "event_id": "1783657226898-LuH5qO",
            "channel": "dm",
            "sender_session_id": sid,
            "created_at": "2026-07-10T04:20:26Z",
            "payload": {"text": "hello"},
        }
    }


def test_format_entry_shows_full_sender_not_truncated():
    out = codex_inbox_hook._format_entry(_entry(FULL_SID))
    assert FULL_SID in out, (
        f"sender id must appear in full (was truncated?). rendered:\n{out}"
    )
    # The specific regression: the trailing chars beyond 32 must survive.
    assert len(FULL_SID) > 32
    assert FULL_SID[32:] and FULL_SID[32:] in out


def test_format_entry_emits_structured_reply_target():
    """A machine-readable reply-to line makes the reply target unambiguous."""
    out = codex_inbox_hook._format_entry(_entry(FULL_SID))
    assert f"reply-to: {FULL_SID}" in out


def test_format_entry_reply_target_matches_from():
    """from= and reply-to: must be the same exact id (no drift between them)."""
    out = codex_inbox_hook._format_entry(_entry(FULL_SID))
    assert f"from={FULL_SID}" in out
    assert f"reply-to: {FULL_SID}" in out
