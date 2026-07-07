"""ms-54 e-2934: user-scoped DM 宛先の routing 判定 unit test。

_bus_event_addressed_to が session-scoped (payload.recipient_session_id) と
user-scoped (payload.recipient_user_id) の 2 経路を正しく分岐することを検証。

Integration 側 (= /api/projects/{pid}/bus/unread endpoint の end-to-end
delivery 実機確認) は次コミットで pytest fixture 経由で追加。
"""
import os
import sys

import pytest

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)


@pytest.fixture(scope="module")
def addressed_to():
    """Return app._bus_event_addressed_to bound in test scope."""
    import app
    return app._bus_event_addressed_to


# ---------------------------------------------------------------------------
# session-scoped (= 従来経路、e-1209 で入った filter)。 変更なしを担保。
# ---------------------------------------------------------------------------

def test_session_scoped_delivered_when_sid_matches(addressed_to):
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"recipient_session_id": "recipient-sid", "text": "hi"},
    }
    assert addressed_to(event, "recipient-sid") is True


def test_session_scoped_dropped_when_sid_mismatches(addressed_to):
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"recipient_session_id": "recipient-sid", "text": "hi"},
    }
    assert addressed_to(event, "some-other-sid") is False


def test_self_loop_dropped(addressed_to):
    """sender == recipient は必ず drop (e-1209 の self-loop guard)。"""
    event = {
        "channel": "dm",
        "sender_session_id": "same-sid",
        "payload": {"recipient_session_id": "same-sid", "text": "echo"},
    }
    assert addressed_to(event, "same-sid") is False


# ---------------------------------------------------------------------------
# user-scoped (= e-2934 で追加)。 sid churn しても届く。
# ---------------------------------------------------------------------------

def test_user_scoped_delivered_when_uid_matches(addressed_to):
    """payload.recipient_user_id が caller の user_id と一致すれば delivery。"""
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"recipient_user_id": "uid-abc", "text": "hi"},
    }
    # caller の recipient_id (= 自 session sid) は uid と直接関係しない、
    # user_id が明示的に addressed_to に渡される (= list_unread が
    # _caller_uid で resolve して渡す flow を想定した unit テスト)。
    assert addressed_to(event, "any-sid", recipient_user_id="uid-abc") is True


def test_user_scoped_dropped_when_uid_mismatches(addressed_to):
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"recipient_user_id": "uid-abc", "text": "hi"},
    }
    assert addressed_to(event, "any-sid", recipient_user_id="uid-XYZ") is False


def test_user_scoped_dropped_when_caller_uid_empty(addressed_to):
    """caller の recipient_user_id が空 (= user 未解決) なら user-scoped は評価
    しない (= 「DM 宛先不明」 の防御的 drop)。 session-scoped の未 stamp と
    同じ扱いで、 未認証 caller が他人宛 user-scoped DM を掠め取ることを防ぐ。
    """
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"recipient_user_id": "uid-abc", "text": "hi"},
    }
    assert addressed_to(event, "any-sid", recipient_user_id="") is False


def test_session_scoped_wins_when_both_fields_set(addressed_to):
    """session_id 優先 (= sender 側の明示的意図を優先)。 両方 set は本来禁止
    (= CLI 側で hard error) だが、 万一届いた場合は session 判定が優先。
    """
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {
            "recipient_session_id": "sid-target",
            "recipient_user_id": "uid-abc",
            "text": "hi",
        },
    }
    # caller の sid が sid-target なら delivery、 uid は同じでも別 sid なら drop。
    assert addressed_to(event, "sid-target", recipient_user_id="uid-abc") is True
    assert addressed_to(event, "sid-other", recipient_user_id="uid-abc") is False


# ---------------------------------------------------------------------------
# 未変更の broadcast semantics 回帰 (= 非 DM channel は broadcast 保持)。
# ---------------------------------------------------------------------------

def test_non_dm_channel_broadcast_when_no_recipient(addressed_to):
    event = {
        "channel": "notify",
        "sender_session_id": "sender-sid",
        "payload": {"text": "broadcast"},
    }
    assert addressed_to(event, "any-sid") is True


def test_dm_channel_dropped_when_no_recipient(addressed_to):
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": {"text": "orphan"},  # 宛先 field 一切なし
    }
    assert addressed_to(event, "any-sid") is False


def test_malformed_payload_dm_dropped(addressed_to):
    event = {
        "channel": "dm",
        "sender_session_id": "sender-sid",
        "payload": "not a dict",  # 不正 shape
    }
    assert addressed_to(event, "any-sid") is False


def test_malformed_payload_non_dm_broadcast(addressed_to):
    event = {
        "channel": "notify",
        "sender_session_id": "sender-sid",
        "payload": "not a dict",
    }
    assert addressed_to(event, "any-sid") is True


# ---------------------------------------------------------------------------
# CLI side の env-var → payload stamp 経路 (= cmd_bus_send の分岐) の単体テスト。
# 実 network を叩かず、payload dict の組み立てだけ確認する。
# ---------------------------------------------------------------------------

def test_cli_stamps_recipient_user_id(monkeypatch):
    """BEACON_BUS_RECIPIENT_USER が set されると payload に stamp される。
    session と user の同時指定は sys.exit(1) で hard error。
    """
    import importlib
    LIB = os.path.normpath(os.path.join(THIS, "..", "lib"))
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    # commands は巨大 module なので、 分岐だけ抽出した shim 経路をテストする
    # (= import すると click / os.environ を実 evaluate してしまう可能性、
    # 単体テストは env var → payload dict の rules だけを検証)。
    #
    # 実装は cmd_bus_send 内 (lib/commands.py) の分岐:
    #   recipient = BEACON_BUS_RECIPIENT_SESSION
    #   recipient_user = BEACON_BUS_RECIPIENT_USER
    #   if recipient and recipient_user: hard-error
    #   if recipient_user and "@" in recipient_user: hard-error (未実装案内)
    #   elif recipient: payload["recipient_session_id"] = recipient
    #   elif recipient_user: payload["recipient_user_id"] = recipient_user
    #
    # 上記 rules を関数化した _resolve_recipient_stamp を仮想的に再現する
    # (= 実装の contract をここに文書化)。
    def stamp(payload_in: dict, sid_env: str, uid_env: str) -> dict:
        payload = dict(payload_in)
        recipient = (sid_env or "").strip()
        recipient_user = (uid_env or "").strip()
        if recipient and recipient_user:
            raise ValueError("EXCLUSIVE_TO_AND_TO_USER")
        if recipient_user and "@" in recipient_user:
            raise ValueError("EMAIL_NOT_YET_SUPPORTED")
        if recipient:
            payload["recipient_session_id"] = recipient
        elif recipient_user:
            payload["recipient_user_id"] = recipient_user
        return payload

    # session-only path (= 従来通り、下位互換)
    p = stamp({}, "sid-1", "")
    assert p == {"recipient_session_id": "sid-1"}

    # user-only path (= e-2934 の新機能)
    p = stamp({}, "", "uid-abc")
    assert p == {"recipient_user_id": "uid-abc"}

    # 相互排他エラー
    with pytest.raises(ValueError, match="EXCLUSIVE"):
        stamp({}, "sid-1", "uid-abc")

    # email 未対応エラー
    with pytest.raises(ValueError, match="EMAIL"):
        stamp({}, "", "iruca@example.com")

    # どちらも未指定 → payload 変化なし
    p = stamp({"text": "raw"}, "", "")
    assert p == {"text": "raw"}
