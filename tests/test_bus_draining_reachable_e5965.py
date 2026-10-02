"""ms-165 / e-5965 — the *progress* liveness dimension: draining / reachable.

A recipient can be LIVE (bridge polling, ``last_poll_at`` fresh) yet never
CONSUME its inbox — DMs pile up unread and the sender is fooled into "sent✓
delivered✗". These tests pin the four SPEC 方針 guarantees:

  1. draining derivation is False when unread backlog is stale (pure helper +
     the server ``_stamp_session_liveness`` end).
  2. a send to a WEDGED (live-but-not-draining) recipient returns loud
     structured flags (``recipient_wedged`` / ``delivery_uncertain``).
  3. a send to a NORMAL (live + draining) recipient is BYTE-UNCHANGED — no new
     keys (the negative-regression guard).
  4. an idle fork stays LIVE and reachable (the ``live`` union is unchanged, so
     the picker never drops it).
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

import bus_liveness  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ===========================================================================
# 1. Pure derivation (SPEC 方針 a) — the "stale unread ⇒ not draining" rule.
# ===========================================================================

class TestDeriveDraining:
    def test_stale_unread_is_not_draining(self):
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))  # older than 300s window
        assert bus_liveness.derive_draining(old, now, 300) is False

    def test_recent_unread_is_still_draining(self):
        now = _now()
        fresh = _iso(now - datetime.timedelta(seconds=5))  # in-flight, not a wedge
        assert bus_liveness.derive_draining(fresh, now, 300) is True

    def test_no_backlog_is_unknown_not_healthy(self):
        """契約の変更点 (ms-173 / e-6777): バックログが無いときは ``None`` (不明)。

        以前はここで ``True`` (健全) を固定していた。そのため「誰からも送られない
        セッション」と「瞬時に消化しているセッション」が同じ最高評価になり、**受信して
        いないセッションが最も健全に見える** 指標になっていた (実測 2026-10-01: live 14 行
        のうち 13 行が draining=True だが大半は送られた実績が無く、DM が集まる本体だけが
        draining=False で到達不能と判定された)。消化の証拠が無いなら「不明」が正直。

        送信経路は不変: is_reachable / classify_send_delivery はどちらも ``False`` だけを
        悪い信号として扱い ``None`` と ``True`` を同じく扱う (下の 2 テストで固定済)。
        """
        assert bus_liveness.derive_draining("", _now(), 300) is None

    def test_unparseable_is_unknown(self):
        assert bus_liveness.derive_draining("not-a-date", _now(), 300) is None

    def test_reachable_only_false_on_definitive_wedge(self):
        assert bus_liveness.is_reachable(True, True) is True
        assert bus_liveness.is_reachable(True, None) is True   # unknown ⇒ keep
        assert bus_liveness.is_reachable(True, False) is False  # wedge ⇒ drop
        assert bus_liveness.is_reachable(False, True) is False  # not live ⇒ drop

    def test_classify_send_delivery_three_tiers(self):
        assert bus_liveness.classify_send_delivery(True, True) == bus_liveness.SEND_NORMAL
        assert bus_liveness.classify_send_delivery(True, None) == bus_liveness.SEND_NORMAL
        assert bus_liveness.classify_send_delivery(True, False) == bus_liveness.SEND_WEDGED
        assert bus_liveness.classify_send_delivery(False, False) == bus_liveness.SEND_NOT_LIVE
        assert bus_liveness.classify_send_delivery(False, True) == bus_liveness.SEND_NOT_LIVE


# ===========================================================================
# 4. Directory stamping — idle fork stays live (picker no-regression) + the
#    server end of the draining derivation (1).
# ===========================================================================

class TestStampReachability:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        # Redis unavailable ⇒ ws_live=None ⇒ live decided by poll heartbeat.
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        return app_module

    def _live_session(self, now):
        return {"session_id": "sv-fork",
                "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
                "last_active": _iso(now - datetime.timedelta(seconds=3)),
                "poll_interval_ms": 5000}

    def test_idle_fork_with_no_backlog_stays_live_and_reachable(self, _app, monkeypatch):
        now = _now()
        # No events addressed to it (idle fork, nobody sent anything).
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        # live union UNCHANGED (SPEC 方針 c): a healthy poll keeps it live.
        assert s["live"] is True
        # ms-173 / e-6777: バックログが無い = 消化の証拠が無い ⇒ 不明 (健全と言わない)。
        assert s["draining"] is None
        # **ここが本質**: 不明でも reachable は True のまま = 配信は一切変わらない。
        # 指標の嘘だけを消し、受信者を誤って落とすリスクは負わない。
        assert s["reachable"] is True    # live AND not-False ⇒ reachable

    def test_live_but_stale_backlog_is_wedged_not_reachable(self, _app, monkeypatch):
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        # An event addressed to sv-fork, unopened, older than the window.
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            {"event_id": "old1", "channel": "dm", "sender_session_id": "sv-other",
             "created_at": old, "payload": {"recipient_session_id": "sv-fork"}},
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is True          # still LIVE (transport intact)
        assert s["draining"] is False     # but not consuming (wedged)
        assert s["reachable"] is False    # ⇒ not reachable for the strict send

    def test_not_live_skips_scan_and_is_unreachable(self, _app, monkeypatch):
        now = _now()
        calls = {"n": 0}

        def _count_list(pid, **k):
            calls["n"] += 1
            return []
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", _count_list)
        # Stale poll ⇒ not live.
        s = {"session_id": "sv-dead",
             "last_poll_at": _iso(now - datetime.timedelta(hours=1)),
             "poll_interval_ms": 5000}
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is False
        assert s["draining"] is None       # skipped (cost bound)
        assert s["reachable"] is False
        assert calls["n"] == 0             # no store scan for a not-live session


# ===========================================================================
# 2 + 3. Send path (SPEC 方針 b) — graded response through POST /bus.
# ===========================================================================

PROJECT_ID = "test-draining-e5965"

_PROJECT_DOC = {
    "name": "test", "milestones": [], "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
    ],
}


class _FakeVerify:
    rejection_reason = None
    effective_tier = "T1"
    steps: dict = {}

    def to_audit_dict(self):
        return {}


class TestSendPathGraded:
    @pytest.fixture
    def wired(self, monkeypatch):
        import app as app_module
        import store_router as db_module
        import firestore_client

        now = _now()
        # Two same-user sessions (sender + recipient) so the cross-user consent
        # gate is carved out — this isolates the ms-165 reachability logic.
        sessions = [
            {"session_id": "sv-alice", "user_id": "uid-alice",
             "actor": {"email": "alice@example.com"}},
            {"session_id": "sv-rehab", "user_id": "uid-alice",
             "actor": {"email": "alice@example.com"},
             "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
             "poll_interval_ms": 5000},
        ]
        # Per-test backlog the draining scan sees (default: none ⇒ draining).
        backlog = {"events": []}

        def _list_bus_events(pid, **k):
            return copy.deepcopy(backlog["events"])

        seq = [0]

        def _append(pid, data):
            seq[0] += 1
            return f"ev-{seq[0]:06d}"

        for name, ref in (
            ("append_bus_event", _append),
            ("list_bus_events", _list_bus_events),
            ("list_sessions", lambda pid: copy.deepcopy(sessions)),
            ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
            ("save_project", lambda pid, data: None),
            ("list_projects", lambda: []),
            ("append_bus_audit", lambda pid, rec: "audit-1"),
            ("append_decision_event", lambda *a, **k: "dec-1"),
            ("list_treks", lambda *a, **k: []),
            ("get_bus_cursor", lambda pid, rid: {}),
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
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
        monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)
        monkeypatch.setattr(app_module, "_auth_enabled", False)

        async def _noop_fanout(pid, event):
            return None
        monkeypatch.setattr(app_module, "_fanout_bus_event", _noop_fanout)
        return app_module, backlog

    def _post(self, app_module):
        from fastapi.testclient import TestClient
        body = {
            "channel": "dm", "sender_session_id": "sv-alice",
            "payload": {"text": "hi", "recipient_session_id": "sv-rehab"},
            "envelope": {"tier": "T1", "actions_authorized": []},
        }
        client = TestClient(app_module.app)
        return client.post(f"/api/projects/{PROJECT_ID}/bus", json=body)

    def test_send_to_wedged_recipient_flags_delivery_uncertain(self, wired):
        app_module, backlog = wired
        # Recipient has a stale unopened event addressed to it ⇒ wedged.
        backlog["events"] = [
            {"event_id": "old1", "channel": "dm", "sender_session_id": "sv-x",
             "created_at": _iso(_now() - datetime.timedelta(seconds=600)),
             "payload": {"recipient_session_id": "sv-rehab"}},
        ]
        resp = self._post(app_module)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out.get("recipient_wedged") is True
        assert out.get("delivery_uncertain") is True

    def test_send_to_normal_recipient_is_byte_unchanged(self, wired):
        app_module, backlog = wired
        backlog["events"] = []  # no backlog ⇒ draining ⇒ normal
        resp = self._post(app_module)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        # Negative regression: the healthy common path grows NO new keys.
        assert "recipient_wedged" not in out
        assert "delivery_uncertain" not in out


# ===========================================================================
# ms-173 / e-6777 — 指標が「受信しないこと」を報酬にしない。
#
# draining は「消化できているか」を答える指標だが、バックログが無いときに True
# (健全) を返していたため、送られた実績の無いセッションが最高評価になっていた。
# True は **観測したとき** だけ出す。
# ===========================================================================

class TestDrainingDoesNotRewardNotReceiving:
    def test_never_received_and_drained_instantly_are_distinguishable(self):
        """この 2 つが同じ値だったのが e-6777 の核。"""
        now = _now()
        never_received = bus_liveness.derive_draining("", now, 300)
        drained_fast = bus_liveness.derive_draining(
            _iso(now - datetime.timedelta(seconds=5)), now, 300)
        assert never_received is None, "送られた実績が無いのに健全と主張している"
        assert drained_fast is True, "実際に消化している証拠は True であるべき"
        assert never_received is not drained_fast

    def test_true_requires_observed_consumption(self):
        """True を返す経路が「未読があって新しい」ときだけであること。"""
        now = _now()
        assert bus_liveness.derive_draining(
            _iso(now - datetime.timedelta(seconds=1)), now, 300) is True
        for no_evidence in ("", None, "not-a-date"):
            assert bus_liveness.derive_draining(no_evidence, now, 300) is None

    def test_delivery_behaviour_is_unchanged_by_the_honesty_fix(self):
        """**配信は一切変わらない**。これがこの修正を安全に入れられる理由で、
        ここが壊れると受信者を誤って落とすので、指標の正直さより優先される。"""
        for v in (None, True):
            assert bus_liveness.is_reachable(True, v) is True
            assert bus_liveness.classify_send_delivery(True, v) \
                == bus_liveness.SEND_NORMAL
        # 確定的な wedge だけが悪い信号。
        assert bus_liveness.is_reachable(True, False) is False
        assert bus_liveness.classify_send_delivery(True, False) \
            == bus_liveness.SEND_WEDGED

    def test_wedge_detection_is_untouched(self):
        """古い未読は従来どおり wedge。指標を正直にしただけで、検出は緩めない。"""
        now = _now()
        assert bus_liveness.derive_draining(
            _iso(now - datetime.timedelta(seconds=600)), now, 300) is False


# ===========================================================================
# ms-173 / e-6799 — 「未読」を cursor でなく受信の証跡 (receipt) で決める。
#
# cursor (``bus_cursors``) を進めるのは inbox-hook (人の打鍵ごとに走る) と
# ``beacon bus receive`` だけで、idle-wake を実際に配る MCP bridge は ms-140 で
# 意図的に cursor から切り離されている。その cursor を消化の真値として読むと、
# 「配られていない」ではなく「人がまだ打鍵していない」を測ってしまい、DM が最も
# 集まる本体セッションが恒久的に到達不能と判定された。
# ===========================================================================

class TestDeliveredToRecipient:
    def test_own_receipt_counts_as_consumed(self):
        assert bus_liveness.delivered_to_recipient(
            {"delivered_at": "2026-10-02T01:00:00Z", "delivered_by": "sv-a"},
            "sv-a") is True

    def test_another_sessions_receipt_does_not_count(self):
        """receipt は stage ごとに first-write-wins = 最初に取った 1 名の名前しか
        残らない。broadcast で「誰かが取った」を全員の消化と読むと、残り全員の
        本物の wedge をまとめて隠す。"""
        assert bus_liveness.delivered_to_recipient(
            {"delivered_at": "2026-10-02T01:00:00Z", "delivered_by": "sv-other"},
            "sv-a") is False

    def test_no_receipt_is_not_consumed(self):
        assert bus_liveness.delivered_to_recipient(
            {"created_at": "2026-10-02T01:00:00Z"}, "sv-a") is False

    def test_unattributed_receipt_is_not_consumed(self):
        """``delivered_at`` はあるが ``delivered_by`` が空 = 帰属不明。安全側に
        倒して未消化として残す (wedge を見逃す方向に倒さない)。"""
        assert bus_liveness.delivered_to_recipient(
            {"delivered_at": "2026-10-02T01:00:00Z", "delivered_by": ""},
            "sv-a") is False

    def test_bad_input_is_not_consumed(self):
        assert bus_liveness.delivered_to_recipient(None, "sv-a") is False
        assert bus_liveness.delivered_to_recipient({"delivered_at": "t",
                                                    "delivered_by": "sv-a"},
                                                   "") is False


class TestStaleCursorDoesNotFakeAWedge:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        return app_module

    def _live_session(self, now):
        return {"session_id": "sv-main",
                "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
                "last_active": _iso(now - datetime.timedelta(seconds=3)),
                "poll_interval_ms": 5000}

    def _dm(self, created_at, *, delivered_by=None, recipient="sv-main"):
        e = {"event_id": "e1", "channel": "dm", "sender_session_id": "sv-peer",
             "created_at": created_at,
             "payload": {"recipient_session_id": recipient}}
        if delivered_by is not None:
            # bridge は filter chain より手前で全取得 event に刻む (e-1348)。
            e["delivered_at"] = created_at
            e["delivered_by"] = delivered_by
        return e

    def test_delivered_dm_behind_a_frozen_cursor_is_not_a_wedge(self, _app,
                                                                monkeypatch):
        """e-6799 の再現形。実測された本番の形そのもの: cursor は 25 時間止まって
        いて、その先に 10 時間前の DM が居る。しかし receipt は created_at +0 秒で、
        bridge は即座に配っていた。これを wedge と呼んでいた。"""
        now = _now()
        old = _iso(now - datetime.timedelta(hours=10))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {
            "last_seen_at": _iso(now - datetime.timedelta(hours=25))})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            self._dm(old, delivered_by="sv-main"),
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is True
        assert s["draining"] is None, "配信済なのに未消化のバックログと数えている"
        assert s["reachable"] is True, "配信できているセッションを到達不能と判定した"

    def test_undelivered_dm_is_still_a_wedge(self, _app, monkeypatch):
        """test-the-test: 本物の wedge (= 受信経路が取っていない) は従来どおり
        False で落ちる。ここが緑のままだと「偽の安全」になる。"""
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            self._dm(old),  # receipt なし = bridge が一度も取っていない
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is True
        assert s["draining"] is False
        assert s["reachable"] is False

    def test_receipt_by_another_session_does_not_clear_my_backlog(self, _app,
                                                                  monkeypatch):
        """broadcast (= dm 以外の channel で宛先無指定) は全員宛と判定される。
        他人が先に取った receipt で自分の wedge が消えてはいけない。"""
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            {"event_id": "b1", "channel": "operation-trigger",
             "sender_session_id": "sv-peer", "created_at": old, "payload": {},
             "delivered_at": old, "delivered_by": "sv-someone-else"},
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["draining"] is False
        assert s["reachable"] is False

    def test_oldest_unconsumed_wins_over_a_newer_delivered_one(self, _app,
                                                               monkeypatch):
        """配信済を飛ばして走査を続け、未消化の最古を返すこと (先頭が配信済でも
        後ろの未消化を取りこぼさない)。"""
        now = _now()
        delivered = _iso(now - datetime.timedelta(hours=3))
        stuck = _iso(now - datetime.timedelta(seconds=900))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            self._dm(delivered, delivered_by="sv-main"),
            self._dm(stuck),
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["draining"] is False, "未消化の最古を飛ばして健全と判定した"
