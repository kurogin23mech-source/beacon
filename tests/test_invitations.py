"""Unit tests for lib/invitations.py (ms-78 e-1803/e-1804).

Pure-data layer only — no I/O, no apply_operation. Verifies the
data shape, token round-trip, expiry handling, and the consume-once
contract that protects /join/<token> from replay.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import invitations as inv_mod  # noqa: E402


def _empty_project():
    return {"name": "t", "milestones": [], "members": []}


# ---------------------------------------------------------------------------
# Token primitives
# ---------------------------------------------------------------------------

def test_generate_token_returns_unique_urlsafe_strings():
    a = inv_mod.generate_token()
    b = inv_mod.generate_token()
    assert a != b
    assert len(a) >= 32
    # URL-safe base64 alphabet
    for ch in a:
        assert ch.isalnum() or ch in "-_"


def test_hash_token_is_deterministic_and_one_way():
    t = "some-token"
    assert inv_mod.hash_token(t) == inv_mod.hash_token(t)
    assert inv_mod.hash_token(t) != t
    assert len(inv_mod.hash_token(t)) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# invitation_create
# ---------------------------------------------------------------------------

def test_create_adds_invitation_and_returns_plaintext():
    data = _empty_project()
    invitation, token = inv_mod.invitation_create(
        data,
        email="alice@example.com",
        role="viewer",
        invited_by_user_id="uid-owner",
        invited_by_email="owner@example.com",
    )
    assert invitation["email"] == "alice@example.com"
    assert invitation["role"] == "viewer"
    assert invitation["invited_by"] == "uid-owner"
    assert invitation["invited_by_email"] == "owner@example.com"
    assert "id" in invitation
    assert "token_hash" in invitation
    assert "created_at" in invitation
    assert "expires_at" in invitation
    # plaintext is returned, not stored
    assert token
    assert "token" not in invitation
    assert invitation["token_hash"] == inv_mod.hash_token(token)
    assert data["invitations"] == [invitation]


def test_create_rejects_invalid_role():
    data = _empty_project()
    with pytest.raises(ValueError) as exc:
        inv_mod.invitation_create(
            data, email="a@x.com", role="superadmin",
            invited_by_user_id="u",
        )
    assert "Invalid invite role" in str(exc.value)


def test_create_rejects_invalid_email():
    data = _empty_project()
    with pytest.raises(ValueError):
        inv_mod.invitation_create(
            data, email="not-an-email", role="viewer",
            invited_by_user_id="u",
        )
    with pytest.raises(ValueError):
        inv_mod.invitation_create(
            data, email="", role="viewer", invited_by_user_id="u",
        )


def test_create_normalizes_email_lowercase():
    data = _empty_project()
    inv, _ = inv_mod.invitation_create(
        data, email="ALICE@Example.COM", role="viewer",
        invited_by_user_id="u",
    )
    assert inv["email"] == "alice@example.com"


def test_create_rejects_duplicate_live_invitation():
    data = _empty_project()
    inv_mod.invitation_create(
        data, email="alice@example.com", role="viewer",
        invited_by_user_id="u",
    )
    with pytest.raises(ValueError) as exc:
        inv_mod.invitation_create(
            data, email="alice@example.com", role="editor",
            invited_by_user_id="u",
        )
    assert "already exists" in str(exc.value)


def test_create_rejects_email_already_a_member():
    data = _empty_project()
    data["members"] = [{"user_id": "uid-bob", "email": "bob@example.com", "role": "viewer"}]
    with pytest.raises(ValueError) as exc:
        inv_mod.invitation_create(
            data, email="bob@example.com", role="editor",
            invited_by_user_id="u",
        )
    assert "already a project member" in str(exc.value)


# ---------------------------------------------------------------------------
# invitation_cancel
# ---------------------------------------------------------------------------

def test_cancel_removes_and_returns_invitation():
    data = _empty_project()
    inv, _ = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    removed = inv_mod.invitation_cancel(data, inv["id"])
    assert removed["id"] == inv["id"]
    assert data["invitations"] == []


def test_cancel_unknown_id_raises():
    data = _empty_project()
    with pytest.raises(ValueError):
        inv_mod.invitation_cancel(data, "no-such-id")


# ---------------------------------------------------------------------------
# invitation_consume
# ---------------------------------------------------------------------------

def test_consume_removes_invitation_and_returns_role():
    data = _empty_project()
    inv, token = inv_mod.invitation_create(
        data, email="a@x.com", role="editor", invited_by_user_id="u",
    )
    consumed = inv_mod.invitation_consume(data, token)
    assert consumed["email"] == "a@x.com"
    assert consumed["role"] == "editor"
    assert data["invitations"] == []


def test_consume_twice_is_rejected_replay_protection():
    data = _empty_project()
    _, token = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    inv_mod.invitation_consume(data, token)
    with pytest.raises(ValueError) as exc:
        inv_mod.invitation_consume(data, token)
    assert "not found" in str(exc.value)


def test_consume_unknown_token_rejected():
    data = _empty_project()
    with pytest.raises(ValueError):
        inv_mod.invitation_consume(data, "totally-bogus-token")


def test_consume_expired_invitation_rejected_and_removed():
    data = _empty_project()
    _, token = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer",
        invited_by_user_id="u",
        expiry_days=1,
    )
    # Force-expire by rewriting expires_at to 1 day in the past
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data["invitations"][0]["expires_at"] = past
    with pytest.raises(ValueError) as exc:
        inv_mod.invitation_consume(data, token)
    assert "expired" in str(exc.value)
    # expired row is swept on consume attempt
    assert data["invitations"] == []


# ---------------------------------------------------------------------------
# invitation_find_by_token
# ---------------------------------------------------------------------------

def test_find_by_token_returns_invitation():
    data = _empty_project()
    inv, token = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    found = inv_mod.invitation_find_by_token(data, token)
    assert found is not None
    assert found["id"] == inv["id"]
    # find does NOT consume — invitation still in list
    assert len(data["invitations"]) == 1


def test_find_by_token_returns_none_for_unknown_token():
    data = _empty_project()
    assert inv_mod.invitation_find_by_token(data, "bogus") is None


def test_find_by_token_returns_none_for_expired():
    data = _empty_project()
    _, token = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    data["invitations"][0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    assert inv_mod.invitation_find_by_token(data, token) is None


# ---------------------------------------------------------------------------
# invitations_list / public_view
# ---------------------------------------------------------------------------

def test_invitations_list_handles_corrupt_field():
    data = _empty_project()
    data["invitations"] = "garbage"
    assert inv_mod.invitations_list(data) == []


def test_public_view_strips_sensitive_fields_by_default():
    data = _empty_project()
    inv, _ = inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    view = inv_mod.invitation_public_view(inv)
    assert "token_hash" not in view
    assert view["email"] == "a@x.com"
    assert view["role"] == "viewer"


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def test_sweep_drops_expired_entries():
    data = _empty_project()
    inv_mod.invitation_create(
        data, email="a@x.com", role="viewer", invited_by_user_id="u",
    )
    inv_mod.invitation_create(
        data, email="b@x.com", role="viewer", invited_by_user_id="u",
    )
    # Force-expire the first
    data["invitations"][0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    removed = inv_mod.invitation_sweep_expired(data)
    assert removed == 1
    assert len(data["invitations"]) == 1
    assert data["invitations"][0]["email"] == "b@x.com"
