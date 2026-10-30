"""Tests for ms-93 / e-2502 shared bus protocol (lib/bus_protocol.py).

Pins the canonical contract that Claude Code's channel/bus.mjs and
Codex's lib/codex_receive_loop.py both follow. Three behaviours
specifically catch silent drifts the 2026-06-26 Codex dogfood
uncovered:

1. DM channel + missing recipient = drop (= e-1209, bus.mjs filter 2b)
2. Channel outside the allowlist = drop (= bus.mjs filter 3)
3. ``opened`` ack stage is a real protocol stage (not just ``delivered``)
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import bus_protocol as bp


# ------------------------------------------------------------------ #
# Heartbeat
# ------------------------------------------------------------------ #


class TestHeartbeatBody:
    def test_shape_matches_bus_heartbeat_mjs(self):
        # 引数を 1 つも渡さないときの既定の姿を固定する。
        # **JS との一致はこのテストでは見ていない** (名前が紛らわしいが、固定集合との
        # 比較なので JS を一度も読まない)。両実装の突き合わせは下の
        # test_emittable_keys_do_not_drift_from_the_js_twin が機械的に行う。
        body = bp.heartbeat_body("2026-06-26T00:00:00Z", poll_interval_ms=2000)
        assert set(body) == {"last_active", "last_poll_at", "poll_interval_ms", "shutdown"}

    def test_shutdown_flag_propagates(self):
        body = bp.heartbeat_body("now", poll_interval_ms=1000, shutdown=True)
        assert body["shutdown"] is True

    def test_default_shutdown_is_false(self):
        body = bp.heartbeat_body("now", poll_interval_ms=1000)
        assert body["shutdown"] is False

    def test_transport_omitted_by_default(self):
        # ms-145 / e-5378: back-compat — no transport arg → field absent, so the
        # server merge doesn't clobber anything and old readers are unaffected.
        body = bp.heartbeat_body("now", poll_interval_ms=1000)
        assert "transport" not in body

    def test_transport_included_when_provided(self):
        # ms-145 / e-5378: Codex loop self-reports poll-only so the directory can
        # explain why the session is on frequent polling (by design, not broken WS).
        body = bp.heartbeat_body(
            "now", poll_interval_ms=2000,
            transport={"ws_state": bp.WS_STATE_POLL_ONLY, "effective_poll_ms": 2000},
        )
        assert body["transport"] == {"ws_state": "poll-only", "effective_poll_ms": 2000}

    def test_context_pct_omitted_by_default(self):
        # ms-159 / e-6499: back-compat — no context_pct arg → field absent, so an
        # old client / un-computed session keeps the row shape (server merge no-op).
        body = bp.heartbeat_body("now", poll_interval_ms=1000)
        assert "context_pct" not in body

    def test_context_pct_included_when_provided(self):
        # ms-159 / e-6499: the receive loop forwards the monitor's context usage %
        # so the roster can surface a context-pressured session.
        body = bp.heartbeat_body("now", poll_interval_ms=2000, context_pct=73)
        assert body["context_pct"] == 73

    def test_context_pct_zero_is_sent(self):
        # 0 is a legitimate value (a fresh session) — it must NOT be dropped as
        # falsy. Only None (unknown) omits the field.
        body = bp.heartbeat_body("now", poll_interval_ms=2000, context_pct=0)
        assert body["context_pct"] == 0


class TestWsStateVocabulary:
    """ms-145 / e-5378: the ws_state value set is defined once here (canonical)
    so emitters (bus.mjs / codex_receive_loop / server) can't silently drift onto
    private strings. Independent review PR#729 flagged the values were scattered."""

    def test_canonical_set_is_the_documented_five(self):
        assert set(bp.WS_STATE_VALUES) == {
            "open", "reconnecting", "connecting", "disabled", "poll-only"
        }

    def test_poll_only_constant_is_in_the_set(self):
        # The Codex loop stamps bp.WS_STATE_POLL_ONLY; it must be a legal value.
        assert bp.WS_STATE_POLL_ONLY in bp.WS_STATE_VALUES


