"""Tests for claim primitives (ms-55 e-1648).

Layers covered:
  - build / parse — schema round-trip + validation
  - latest_claims_by_target — first-publisher-wins, response binding,
    release removal, conflict surface
  - local persistence — atomic write + filter by --mine / --target
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import claims  # noqa: E402


# ---------------------------------------------------------------------------
# build_claim_payload
# ---------------------------------------------------------------------------

def test_build_claim_broadcast_no_recipient():
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-1",
        target_kind="task",
        target_id="e-1646",
        intent="implementing stop CLI",
    )
    assert p["claim_kind"] == "claim"
    assert p["target"] == {"kind": "task", "id": "e-1646"}
    assert p["claim_id"].startswith("cl-")
    assert "to_session_id" not in p
    assert p["intent"] == "implementing stop CLI"


def test_build_request_requires_recipient():
    with pytest.raises(ValueError, match="to_session_id"):
        claims.build_claim_payload(
            claim_kind="request",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
        )


def test_build_handoff_requires_recipient():
    with pytest.raises(ValueError, match="to_session_id"):
        claims.build_claim_payload(
            claim_kind="handoff",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
        )


def test_build_request_with_recipient():
    p = claims.build_claim_payload(
        claim_kind="request",
        from_session_id="sv-A",
        target_kind="task",
        target_id="e-1648",
        to_session_id="sv-B",
        intent="please pick this up after your current commit",
    )
    assert p["to_session_id"] == "sv-B"


def test_build_claim_rejects_recipient():
    """Plain claim is broadcast — letting it carry to_session_id makes
    the wire format ambiguous (is it broadcast or targeted?)."""
    with pytest.raises(ValueError, match="must not set to_session_id"):
        claims.build_claim_payload(
            claim_kind="claim",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
            to_session_id="sv-B",
        )


def test_build_invalid_claim_kind():
    with pytest.raises(ValueError, match="claim_kind"):
        claims.build_claim_payload(
            claim_kind="lock",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
        )


def test_build_invalid_target_kind():
    with pytest.raises(ValueError, match="target_kind"):
        claims.build_claim_payload(
            claim_kind="claim",
            from_session_id="sv-1",
            target_kind="release",
            target_id="v0.1",
        )


def test_build_requires_from_session_id():
    with pytest.raises(ValueError, match="from_session_id"):
        claims.build_claim_payload(
            claim_kind="claim",
            from_session_id="",
            target_kind="ms",
            target_id="ms-55",
        )


def test_build_requires_target_id():
    with pytest.raises(ValueError, match="target_id"):
        claims.build_claim_payload(
            claim_kind="claim",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="",
        )


def test_build_custom_claim_id_preserved():
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-1",
        target_kind="ms",
        target_id="ms-55",
        claim_id="cl-custom-xyz",
    )
    assert p["claim_id"] == "cl-custom-xyz"


def test_build_metadata_must_be_dict():
    with pytest.raises(ValueError, match="metadata"):
        claims.build_claim_payload(
            claim_kind="claim",
            from_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
            metadata="not a dict",  # type: ignore[arg-type]
        )


def test_mint_claim_id_format():
    cid = claims.mint_claim_id()
    assert cid.startswith("cl-")
    parts = cid.split("-")
    assert len(parts) == 3
    assert parts[1].isdigit()
    assert len(parts[2]) == 6  # 3 hex bytes


# ---------------------------------------------------------------------------
# build_response / build_release
# ---------------------------------------------------------------------------

def test_build_response_accept():
    p = claims.build_response_payload(
        claim_id="cl-1",
        decision="accept",
        from_session_id="sv-B",
        reason="I have cycles",
    )
    assert p["kind"] == "claim_response"
    assert p["decision"] == "accept"


def test_build_response_decline():
    p = claims.build_response_payload(
        claim_id="cl-1",
        decision="decline",
        from_session_id="sv-B",
    )
    assert p["decision"] == "decline"


def test_build_response_invalid_decision():
    with pytest.raises(ValueError, match="decision"):
        claims.build_response_payload(
            claim_id="cl-1",
            decision="maybe",
            from_session_id="sv-B",
        )


def test_build_release_completed():
    p = claims.build_release_payload(
        claim_id="cl-1",
        outcome="completed",
        from_session_id="sv-A",
    )
    assert p["kind"] == "claim_release"
    assert p["outcome"] == "completed"


def test_build_release_abandoned():
    p = claims.build_release_payload(
        claim_id="cl-1",
        outcome="abandoned",
        from_session_id="sv-A",
        reason="out of context",
    )
    assert p["outcome"] == "abandoned"


def test_build_release_invalid_outcome():
    with pytest.raises(ValueError, match="outcome"):
        claims.build_release_payload(
            claim_id="cl-1",
            outcome="success",
            from_session_id="sv-A",
        )


# ---------------------------------------------------------------------------
# parse_*  (graceful on bad input)
# ---------------------------------------------------------------------------

def _wrap(payload, *, channel="claim-signal", event_id="e-1",
          created_at="2026-06-15T00:00:00Z"):
    return {
        "event_id": event_id,
        "channel": channel,
        "payload": payload,
        "created_at": created_at,
    }


def test_parse_claim_roundtrip():
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-1",
        target_kind="task",
        target_id="e-1646",
    )
    rec = claims.parse_claim_event(_wrap(p))
    assert rec is not None
    assert rec["claim_kind"] == "claim"
    assert rec["target"] == {"kind": "task", "id": "e-1646"}


def test_parse_claim_wrong_channel_none():
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-1",
        target_kind="ms",
        target_id="ms-55",
    )
    assert claims.parse_claim_event(_wrap(p, channel="dm")) is None


def test_parse_claim_wrong_kind_none():
    p = claims.build_response_payload(
        claim_id="cl-1",
        decision="accept",
        from_session_id="sv-B",
    )
    assert claims.parse_claim_event(_wrap(p)) is None


def test_parse_claim_missing_target_none():
    assert claims.parse_claim_event(_wrap({
        "kind": "claim",
        "claim_kind": "claim",
        "claim_id": "cl-1",
        "from_session_id": "sv-1",
    })) is None


def test_parse_response_roundtrip():
    p = claims.build_response_payload(
        claim_id="cl-1",
        decision="decline",
        from_session_id="sv-B",
    )
    rec = claims.parse_response_event(_wrap(p))
    assert rec is not None
    assert rec["decision"] == "decline"


def test_parse_release_roundtrip():
    p = claims.build_release_payload(
        claim_id="cl-1",
        outcome="completed",
        from_session_id="sv-A",
    )
    rec = claims.parse_release_event(_wrap(p))
    assert rec is not None
    assert rec["outcome"] == "completed"


def test_parse_garbage_safe():
    assert claims.parse_claim_event(None) is None  # type: ignore[arg-type]
    assert claims.parse_claim_event({}) is None
    assert claims.parse_response_event({}) is None
    assert claims.parse_release_event({}) is None


# ---------------------------------------------------------------------------
# latest_claims_by_target
# ---------------------------------------------------------------------------

def _claim_event(*, eid, ts, ckind, tkind, tid, sid, claim_id=None,
                  to_sid=""):
    p = claims.build_claim_payload(
        claim_kind=ckind,
        from_session_id=sid,
        target_kind=tkind,
        target_id=tid,
        to_session_id=to_sid,
        claim_id=claim_id or f"cid-{eid}",
        issued_at=ts,
    )
    return _wrap(p, event_id=eid, created_at=ts)


def _response_event(*, eid, ts, claim_id, decision, sid):
    p = claims.build_response_payload(
        claim_id=claim_id,
        decision=decision,
        from_session_id=sid,
        issued_at=ts,
    )
    return _wrap(p, event_id=eid, created_at=ts)


def _release_event(*, eid, ts, claim_id, outcome, sid):
    p = claims.build_release_payload(
        claim_id=claim_id,
        outcome=outcome,
        from_session_id=sid,
        issued_at=ts,
    )
    return _wrap(p, event_id=eid, created_at=ts)


def test_latest_empty():
    assert claims.latest_claims_by_target([]) == {}


def test_latest_single_claim():
    events = [_claim_event(
        eid="e1", ts="2026-06-15T00:00:00Z",
        ckind="claim", tkind="ms", tid="ms-55", sid="sv-A",
    )]
    state = claims.latest_claims_by_target(events)
    assert ("ms", "ms-55") in state
    assert state[("ms", "ms-55")]["from_session_id"] == "sv-A"


def test_latest_first_publisher_wins():
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="claim", tkind="task", tid="e-1646", sid="sv-A",
        ),
        _claim_event(
            eid="e2", ts="2026-06-15T00:01:00Z",
            ckind="claim", tkind="task", tid="e-1646", sid="sv-B",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    key = ("task", "e-1646")
    assert state[key]["from_session_id"] == "sv-A"  # first wins
    # Conflict surfaced.
    conflicts = state[key].get("_conflicts", [])
    assert len(conflicts) == 1
    assert conflicts[0]["from_session_id"] == "sv-B"


def test_latest_request_not_bound_until_accepted():
    """A pending request does not show up as an active claim — it's
    just a proposal until the recipient responds."""
    events = [_claim_event(
        eid="e1", ts="2026-06-15T00:00:00Z",
        ckind="request", tkind="task", tid="e-1648", sid="sv-A",
        to_sid="sv-B", claim_id="cl-req",
    )]
    state = claims.latest_claims_by_target(events)
    assert state == {}


def test_latest_request_accepted_binds_to_responder():
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="request", tkind="task", tid="e-1648", sid="sv-A",
            to_sid="sv-B", claim_id="cl-req",
        ),
        _response_event(
            eid="e2", ts="2026-06-15T00:00:30Z",
            claim_id="cl-req", decision="accept", sid="sv-B",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    key = ("task", "e-1648")
    assert key in state
    rec = state[key]
    assert rec["from_session_id"] == "sv-B"   # responder is now owner
    assert rec["issued_by_session_id"] == "sv-A"  # issuer preserved
    assert rec["accepted_at"] == "2026-06-15T00:00:30Z"


def test_latest_request_declined_no_binding():
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="request", tkind="task", tid="e-1648", sid="sv-A",
            to_sid="sv-B", claim_id="cl-req",
        ),
        _response_event(
            eid="e2", ts="2026-06-15T00:00:30Z",
            claim_id="cl-req", decision="decline", sid="sv-B",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    assert state == {}


def test_latest_release_removes_claim():
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="claim", tkind="ms", tid="ms-55", sid="sv-A",
            claim_id="cl-1",
        ),
        _release_event(
            eid="e2", ts="2026-06-15T00:05:00Z",
            claim_id="cl-1", outcome="completed", sid="sv-A",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    assert state == {}


def test_latest_release_other_id_doesnt_remove():
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="claim", tkind="ms", tid="ms-55", sid="sv-A",
            claim_id="cl-1",
        ),
        _release_event(
            eid="e2", ts="2026-06-15T00:05:00Z",
            claim_id="cl-different", outcome="completed", sid="sv-A",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    assert ("ms", "ms-55") in state  # unaffected


def test_latest_handoff_accepted_transfers_owner():
    events = [
        # Initial claim by sv-A
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="claim", tkind="task", tid="e-1648", sid="sv-A",
            claim_id="cl-base",
        ),
        # Handoff from sv-A to sv-B
        _claim_event(
            eid="e2", ts="2026-06-15T00:01:00Z",
            ckind="handoff", tkind="task", tid="e-1648", sid="sv-A",
            to_sid="sv-B", claim_id="cl-handoff",
        ),
        _response_event(
            eid="e3", ts="2026-06-15T00:01:30Z",
            claim_id="cl-handoff", decision="accept", sid="sv-B",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    key = ("task", "e-1648")
    rec = state[key]
    # After handoff accept, the conflict surface records 2 owners (the
    # earlier sv-A claim is still in state, and the accepted handoff
    # joined as a conflict). The "first-publisher-wins" rule means
    # sv-A remains canonical; the handoff is recorded but the original
    # owner needs to release before sv-B is canonical.
    assert rec["from_session_id"] == "sv-A"
    # And the handoff acceptance surfaced.
    assert any(
        c["from_session_id"] == "sv-B" for c in rec.get("_conflicts", [])
    )


def test_latest_handoff_with_release_clean_transfer():
    """Realistic handoff flow: release the original claim first, then
    the handoff can bind sv-B without conflict."""
    events = [
        _claim_event(
            eid="e1", ts="2026-06-15T00:00:00Z",
            ckind="claim", tkind="task", tid="e-1648", sid="sv-A",
            claim_id="cl-base",
        ),
        _release_event(
            eid="e2", ts="2026-06-15T00:00:30Z",
            claim_id="cl-base", outcome="abandoned", sid="sv-A",
        ),
        _claim_event(
            eid="e3", ts="2026-06-15T00:01:00Z",
            ckind="handoff", tkind="task", tid="e-1648", sid="sv-A",
            to_sid="sv-B", claim_id="cl-handoff",
        ),
        _response_event(
            eid="e4", ts="2026-06-15T00:01:30Z",
            claim_id="cl-handoff", decision="accept", sid="sv-B",
        ),
    ]
    state = claims.latest_claims_by_target(events)
    rec = state[("task", "e-1648")]
    assert rec["from_session_id"] == "sv-B"
    assert "_conflicts" not in rec


# ---------------------------------------------------------------------------
# Local persistence
# ---------------------------------------------------------------------------

def test_local_record_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_DIR", str(tmp_path))
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="ms",
        target_id="ms-55",
    )
    claims.record_local_claim(p, beacon_dir=str(tmp_path))
    all_claims = claims.list_local_claims(beacon_dir=str(tmp_path))
    assert len(all_claims) == 1
    assert all_claims[0]["claim_id"] == p["claim_id"]


def test_local_filter_by_mine(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_DIR", str(tmp_path))
    a = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="ms",
        target_id="ms-55",
    )
    b = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-B",
        target_kind="ms",
        target_id="ms-66",
    )
    claims.record_local_claim(a, beacon_dir=str(tmp_path))
    claims.record_local_claim(b, beacon_dir=str(tmp_path))
    mine = claims.list_local_claims(mine="sv-A", beacon_dir=str(tmp_path))
    assert len(mine) == 1
    assert mine[0]["from_session_id"] == "sv-A"


def test_local_filter_by_target(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_DIR", str(tmp_path))
    a = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="ms",
        target_id="ms-55",
    )
    b = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="task",
        target_id="e-1648",
    )
    claims.record_local_claim(a, beacon_dir=str(tmp_path))
    claims.record_local_claim(b, beacon_dir=str(tmp_path))
    out = claims.list_local_claims(
        target_kind="task", beacon_dir=str(tmp_path),
    )
    assert len(out) == 1
    assert out[0]["target"]["kind"] == "task"


def test_local_release_drops_entry(tmp_path):
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="ms",
        target_id="ms-55",
    )
    claims.record_local_claim(p, beacon_dir=str(tmp_path))
    assert claims.release_local_claim(p["claim_id"], beacon_dir=str(tmp_path))
    assert claims.list_local_claims(beacon_dir=str(tmp_path)) == []


def test_local_release_unknown_id_false(tmp_path):
    assert not claims.release_local_claim(
        "cl-does-not-exist", beacon_dir=str(tmp_path),
    )


def test_local_record_idempotent_overwrites(tmp_path):
    p = claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-A",
        target_kind="ms",
        target_id="ms-55",
        intent="first",
        claim_id="cl-fixed",
    )
    claims.record_local_claim(p, beacon_dir=str(tmp_path))
    p2 = dict(p)
    p2["intent"] = "updated"
    claims.record_local_claim(p2, beacon_dir=str(tmp_path))
    out = claims.list_local_claims(beacon_dir=str(tmp_path))
    assert len(out) == 1
    assert out[0]["intent"] == "updated"


def test_local_read_missing_file_returns_empty(tmp_path):
    """Pristine .beacon/ with no active_claims.json → empty list."""
    assert claims.list_local_claims(beacon_dir=str(tmp_path)) == []


def test_local_corrupt_file_returns_empty(tmp_path):
    """Malformed JSON in active_claims.json must not crash list_local_claims
    — the consumer should still be able to render an empty list and
    log a separate diagnostic somewhere."""
    path = os.path.join(str(tmp_path), "active_claims.json")
    with open(path, "w") as f:
        f.write("{not json")
    assert claims.list_local_claims(beacon_dir=str(tmp_path)) == []
