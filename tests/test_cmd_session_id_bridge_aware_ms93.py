"""`beacon session id` must be bridge-aware (ms-93 recipient-stability).

Before this fix ``cmd_session_id`` called ``session.get_session_id()`` directly,
so in a git worktree with no local bridge it printed the *orphan* cwd session
id — diverging from the sid the running bridge actually receives on, and from
the bus sender (which already uses the bridge-aware ``resolve_active_session_id``
via ``_resolve_session_id``). That divergence is what made ``beacon session id``
report a stale identity during the profile-extractor worktree diagnosis.

Now it prefers ``resolve_active_session_id`` and only falls through to
``get_session_id`` (which mints) when no bridge claim exists.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import session  # noqa: E402


def test_session_id_prefers_bridge_claim(monkeypatch, capsys):
    """When a bridge claim resolves (e.g. reconnected across a worktree), print
    that sid — NOT the orphan cwd session id."""
    monkeypatch.setattr(session, "resolve_active_session_id",
                        lambda: "sv-BRIDGE")
    monkeypatch.setattr(session, "get_session_id",
                        lambda: "sv-ORPHAN-CWD")  # must NOT be printed
    commands.cmd_session_id()
    assert capsys.readouterr().out.strip() == "sv-BRIDGE"


def test_session_id_falls_through_to_mint_when_no_claim(monkeypatch, capsys):
    """No bridge claim (resolve returns "") — fall through to get_session_id so
    the historical mint-on-first-call behavior is preserved (bus.mjs cold-start
    still gets an id in a fresh cwd)."""
    monkeypatch.setattr(session, "resolve_active_session_id", lambda: "")
    monkeypatch.setattr(session, "get_session_id", lambda: "sv-MINTED")
    commands.cmd_session_id()
    assert capsys.readouterr().out.strip() == "sv-MINTED"
