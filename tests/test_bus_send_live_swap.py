"""Unit tests for `bus send` stale-sid auto-swap (ms-95 / e-2280).

Background — 2026-06-22 kyozai dolphin Trek dogfood: the executor's
session_id churned frequently (= bclaude restarts during the work
session) and the leader's DMs landed on stale sids twice in 30 minutes.
The e-1402 soft-warn gate caught it (= warning visible in stderr) but
the AI was still responsible for manually re-resolving via
``bus directory --live`` before each retry. That re-resolve duty per
send is design debt: the CLI has the directory data already, it can
swap automatically when the choice is unambiguous.

These tests pin the auto-swap behaviour added on top of the existing
e-1402 soft-warn contract:

  * stale recipient + exactly one same-user healthy sid → auto-swap
    (loud stderr notice, returned sid is the new one)
  * stale recipient + multiple same-user healthy sids → no swap,
    keep the e-1402 soft-warn (refuse to guess)
  * stale recipient + zero same-user healthy sids → no swap, keep
    the e-1402 soft-warn (no candidate)
  * stale recipient + identity unknown (= owner not in broader
    directory) → no swap, keep the e-1402 soft-warn
  * BEACON_BUS_NO_AUTO_SWAP=1 → swap skipped even when a unique
    candidate exists; e-1402 soft-warn still fires
  * BEACON_BUS_REFUSE_STALE=1 + stale + no candidate → exit 1
  * BEACON_BUS_REFUSE_STALE=1 + stale + swap candidate → swap wins
    (refuse only applies when there is no recovery path)

All tests stub ``dm_discover.discover_and_aggregate`` so no real
bridge directory is required.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


STALE_SID = "cfgw5d79ll-1781049495026-6f24fc57"
NEW_SID = "cfgw5d79ll-1781054714814-869d1ee0"
OTHER_SID = "cfgw5d79ll-1781057000000-aaaaaaaa"

USER_A = "user-A"
USER_B = "user-B"
EMAIL_A = "alice@example.com"
EMAIL_B = "bob@example.com"


def _stub_discover(healthy_rows, broader_rows=None):
    """Return a fake dm_discover module.

    ``discover_and_aggregate(healthy=True, ...)``  → healthy_rows
    ``discover_and_aggregate(healthy=False, ...)`` → broader_rows
       (defaults to healthy_rows if not provided)
    """
    if broader_rows is None:
        broader_rows = healthy_rows

    class _M:
        @staticmethod
        def discover_and_aggregate(**kw):
            if kw.get("healthy", True):
                return list(healthy_rows)
            return list(broader_rows)

    return _M


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Each test starts with no opt-out / opt-in flags."""
    monkeypatch.delenv("BEACON_BUS_NO_LIVE_CHECK", raising=False)
    monkeypatch.delenv("BEACON_BUS_NO_AUTO_SWAP", raising=False)
    monkeypatch.delenv("BEACON_BUS_REFUSE_STALE", raising=False)


# ---------------------------------------------------------------------------
# Auto-swap happy path (= the e-2280 AC #1 / #3 pin)
# ---------------------------------------------------------------------------

def test_stale_with_unique_same_user_swaps(capsys):
    """Stale sid + exactly one same-user healthy sid → swap to the new sid.

    This is the kyozai dolphin scenario: executor restarted bclaude,
    leader's cached sid is stale, but the new live sid is the same user
    in the directory. Auto-swap removes the re-resolve burden from the
    AI side.
    """
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == NEW_SID
    assert notice is not None
    err = capsys.readouterr().err
    assert "auto-swapped" in err
    assert NEW_SID[:24] in err
    # The legacy soft-warn must NOT also fire when swap succeeded.
    assert "is not in the live+healthy directory" not in err


def test_swap_match_via_email_only(capsys):
    """Identity match works on email alone when user_id stamp is missing.

    Legacy bridges that pre-date the ms-70 user_id stamp may have empty
    ``user_id`` but still carry ``actor.email`` from the e-1349 stamp.
    The swap must still recognise same-user via email so we don't have
    to wait for every old bridge to roll forward.
    """
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": "",
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": "",
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": "",
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == NEW_SID
    assert notice is not None
    capsys.readouterr()  # drain


# ---------------------------------------------------------------------------
# Conservative refusal (= we never silently guess between options)
# ---------------------------------------------------------------------------

def test_stale_with_multiple_same_user_no_swap_soft_warn(capsys):
    """Same user has 2+ live healthy sids → refuse to swap (= ambiguous).

    Cross-machine or cross-worktree parallel bclaude is legitimate;
    without context the CLI can't pick the right target, so we fall
    back to the e-1402 soft-warn and let the sender re-pick.
    """
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": OTHER_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "win-1"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": OTHER_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "win-1"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == STALE_SID
    assert notice is None
    err = capsys.readouterr().err
    assert "auto-swapped" not in err
    assert "is not in the live+healthy directory" in err