class TestHeartbeatPath:
    def test_includes_project_and_session(self):
        assert bp.heartbeat_path("proj-1", "sid-abc") == "/api/projects/proj-1/sessions/sid-abc"


# ------------------------------------------------------------------ #
# Initial watermark
# ------------------------------------------------------------------ #


class TestInitialWatermark:
    def test_returns_iso8601_now(self):
        ts = bp.initial_watermark_now()
        # Format check — full parse is fine if the string fits.
        from datetime import datetime, timezone
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert parsed.tzinfo is not None
        delta = (datetime.now(timezone.utc) - parsed).total_seconds()
        assert 0 <= delta <= 5


# ------------------------------------------------------------------ #
# Poll URL builder
# ------------------------------------------------------------------ #


class TestPollUnreadPath:
    def test_includes_recipient_and_since(self):
        url = bp.poll_unread_path("proj-1", "sid-abc", "2026-06-26T00:00:00Z")
        assert "/api/projects/proj-1/bus/unread" in url
        assert "recipient_id=sid-abc" in url
        assert "since=2026-06-26T00%3A00%3A00Z" in url or "since=2026-06-26T00:00:00Z" in url

    def test_empty_since_renders_empty_param(self):
        url = bp.poll_unread_path("proj-1", "sid-abc", "")
        assert url.endswith("&since=")


# ------------------------------------------------------------------ #
# Filter chain — the core drift fix
# ------------------------------------------------------------------ #


def _config(sid="sid-abc", watermark="", channels=bp.DEFAULT_ALLOWED_CHANNELS):
    return bp.FilterConfig(
        our_session_id=sid,
        allowed_channels=tuple(channels),
        watermark=watermark,
    )


def _event(**kw):
    base = {
        "event_id": "evt-1",
        "created_at": "2026-06-26T01:00:00Z",
        "channel": "dm",
        "sender_session_id": "other-sid",
        "payload": {"recipient_session_id": "sid-abc", "text": "hi"},
    }
    base.update(kw)
    return base


