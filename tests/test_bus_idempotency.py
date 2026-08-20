"""DM duplicate-delivery idempotency key (ms-110 / e-4001).

The hole this closes: ``ApiClient._request`` has a 30s timeout and no retry.
When a bus POST reaches the server and the event is recorded, but the *ack*
is lost (timeout / connection reset / hang), the caller believes the send
failed. A retry then creates a SECOND event → the recipient gets the DM twice.

The fix gives each logical send a stable ``client_event_id``. On an ambiguous
failure the client retries with the same key + ``is_retry``; the server, only
on that retry path, looks up the recent event carrying the key and returns it
verbatim (``idempotent_replay``) instead of appending a duplicate. The common
first-send path scans nothing.

Two halves are pinned here:
  * server: POST /bus dedup semantics (create once, replay returns the same
    event, unseen key still creates, non-retry duplicates are NOT deduped).
  * client: ApiClient.post_bus_event retries the SAME key on ConnectionError,
    stops on success, gives up after max_retries, and does NOT retry a
    definite HTTPError.
"""

from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

PROJECT_ID = "test-bus-idempotency"

_PROJECT_DOC = {
    "name": "test",
    "milestones": [],
    "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
    ],
}
_SESSIONS = [
    {"session_id": "sv-alice", "user_id": "uid-alice",
     "actor": {"email": "alice@example.com"}},
    {"session_id": "sv-alice2", "user_id": "uid-alice",
     "actor": {"email": "alice@example.com"}},
]

_bus_store: dict[str, list[dict]] = {}
_audit_store: list[dict] = []
_event_seq = [0]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"ev-{_event_seq[0]:06d}"
    _bus_store.setdefault(project_id, []).append(
        {"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_list_bus_events(project_id: str, since: str = "", channel: str = "",
                          limit: int = 100) -> list[dict]:
    """本物と同じ契約: since より後を **古い順** に並べ、先頭 limit 件を返す。

    2026-08-20 / e-5369 — 以前このダブルは rows[-limit:] (= 新しい方から limit 件)
    を返しており、実装 (古い順に先頭 limit 件) と逆だった。そのため「dedup が最古
    100 件しか見ていない」という本番の実バグをテストが素通りさせていた。契約を
    取り違えたダブルは、テストが緑であることの意味を消す。
    """
    rows = _bus_store.get(project_id, [])
    if since:
        rows = [r for r in rows if str(r.get("created_at") or "") > since]
    if channel:
        rows = [r for r in rows if r.get("channel") == channel]
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""))
    if limit:
        rows = rows[:limit]
    return copy.deepcopy(rows)


class _FakeVerify:
    rejection_reason = None
    effective_tier = "T1"
    steps: dict = {}

    def to_audit_dict(self) -> dict:
        return {}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    import app as app_module
    import store_router as db_module
    import firestore_client

    _bus_store.clear()
    _audit_store.clear()
    _event_seq[0] = 0

    for name, ref in (
        ("append_bus_event", _mock_append_bus_event),
        ("list_bus_events", _mock_list_bus_events),
        ("list_sessions", lambda pid: copy.deepcopy(_SESSIONS)),
        ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
        ("save_project", lambda pid, data: None),
        ("list_projects", lambda: []),
        ("append_bus_audit",
         lambda pid, rec: (_audit_store.append(rec), "audit-1")[1]),
        ("put_bus_event_approval", lambda *a, **k: {"approval_status": "pending"}),
        ("append_decision_event", lambda *a, **k: "dec-1"),
        ("list_treks", lambda *a, **k: []),
        ("find_bus_event", lambda pid, eid: None),
        ("get_bus_cursor", lambda pid, rid: {}),
        ("check_and_record_bus_nonce", lambda *a, **k: True),
        ("set_bus_event_receipt", lambda *a, **k: None),
        ("get_or_create_user", lambda *a, **k: None),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)

    monkeypatch.setattr(app_module.envelope_mod, "verify",
                        lambda *a, **k: _FakeVerify())
    monkeypatch.setattr(app_module.envelope_mod, "validate_t5_payload",
                        lambda p: None)
    monkeypatch.setattr(app_module.envelope_mod, "decide_delivery",
                        lambda **k: "propose-to-ai")
    monkeypatch.setattr(app_module, "_start_watcher", lambda project_id: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda project_id: None)
    monkeypatch.setattr(app_module, "_auth_enabled", False)

    async def _noop_fanout(project_id, event):
        return None

    monkeypatch.setattr(app_module, "_fanout_bus_event", _noop_fanout)
    return app_module


