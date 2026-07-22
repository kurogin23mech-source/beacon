"""Same-user cross-project DM must not be held in armed mode (ms-110 / e-3880).

Background — the non-deterministic hold bug:

  The armed qual gate (``lib/dm_qualgate.py``) already exempts a same-user
  cross-project reply from the 外部宛 hold (e-3566): the server dm_gate permits
  a user DMing their own other project, so the client must not over-block it.
  The exemption fires when ``classify_outbound_reply`` sees the sender's and
  recipient's identity (email) match.

  But the recipient email was resolved *only* from the live directory row's
  ``actor.email`` (``_resolve_recipient_live``). That field is stamped
  separately (``stamp_session_actor_email``), so a heartbeat-only session can be
  live yet carry a blank email. When that happened the recipient identity came
  back ``""`` → ``_same_user`` false → the same-user reply was misclassified as
  external → held. The same operation would pass while not-armed (gate skipped)
  but jam once budget-granted (armed) — the observed non-determinism.

  The structural fix (e-3880): resolve the recipient sid → user via
  ``GET /api/me/sessions`` (``list_user_sessions``), which by construction only
  returns the CALLER's own sessions across all their projects. If the recipient
  sid is in that set, the recipient is the same user — definitively — even when
  ``actor.email`` was never stamped. ``_resolve_recipient_email_via_self_sessions``
  implements that fallback; it feeds a non-blank identity to the gate so the
  same-user exemption fires.

These tests pin:
  * the gate itself: same-user (email resolved) / same-user (email absent but
    identity known) / cross-user / blank-recipient (conservative external);
  * the sid→user fallback: found-with-email / found-without-email (→ caller
    email) / not-found (→ blank, stays external);
  * ``_resolve_recipient_live`` fills a blank live-row email via the fallback;
  * a live row that DOES carry an email is untouched (no extra call).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import dm_qualgate  # noqa: E402


RECIP_SID = "cfgw5d79ll-1781054714814-869d1ee0"
OTHER_SID = "cfgw5d79ll-1781057000000-aaaaaaaa"
SENDER_EMAIL = "kurogin23mech@gmail.com"
OTHER_EMAIL = "someone@else.com"


# ---------------------------------------------------------------------------
# The gate: same-user cross-project must be normal_chat (not held) in armed
# mode, whether the recipient identity was resolved from a stamped actor.email
# or from the sid→user fallback. Different-user must still hold.
# ---------------------------------------------------------------------------

def _gate(sender_email, recipient_email):
    cat = dm_qualgate.classify_outbound_reply(
        channel="dm",
        sender_project_id="PE",
        recipient_project_id="LPS",  # cross-project on purpose
        actions_authorized=None,
        confidential=False,
        sender_user_id=sender_email,
        recipient_user_id=recipient_email,
    )
    return cat, dm_qualgate.evaluate_outbound_qual_gate(cat, armed=True)


def test_same_user_cross_project_email_resolved_flows():
    """AC1: same user (identity resolved on both sides) → normal_chat, not held."""
    cat, gate = _gate(SENDER_EMAIL, SENDER_EMAIL)
    assert cat == dm_qualgate.CAT_NORMAL
    assert gate["hold"] is False


def test_same_user_cross_project_email_via_fallback_flows():
    """AC2: the recipient email that came from the sid→user fallback is the
    caller's own email, so identity matches and the reply flows. (This is the
    exact value _resolve_recipient_email_via_self_sessions returns when the
    live row lacked a stamped actor.email.)"""
    resolved = commands._resolve_creator_identity  # sanity: helper exists
    assert callable(resolved)
    cat, gate = _gate(SENDER_EMAIL, SENDER_EMAIL)
    assert cat == dm_qualgate.CAT_NORMAL
    assert gate["hold"] is False


def test_different_user_cross_project_still_holds():
    """AC3: a genuinely external recipient (different user) still HOLDS in
    armed mode — the fix must not over-relax."""
    cat, gate = _gate(SENDER_EMAIL, OTHER_EMAIL)
    assert cat == dm_qualgate.CAT_EXTERNAL
    assert gate["hold"] is True


def test_blank_recipient_identity_stays_external():
    """AC3 corollary: when the recipient identity is unknown (blank), the gate
    keeps the conservative external default (does NOT flow)."""
    cat, gate = _gate(SENDER_EMAIL, "")
    assert cat == dm_qualgate.CAT_EXTERNAL
    assert gate["hold"] is True


# ---------------------------------------------------------------------------
# The sid→user fallback: _resolve_recipient_email_via_self_sessions.
# Stub _get_api_client so no real server / auth is needed.
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_user_sessions(self, **kw):
        return list(self._sessions)


def _stub_api(monkeypatch, sessions):
    monkeypatch.setattr(
        commands, "_get_api_client", lambda: (_FakeClient(sessions), {})
    )


def _stub_identity(monkeypatch, email):
    monkeypatch.setattr(
        commands, "_resolve_creator_identity",
        lambda: ("user-self", email, "sender-sid"),
    )


def test_fallback_found_without_email_returns_caller_email(monkeypatch):
    """AC2 core: recipient sid is one of the caller's own sessions but its
    session doc carries no stamped actor.email → return the caller's own login
    email (same user by construction, since /api/me/sessions is caller-scoped)."""
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-self", "actor": {}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)
    email = commands._resolve_recipient_email_via_self_sessions(RECIP_SID)
    assert email == SENDER_EMAIL


def test_fallback_found_with_email_prefers_row_email(monkeypatch):
    """When the caller's own recipient session DOES carry a stamped email,
    prefer it (it is authoritative and identical to the caller anyway)."""
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-self",
         "actor": {"email": SENDER_EMAIL, "machine": "mac-1"}},
    ])
    _stub_identity(monkeypatch, "should-not-be-used@example.com")
    email = commands._resolve_recipient_email_via_self_sessions(RECIP_SID)
    assert email == SENDER_EMAIL


def test_fallback_comember_without_email_stays_external(monkeypatch):
    """e-3880 review fix (over-relaxation): /api/me/sessions returns EVERY session
    in the caller's projects — INCLUDING a co-member's session in a shared project.
    A co-member (different owner user_id) whose session has NO stamped email must
    NOT be treated as same-user: return "" (external), never the caller's email.
    Without this guard a cross-user DM would bypass the armed 外部宛 hold."""
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-OTHER", "actor": {}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)  # caller uid = "user-self"
    email = commands._resolve_recipient_email_via_self_sessions(RECIP_SID)
    assert email == ""
    assert email != SENDER_EMAIL


def test_fallback_comember_with_email_returns_their_email(monkeypatch):
    """A co-member WITH a stamped email → return THEIR email (→ _same_user False
    → 外部宛), never the caller's. Distinguishes users correctly."""
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-OTHER",
         "actor": {"email": "other@example.com"}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)
    email = commands._resolve_recipient_email_via_self_sessions(RECIP_SID)
    assert email == "other@example.com"
    assert email != SENDER_EMAIL


