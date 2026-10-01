"""ms-173 / e-6729 — WS 単独の生存主張を裏付け無しに通さない (server 側の保険)。

e-6583 で真因 (孤児 bridge の ping が生存の証拠にされていた) は bridge 側で直したが、
直したコードが入っていない古い bridge には届かない。e-6563 の先行ガードは
``last_poll_at`` を一度でも書いた行だけを対象にしており、書かない古い bridge や報告を
出さない購読者はガードを素通りして永久に live を主張できた — e-6583 の記述自身が
「poll 履歴を持たない行にはガードが効かない設計なので、そこでリークすると永久ゾンビが
再発しうる」と指摘していた穴。

e-6582 (汚染された『確認待ち』の降格) と違い、ここには曖昧さが無い: 「WS で生存を
主張しているのに生存の痕跡がどれも古い (または一つも無い)」は、本物を隠す恐れのある
推定ではなく嘘の証拠そのもの。

固定する契約:

  * 判定の線引きは「poll 履歴があるか」ではなく「裏付けが何か一つでも新しいか」。
  * 裏付けは互いに独立した複数源から取る。1 源だけだと、その源を持たない正当な行を
    誤爆する (古い bridge は poll 報告を出さないが、人/AI が動かしていれば hook 由来の
    heartbeat が新しい)。
  * 誤って not-live にすると DM が届かなくなるので、判定は常に「救う」側に倒す:
    境界ちょうどは救う / 若いセッションは判定しない / 閾値が不正なら何もしない /
    読めない源は「古い」ではなく「無い」として扱う。
  * e-6563 の既存挙動 (poll 履歴あり + poll stale → ws-zombie-poll-stale) は不変。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import bus_liveness  # noqa: E402

POLL_LIMIT = 1800          # 30 分 (既存 _WS_ZOMBIE_POLL_AGE_S)
NO_HISTORY_LIMIT = 10800   # 3 時間 (新 _WS_ZOMBIE_NO_HISTORY_AGE_S)


def _suppress(ws_live=True, poll_healthy=False, has_history=False,
              ages=(None,), session_age=None):
    return bus_liveness.ws_only_liveness_suppression(
        ws_live, poll_healthy, has_history, ages, POLL_LIMIT,
        max_age_seconds_no_history=NO_HISTORY_LIMIT,
        session_age_seconds=session_age,
    )


# --- この関数が扱う範囲 (WS 単独の主張だけ) ---------------------------------

class TestScope:
    def test_poll_healthy_is_never_touched(self):
        """poll が健全なら生存は WS 単独の主張ではない。触ってはならない。"""
        assert _suppress(poll_healthy=True, ages=(None,), session_age=10**6) is None

    @pytest.mark.parametrize("ws_live", [False, None])
    def test_not_ws_live_is_never_touched(self, ws_live):
        """ws_live が False (台帳に無い) / None (Redis 不通) の行は対象外。
        None を抑止対象にすると、Redis 不通時に全行が not-live に落ちる。"""
        assert _suppress(ws_live=ws_live, ages=(None,), session_age=10**6) is None


# --- 実測されたゾンビを止める -------------------------------------------------

class TestObservedZombie:
    def test_orphan_with_no_poll_history_is_suppressed(self):
        """e-6729 の本体: 履歴が無く痕跡も全部古い行 (= 実測 500 時間) を止める。"""
        assert _suppress(
            has_history=False, ages=(None, 500 * 3600, 500 * 3600),
            session_age=25 * 86400,
        ) == bus_liveness.WS_SUPPRESS_NO_EVIDENCE

    def test_no_evidence_at_all_on_an_old_session_is_suppressed(self):
        """痕跡が 1 つも無い古いセッション = 名乗るだけで何も報告していない。"""
        assert _suppress(
            has_history=False, ages=(None, None, None), session_age=25 * 86400,
        ) == bus_liveness.WS_SUPPRESS_NO_EVIDENCE


# --- e-6563 の既存挙動は不変 (退行ガード) ------------------------------------

class TestExistingGuardPreserved:
    def test_poll_history_stale_keeps_the_original_reason(self):
        """理由文字列も変えない。診断側と UI が既にこの値を読んでいる。"""
        assert _suppress(
            has_history=True, ages=(POLL_LIMIT + 1, None, None),
            session_age=10**6,
        ) == bus_liveness.WS_SUPPRESS_POLL_STALE
        assert bus_liveness.WS_SUPPRESS_POLL_STALE == "ws-zombie-poll-stale"

    def test_poll_history_within_limit_is_saved(self):
        assert _suppress(
            has_history=True, ages=(POLL_LIMIT - 1, None, None), session_age=10**6,
        ) is None


# --- 誤爆しない側に倒す (ここが壊れると DM が届かなくなる) -------------------

class TestFailsTowardSavingTheRow:
    def test_boundary_age_is_saved(self):
        """境界ちょうどは救う側。"""
        assert _suppress(
            has_history=True, ages=(POLL_LIMIT,), session_age=10**6) is None
        assert _suppress(
            has_history=False, ages=(NO_HISTORY_LIMIT,), session_age=10**6) is None

    def test_one_fresh_evidence_source_saves_the_row(self):
        """古い bridge は poll 報告を出さないが、人/AI が動かしていれば hook 由来の
        heartbeat が新しい。1 源でも新しければ救う — ここが古い bridge の安全弁。"""
        assert _suppress(
            has_history=False, ages=(None, 5, 500 * 3600), session_age=25 * 86400,
        ) is None

    def test_young_session_is_not_judged(self):
        """繋いだ直後は、まだ一度も報告していないのが正常。証拠の不在を嘘の証拠に
        しない (入れないと起動直後の数秒だけ not-live に見える窓ができる)。"""
        assert _suppress(
            has_history=False, ages=(None, None, None), session_age=3) is None

    def test_no_history_gets_the_longer_grace(self):
        """履歴が無い行は確信度が低いので猶予が長い。30 分では止めない。
        ゾンビは日単位で居座るので検知力は落ちない。"""
        assert _suppress(
            has_history=False, ages=(None, POLL_LIMIT + 60, None),
            session_age=10**6,
        ) is None
        assert _suppress(
            has_history=False, ages=(None, NO_HISTORY_LIMIT + 60, None),
            session_age=10**6,
        ) == bus_liveness.WS_SUPPRESS_NO_EVIDENCE

    @pytest.mark.parametrize("limit", [None, 0, -1])
    def test_misconfigured_threshold_does_nothing(self, limit):
        """設定ミスで健全な行を黙らせない。"""
        assert bus_liveness.ws_only_liveness_suppression(
            True, False, True, (10**6,), limit,
            max_age_seconds_no_history=limit, session_age_seconds=10**6) is None

    def test_empty_evidence_tuple_and_none_are_handled(self):
        assert _suppress(has_history=True, ages=(), session_age=10**6) \
            == bus_liveness.WS_SUPPRESS_POLL_STALE
        assert _suppress(has_history=True, ages=None, session_age=10**6) \
            == bus_liveness.WS_SUPPRESS_POLL_STALE


# --- 配線: server がこの規則を使っているか ------------------------------------

class TestServerWiring:
    """規則が正しくても server が呼んでいなければ意味が無い。
    消費側ごとに if を書き足す形に戻っていないことも併せて見る。"""

    @pytest.fixture(scope="class")
    def app_src(self) -> str:
        return (REPO_ROOT / "server" / "app.py").read_text(encoding="utf-8")

    def test_server_calls_the_pure_rule(self, app_src):
        assert "bus_liveness.ws_only_liveness_suppression(" in app_src, (
            "server が規則を呼んでいない = 保険が存在しない")

    def test_old_inline_guard_is_gone(self, app_src):
        """e-6563 の inline if が残っていると規則が二重になり、片方だけ直す事故になる。"""
        assert 'session["live_suppressed_reason"] = "ws-zombie-poll-stale"' \
            not in app_src, "inline のガードが残っている (真値源が割れる)"

    def test_server_passes_multiple_evidence_sources(self, app_src):
        """1 源だけ渡すと、その源を持たない正当な行を誤爆する。"""
        call_idx = app_src.index("bus_liveness.ws_only_liveness_suppression(")
        call = app_src[call_idx:call_idx + 900]
        for src in ("poll_health", "last_heartbeat_at", "last_active"):
            assert src in call, f"裏付けの源 {src} を渡していない"

    def test_server_passes_the_no_history_grace_and_session_age(self, app_src):
        call_idx = app_src.index("bus_liveness.ws_only_liveness_suppression(")
        call = app_src[call_idx:call_idx + 900]
        assert "max_age_seconds_no_history=" in call, (
            "履歴なし用の猶予を渡していない = 古い bridge を 30 分で誤爆する")
        assert "session_age_seconds=" in call, (
            "セッション年齢を渡していない = 起動直後に not-live の窓ができる")