def _post(app_module, *, client_event_id="", is_retry=False, text="hi"):
    from fastapi.testclient import TestClient

    body = {
        "channel": "dm",
        "sender_session_id": "sv-alice",
        "payload": {"recipient_session_id": "sv-alice2", "text": text},
        "envelope": {"tier": "T1", "actions_authorized": []},
    }
    if client_event_id:
        body["client_event_id"] = client_event_id
    if is_retry:
        body["is_retry"] = True
    return TestClient(app_module.app).post(
        f"/api/projects/{PROJECT_ID}/bus", json=body)


# ---------------------------------------------------------------------------
# Server: POST /bus dedup semantics
# ---------------------------------------------------------------------------

def test_first_send_creates_and_stamps_key(wired):
    resp = _post(wired, client_event_id="ce-1")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("idempotent_replay") is not True
    events = _bus_store[PROJECT_ID]
    assert len(events) == 1
    assert events[0]["client_event_id"] == "ce-1"


def test_retry_same_key_returns_original_without_duplicate(wired):
    first = _post(wired, client_event_id="ce-2", text="only once")
    first_id = first.json()["event_id"]

    retry = _post(wired, client_event_id="ce-2", is_retry=True, text="only once")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["idempotent_replay"] is True
    assert body["event_id"] == first_id          # same event, not a new one
    assert len(_bus_store[PROJECT_ID]) == 1       # NO duplicate appended


def test_retry_unseen_key_still_creates(wired):
    # A retry whose original never landed (ambiguous failure that truly dropped
    # the message) must still deliver — favour delivery over dedup on a miss.
    resp = _post(wired, client_event_id="ce-never-seen", is_retry=True)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("idempotent_replay") is not True
    assert len(_bus_store[PROJECT_ID]) == 1


def test_non_retry_duplicate_key_is_not_deduped(wired):
    # We trust the client's is_retry flag (only the sender knows a result was
    # ambiguous). Two first-attempt sends — even sharing a key — both deliver;
    # genuine resends must not be silently swallowed.
    _post(wired, client_event_id="ce-3")
    _post(wired, client_event_id="ce-3")   # is_retry=False
    assert len(_bus_store[PROJECT_ID]) == 2


def test_履歴が1万件あっても再送の元メッセージを見つけられる(wired):
    """e-5369 の回帰テスト。

    実装 list_bus_events の契約は「古い順に先頭 limit 件」。従来 dedup はこれを
    since 無しで呼んでいたため、見ていたのは最新ではなく **最古** の 100 件だった。
    本番 beacon-b95643 では最古 100 件に含まれる最新の created_at が 2026-06-09、
    実際の最新は 2026-08-20 で、2 ヶ月半にわたり重複チェックが当たっていなかった。

    件数が少ないと最古 100 件と最新 100 件が一致するため、既存テスト (イベント 1〜2
    件) では永久に検出できない。ここでは古い履歴を 1 万件積んでから再送する。
    """
    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    _bus_store[PROJECT_ID] = [
        {"event_id": f"old-{i:06d}", "channel": "dm",
         "created_at": (base + datetime.timedelta(seconds=i)).strftime(
             "%Y-%m-%dT%H:%M:%S.%fZ"),
         "payload": {"text": "old"}}
        for i in range(10_000)
    ]

    first = _post(wired, client_event_id="ce-scale", text="only once")
    assert first.status_code == 200, first.text
    first_id = first.json()["event_id"]
    assert len(_bus_store[PROJECT_ID]) == 10_001

    retry = _post(wired, client_event_id="ce-scale", is_retry=True,
                  text="only once")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["idempotent_replay"] is True, "1万件の履歴の陰で元メッセージを見失った"
    assert body["event_id"] == first_id
    assert len(_bus_store[PROJECT_ID]) == 10_001, "二重送信された"