def test_stale_with_zero_same_user_no_swap_soft_warn(capsys):
    """No live healthy sid for that user → soft-warn, no swap."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_B,
                "actor": {"email": EMAIL_B, "machine": "mac-2"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": USER_B,
                "actor": {"email": EMAIL_B, "machine": "mac-2"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == STALE_SID
    assert notice is None
    err = capsys.readouterr().err
    assert "is not in the live+healthy directory" in err


def test_stale_with_unknown_owner_no_swap_soft_warn(capsys):
    """Stale sid not present in broader directory either → owner unknown,
    refuse to swap (= we don't know who to match against)."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
        broader_rows=[
            # STALE_SID is absent — bridge tore down its session doc, or
            # the sid was synthetic.
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == STALE_SID
    assert notice is None
    err = capsys.readouterr().err
    assert "is not in the live+healthy directory" in err


# ---------------------------------------------------------------------------
# Opt-out / opt-in env vars
# ---------------------------------------------------------------------------

def test_no_auto_swap_env_disables_swap_keeps_softwarn(monkeypatch, capsys):
    """BEACON_BUS_NO_AUTO_SWAP=1 keeps the live-check warning but
    suppresses the swap, for scripts that want explicit visibility."""
    monkeypatch.setenv("BEACON_BUS_NO_AUTO_SWAP", "1")
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
    )
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == STALE_SID
    assert notice is None
    err = capsys.readouterr().err
    assert "auto-swapped" not in err
    assert "is not in the live+healthy directory" in err


def test_refuse_stale_env_no_candidate_exits(monkeypatch, capsys):
    """BEACON_BUS_REFUSE_STALE=1 hard-errors when stale AND no swap
    candidate (= explicit opt-in strictness for CI)."""
    monkeypatch.setenv("BEACON_BUS_REFUSE_STALE", "1")
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[],
        broader_rows=[],
    )
    with pytest.raises(SystemExit) as excinfo:
        commands._resolve_recipient_live(STALE_SID, "dm")
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "BEACON_BUS_REFUSE_STALE" in err


def test_refuse_stale_env_with_swap_candidate_swaps(monkeypatch, capsys):
    """REFUSE_STALE only fires when there's nothing to swap to. When a
    unique recovery target exists the swap path wins (= refuse is for
    truly dead, not for recoverable churn)."""
    monkeypatch.setenv("BEACON_BUS_REFUSE_STALE", "1")
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover(
        healthy_rows=[
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
        broader_rows=[
            {
                "session_id": STALE_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
            {
                "session_id": NEW_SID,
                "user_id": USER_A,
                "actor": {"email": EMAIL_A, "machine": "mac-1"},
            },
        ],
    )
    # Should NOT raise SystemExit because swap recovered the send.
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == NEW_SID
    assert notice is not None
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Back-compat: the e-1402 contract still holds (skip paths, defensive)
# ---------------------------------------------------------------------------

def test_live_sid_returns_unchanged(capsys):
    """Healthy recipient just passes through, no swap, no warning."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([
        {
            "session_id": NEW_SID,
            "user_id": USER_A,
            "actor": {"email": EMAIL_A},
        },
    ])
    new_recipient, notice, email = commands._resolve_recipient_live(NEW_SID, "dm")
    assert new_recipient == NEW_SID
    assert notice is None
    # e-3566: the live match surfaces the recipient's actor.email so the qual
    # gate can recognise a same-user cross-project DM.
    assert email == EMAIL_A
    assert capsys.readouterr().err == ""


def test_non_dm_channel_returns_unchanged(capsys):
    new_recipient, notice, _email = commands._resolve_recipient_live(
        STALE_SID, "operation-trigger"
    )
    assert new_recipient == STALE_SID
    assert notice is None
    assert capsys.readouterr().err == ""


def test_empty_recipient_returns_unchanged(capsys):
    new_recipient, notice, _email = commands._resolve_recipient_live("", "dm")
    assert new_recipient == ""
    assert notice is None
    assert capsys.readouterr().err == ""


def test_no_live_check_env_returns_unchanged(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_BUS_NO_LIVE_CHECK", "1")
    new_recipient, notice, _email = commands._resolve_recipient_live(STALE_SID, "dm")
    assert new_recipient == STALE_SID
    assert notice is None
    assert capsys.readouterr().err == ""


def test_back_compat_shim_void_return(capsys):
    """The legacy ``_check_recipient_live_health`` shim must remain
    callable as a void function (no return value consumed) so existing
    callers / external tests keep working through the e-2280 rollout."""
    sys.modules["beacon_cli.skills_helpers.dm_discover"] = _stub_discover([])
    result = commands._check_recipient_live_health(STALE_SID, "dm")
    assert result is None
    capsys.readouterr()  # drain stderr soft-warn