def test_fallback_not_found_returns_blank(monkeypatch):
    """AC3: recipient sid is NOT one of the caller's own sessions → blank, so
    the gate keeps the conservative external default (no over-relax to a
    genuinely external recipient)."""
    _stub_api(monkeypatch, [
        {"session_id": OTHER_SID, "user_id": "user-self",
         "actor": {"email": SENDER_EMAIL}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)
    email = commands._resolve_recipient_email_via_self_sessions(RECIP_SID)
    assert email == ""


def test_fallback_api_failure_returns_blank(monkeypatch):
    """A server / auth failure degrades to blank (never raises, never a
    footgun that blocks the send)."""
    def _boom():
        raise RuntimeError("no auth")
    monkeypatch.setattr(commands, "_get_api_client", _boom)
    assert commands._resolve_recipient_email_via_self_sessions(RECIP_SID) == ""


def test_fallback_empty_recipient_returns_blank(monkeypatch):
    # Should short-circuit before touching the client.
    called = {"n": 0}

    def _client():
        called["n"] += 1
        return (_FakeClient([]), {})

    monkeypatch.setattr(commands, "_get_api_client", _client)
    assert commands._resolve_recipient_email_via_self_sessions("") == ""
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# _resolve_recipient_live wires the fallback in for a live row that lacks a
# stamped actor.email — the end-to-end path the bug hit.
# ---------------------------------------------------------------------------

def _stub_discover(healthy_rows):
    class _M:
        @staticmethod
        def discover_and_aggregate(**kw):
            return list(healthy_rows)
    return _M


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_NO_LIVE_CHECK", raising=False)
    monkeypatch.delenv("BEACON_BUS_NO_AUTO_SWAP", raising=False)
    monkeypatch.delenv("BEACON_BUS_REFUSE_STALE", raising=False)


def test_live_row_without_email_backfills_via_fallback(monkeypatch, capsys):
    """AC2 end-to-end: a live+healthy recipient whose row lacks actor.email
    still yields a non-blank recipient identity via the sid→user fallback."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([
        {"session_id": RECIP_SID, "user_id": "user-self", "actor": {"machine": "mac-1"}},
    ])
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-self", "actor": {}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)
    new_recipient, notice, email = commands._resolve_recipient_live(RECIP_SID, "dm")
    assert new_recipient == RECIP_SID
    assert notice is None
    assert email == SENDER_EMAIL  # backfilled, not ""
    capsys.readouterr()


def test_helper_import_failure_still_resolves_identity(monkeypatch):
    """e-3880 live end-to-end fix: when the dm_discover helper cannot be imported
    (beacon_cli not on the `beacon bus send` subprocess's sys.path), the liveness
    check bypasses — but _resolve_recipient_live MUST still resolve the recipient
    identity via the sid→user fallback (which uses the api client, not the helper)
    so a same-user reply is recognised and not held in armed mode. This is the
    failure the isolated unit tests missed (they had beacon_cli importable)."""
    import importlib
    real_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "beacon_cli.skills_helpers.dm_discover":
            raise ImportError("simulated: beacon_cli not on subprocess sys.path")
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.delenv("BEACON_BUS_NO_LIVE_CHECK", raising=False)
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-self", "actor": {}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)  # caller uid = user-self
    _new, notice, email = commands._resolve_recipient_live(RECIP_SID, "dm")
    # Resolved via fallback despite the helper import failing — NOT "".
    assert email == SENDER_EMAIL


def test_no_live_check_env_still_resolves_identity(monkeypatch):
    """BEACON_BUS_NO_LIVE_CHECK=1 opts out of the liveness check but must still
    resolve identity (independent concern) so the qual gate keeps working."""
    monkeypatch.setenv("BEACON_BUS_NO_LIVE_CHECK", "1")
    _stub_api(monkeypatch, [
        {"session_id": RECIP_SID, "user_id": "user-self", "actor": {}},
    ])
    _stub_identity(monkeypatch, SENDER_EMAIL)
    _new, notice, email = commands._resolve_recipient_live(RECIP_SID, "dm")
    assert email == SENDER_EMAIL


def test_live_row_with_email_does_not_call_fallback(monkeypatch, capsys):
    """A live row that already carries actor.email returns it directly and does
    NOT pay the extra sid→user round-trip (no regression to the fast path)."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([
        {"session_id": RECIP_SID, "user_id": "user-self",
         "actor": {"email": SENDER_EMAIL}},
    ])

    def _should_not_be_called():
        raise AssertionError("fallback must not run when row has an email")

    monkeypatch.setattr(commands, "_get_api_client", _should_not_be_called)
    new_recipient, notice, email = commands._resolve_recipient_live(RECIP_SID, "dm")
    assert new_recipient == RECIP_SID
    assert email == SENDER_EMAIL
    capsys.readouterr()


# ---------------------------------------------------------------------------
# AC5: the BEACON_QUALGATE_OFF escape hatch is untouched. The gate module has
# no knowledge of that env var (it lives in the cmd_bus_send guard), so the
# classification/evaluation stay pure regardless of the env — pin that the gate
# never reads the env so the opt-out remains a caller-side concern.
# ---------------------------------------------------------------------------

def test_qualgate_off_env_does_not_change_gate(monkeypatch):
    monkeypatch.setenv("BEACON_QUALGATE_OFF", "1")
    # Same-user still classifies normal, cross-user still external — the gate
    # itself is env-agnostic; the opt-out is applied by the caller guard.
    cat_same, _ = _gate(SENDER_EMAIL, SENDER_EMAIL)
    cat_diff, _ = _gate(SENDER_EMAIL, OTHER_EMAIL)
    assert cat_same == dm_qualgate.CAT_NORMAL
    assert cat_diff == dm_qualgate.CAT_EXTERNAL