# ---------------------------------------------------------------------------
# Client: ApiClient.post_bus_event retry-on-ambiguous
# ---------------------------------------------------------------------------

def test_client_retries_same_key_on_connectionerror(monkeypatch):
    import api_client as ac
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    fake = ac.ApiClient("http://testserver", token="")

    calls = []

    def flaky_post(path, body):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            raise ConnectionError("ambiguous timeout")
        return {"event_id": "ev-ok", **body}

    fake.post = flaky_post
    out = fake.post_bus_event(PROJECT_ID, "dm", payload={"text": "hi"})

    assert out["event_id"] == "ev-ok"
    assert len(calls) == 2
    # same idempotency key across the retry, and the retry is flagged
    assert calls[0]["client_event_id"] == calls[1]["client_event_id"]
    assert "is_retry" not in calls[0] or calls[0]["is_retry"] is False
    assert calls[1]["is_retry"] is True


def test_client_gives_up_after_max_retries(monkeypatch):
    import api_client as ac
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    fake = ac.ApiClient("http://testserver", token="")

    calls = []

    def always_fail(path, body):
        calls.append(1)
        raise ConnectionError("still ambiguous")

    fake.post = always_fail
    with pytest.raises(ConnectionError):
        fake.post_bus_event(PROJECT_ID, "dm", payload={"text": "hi"},
                            max_retries=2)
    assert len(calls) == 3   # initial + 2 retries


def test_client_does_not_retry_definite_httperror(monkeypatch):
    import api_client as ac
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    fake = ac.ApiClient("http://testserver", token="")

    calls = []

    def http_error(path, body):
        calls.append(1)
        raise RuntimeError("API error 403: forbidden")

    fake.post = http_error
    with pytest.raises(RuntimeError):
        fake.post_bus_event(PROJECT_ID, "dm", payload={"text": "hi"})
    assert len(calls) == 1   # a definite server response is NOT ambiguous


def test_client_retries_same_key_on_timeouterror(monkeypatch):
    # review e-4001 (AX): ``post`` wraps most url/socket errors as
    # ConnectionError, but a bare socket-read timeout can surface as
    # TimeoutError. That is equally ambiguous (the request may have landed), so
    # the retry loop must treat it the same — otherwise the declared "retry on
    # timeout" silently does not fire and the double-delivery returns.
    import api_client as ac
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    fake = ac.ApiClient("http://testserver", token="")

    calls = []

    def flaky_post(path, body):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return {"event_id": "ev-ok", **body}

    fake.post = flaky_post
    out = fake.post_bus_event(PROJECT_ID, "dm", payload={"text": "hi"})

    assert out["event_id"] == "ev-ok"
    assert len(calls) == 2
    assert calls[0]["client_event_id"] == calls[1]["client_event_id"]
    assert calls[1]["is_retry"] is True


# ---------------------------------------------------------------------------
# Server: reject the silent-failure edges of the dedup interface (review e-4001)
# ---------------------------------------------------------------------------

def test_retry_without_key_is_rejected_422(wired):
    # is_retry=true with no client_event_id is a contradiction: the client says
    # "resend" but gives nothing to dedup on. Falling through would silently
    # create a NEW event (the double-delivery this feature prevents). Reject.
    resp = _post(wired, is_retry=True)   # no client_event_id
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "is_retry_requires_client_event_id"
    assert len(_bus_store.get(PROJECT_ID, [])) == 0   # nothing created


def test_retry_same_key_different_payload_conflicts_409(wired):
    # Reusing a key with a DIFFERENT payload must not silently return the old
    # event (dropping the new content). A reused key means "same logical send";
    # a different payload means the caller reused a key by mistake.
    first = _post(wired, client_event_id="ce-dup", text="original")
    assert first.status_code == 200, first.text

    conflict = _post(wired, client_event_id="ce-dup", is_retry=True,
                     text="DIFFERENT body")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["error"] == "client_event_id_payload_mismatch"
    assert len(_bus_store[PROJECT_ID]) == 1   # no duplicate, new content rejected