class TestFilterEvent:
    def test_keeps_dm_addressed_to_us(self):
        assert bp.filter_event(_event(), _config()) == bp.FILTER_KEEP

    def test_drops_self_sent(self):
        e = _event(sender_session_id="sid-abc")
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_SELF

    def test_drops_recipient_mismatch(self):
        e = _event()
        e["payload"]["recipient_session_id"] = "someone-else"
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH

    def test_drops_dm_without_recipient_e1209(self):
        # Codex 2026-06-26 silent drift #1: missing this filter let
        # broadcast traffic (= operation-trigger etc.) into the DM inbox.
        e = _event()
        e["payload"] = {"text": "missing recipient"}
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_DM_UNADDRESSED

    def test_drops_non_dm_channel_outside_allowlist(self):
        # Codex 2026-06-26 silent drift #2: no allowlist filter let
        # test-* channels through.
        e = _event(channel="test-random", payload={})
        assert bp.filter_event(e, _config(channels=("dm",))) == bp.FILTER_DROP_CHANNEL

    def test_keeps_non_dm_channel_in_allowlist(self):
        e = _event(channel="operation-trigger", payload={})
        verdict = bp.filter_event(e, _config(channels=("dm", "operation-trigger")))
        assert verdict == bp.FILTER_KEEP

    def test_keeps_stop_signal_even_outside_allowlist(self):
        # ms-160 e-5800: the remote-STOP kill-switch must reach the receive loop
        # regardless of the opt-in channel allowlist — a session cannot opt OUT
        # of being stoppable. Without this the Codex filter dropped stop-signal
        # (channel-not-allowed) and Codex could not be stopped at all.
        e = _event(channel="stop-signal", payload={})
        assert bp.filter_event(e, _config(channels=("dm",))) == bp.FILTER_KEEP

    def test_stop_signal_addressed_to_another_session_still_drops(self):
        # The allowlist exemption must NOT bypass the recipient filter: a stop
        # aimed at someone else is not ours to act on.
        e = _event(channel="stop-signal",
                   payload={"recipient_session_id": "someone-else"})
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH

    def test_stop_signal_from_self_still_drops(self):
        e = _event(channel="stop-signal", sender_session_id="sid-abc", payload={})
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_SELF

    def test_drops_event_older_than_watermark(self):
        e = _event(created_at="2026-06-25T00:00:00Z")
        verdict = bp.filter_event(e, _config(watermark="2026-06-26T00:00:00Z"))
        assert verdict == bp.FILTER_DROP_WATERMARK

    def test_malformed_event_is_dropped_safely(self):
        assert bp.filter_event(None, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH


# ------------------------------------------------------------------ #
# Ack
# ------------------------------------------------------------------ #


class TestAck:
    def test_path_includes_project_and_event(self):
        assert bp.ack_path("proj-1", "evt-1") == "/api/projects/proj-1/bus/evt-1/ack"

    def test_body_delivered(self):
        body = bp.ack_body(bp.ACK_STAGE_DELIVERED, "sid-abc")
        assert body == {"stage": "delivered", "recipient_session_id": "sid-abc"}

    def test_body_opened_is_a_real_stage(self):
        # Codex 2026-06-26 silent drift #3: only ``delivered`` was wired.
        body = bp.ack_body(bp.ACK_STAGE_OPENED, "sid-abc")
        assert body == {"stage": "opened", "recipient_session_id": "sid-abc"}

    def test_body_rejects_unknown_stage(self):
        with pytest.raises(ValueError):
            bp.ack_body("bogus", "sid-abc")


# --- 両実装の形が離れていかないこと (ms-159 e-7235) ----------------------------
#
# ``heartbeat_body`` の docstring は自分を単一の真実源と宣言し「JS 写し
# (buildHeartbeatBody) と同時に増やすこと (片方だけだと drift)」と書いている。
# ところがそれを機械で確かめるものが無く、既存の
# ``test_shape_matches_bus_heartbeat_mjs`` は固定集合と比べるだけで **JS を一度も
# 読まない**自己参照だった。実際 ms-159 で JS だけに足された項目が 4 つ溜まっていた
# (独立保守性レビュー指摘)。
#
# ここでは**両実装を実際に動かして**、出し得るキーの集合を比べる。

_KNOWN_JS_ONLY = {
    # Codex の受信ループは状態マーカー (.beacon/session-state.json) を読まないので
    # 自己申告できず、これら 4 つは JS 側 (Node bridge) にしか無い。
    # ms-159 e-6244 / e-6245 / e-6488 で JS に足された時点から離れている。
    # **この集合は減る方向にしか動かしてはならない。** 増やすのは「JS にだけ足した」
    # ことの宣言であり、下のテストがそれを赤で止める。
    "declared_state",
    "declared_at",
    "state_since",
    "state_detail",
}


def _js_emittable_keys():
    """JS 側の buildHeartbeatBody を全オプション入りで動かし、出たキーを返す。"""
    node = shutil.which("node")
    if node is None:
        return None
    script = (
        "import {buildHeartbeatBody} from './channel/bus-heartbeat.mjs';"
        "const b = buildHeartbeatBody({nowIso:'t', pollIntervalMs:1, shutdown:true,"
        " transport:{x:1}, declaredState:'running', declaredAt:'t', stateSince:'t',"
        " stateDetail:'d', contextPct:5,"
        " where:{cwd:'/w', git:{branch:'b', head_short:'h', head_subject:'s'}}});"
        "console.log(JSON.stringify(Object.keys(b)));"
    )
    out = subprocess.run([node, "--input-type=module", "-e", script],
                         cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"node 失敗: {out.stderr}"
    return set(json.loads(out.stdout))


def _py_emittable_keys():
    """Python 側を全オプション入りで動かし、出たキーを返す。"""
    return set(bp.heartbeat_body(
        "t", poll_interval_ms=1, shutdown=True, transport={"x": 1},
        context_pct=5,
        where={"cwd": "/w",
               "git": {"branch": "b", "head_short": "h", "head_subject": "s"}},
    ))


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境")
def test_emittable_keys_do_not_drift_from_the_js_twin():
    """JS 側だけに在るキーが、既知の 4 つより増えていないこと。

    増えていたら「心拍に項目を足したが Python 写しに足していない」= Codex の
    受信ループだけが古い形を送り続ける状態。e-7235 で直した「古い情報が権威あるものに
    見える」が Codex 側だけで再発する形なので、ここで止める。
    """
    js = _js_emittable_keys()
    py = _py_emittable_keys()
    js_only = js - py
    assert js_only <= _KNOWN_JS_ONLY, (
        "JS にだけ在るキーが増えている: "
        f"{sorted(js_only - _KNOWN_JS_ONLY)}。"
        "lib/bus_protocol.py::heartbeat_body にも同時に足すこと "
        "(docstring が『同時に増やすこと』と宣言している)。"
        "意図的に JS 限定にするなら _KNOWN_JS_ONLY に理由つきで加える")


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境")
def test_known_js_only_gap_has_not_silently_grown_or_gone_stale():
    """既知の欠落の宣言が、実物とずれていないこと (両方向)。

    宣言だけ増やして実物が直っていない / 実物が直ったのに宣言が残っている、
    のどちらも「宣言と中身の食い違い」なので赤にする。許可リストを隠れ蓑に
    しないための歯止め。
    """
    js, py = _js_emittable_keys(), _py_emittable_keys()
    stale = _KNOWN_JS_ONLY - (js - py)
    assert not stale, (
        f"既知の欠落として宣言されているが実際は両方に在る: {sorted(stale)}。"
        "_KNOWN_JS_ONLY から削ること (直ったものを債務として残さない)")


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境")
def test_where_is_wired_on_both_sides():
    """e-7235 で足した『どこで作業しているか』が両実装に在ること。

    片方だけに足すと、Codex の受信ループが古い cwd / branch を運用室に出し続ける。
    """
    js, py = _js_emittable_keys(), _py_emittable_keys()
    for key in ("cwd", "git"):
        assert key in js, f"JS 側に {key} が無い"
        assert key in py, f"Python 側に {key} が無い (Codex 経路が追従しない)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境")
def test_both_sides_withhold_an_incomplete_git_map():
    """3 項目揃わない git は、どちらの実装も載せないこと。

    サーバの upsert はどの保存層でも浅いマージで入れ子のマップを丸ごと置換するため、
    部分的な map を送ると以前の正しい値ごと消える。HEAD が branch を指していない間
    (rebase 中 / コミット指定の worktree) に起きる。片方だけ据え置くと、同じ心拍が
    クライアントによって違う結果を生む。
    """
    node = shutil.which("node")
    out = subprocess.run(
        [node, "--input-type=module", "-e",
         "import {buildHeartbeatBody} from './channel/bus-heartbeat.mjs';"
         "const b = buildHeartbeatBody({nowIso:'t', pollIntervalMs:1,"
         " where:{cwd:'/w', git:{branch:'', head_short:'h', head_subject:'s'}}});"
         "console.log(JSON.stringify(Object.keys(b)));"],
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    js_keys = set(json.loads(out.stdout))
    py_keys = set(bp.heartbeat_body(
        "t", poll_interval_ms=1,
        where={"cwd": "/w",
               "git": {"branch": "", "head_short": "h", "head_subject": "s"}}))
    assert "git" not in js_keys, "JS 側が不完全な git を載せている"
    assert "git" not in py_keys, "Python 側が不完全な git を載せている"
    # cwd は top-level なので浅いマージで安全に更新でき、据え置く理由が無い。
    assert "cwd" in js_keys and "cwd" in py_keys
