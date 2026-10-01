"""ms-173 / e-6774 — 止まった「作業中」を訂正する。待ち状態は訂正しない。

実害 (2026-10-01 実測 2 行): live なのに `running` の `declared_at` が 38 分止まった行が
あった。いずれも待機内容に許可要求の文面が残っており、許可待ちで止まっているのに運用室
では「作業中」に見えていた。`derive_state` は「live な宣言は経年で疑わない」設計で、
docstring 自身がこの live-stuck 検出を後回しと認めていた。

塞げる理由は **非対称性**:

  * `awaiting_human` / `blocked` / `idle` — 静止が正常。待っている間は hook が発火しない
    ので declared_at は当然凍る。経年で疑うと「長く待っている行」= この MS が surface
    したい対象そのものを隠してしまう。
  * `running` — **継続的な主張**。本当に動いていれば PreToolUse / PostToolUse が次々に
    発火して declared_at を進める。進まないなら「作業中」はもう本当ではない。

だから待ち状態を隠すリスクを負わずに running だけを訂正できる。

固定する契約:

  * live + running + 宣言が閾値より古い ⇒ `unknown` (= 生きているが何をしているか言えない)。
    `idle` や `awaiting_human` と断定しない (でっち上げない)。
  * 待ち状態は **どれだけ古くても** 投影が変わらない (ここが退行したら本題が壊れる)。
  * 閾値未指定 (None) / 0 以下 なら従来どおり (既存の呼び出しを壊さない)。
  * not-live 経路は不変 (従来の post-death grace window のまま)。
"""

from __future__ import annotations

import datetime

import pytest

import bus_liveness

WINDOW = 300            # 既存の post-death grace
RUNNING_LIMIT = 1800    # e-6774 の閾値 (30 分)


def _now():
    return datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _ago(seconds):
    return (_now() - datetime.timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z")


def _derive(declared, age_s, live=True, limit=RUNNING_LIMIT):
    return bus_liveness.derive_state(
        declared, _ago(age_s), live, _now(), WINDOW,
        running_stale_after_seconds=limit)


class TestStuckRunningIsCorrected:
    def test_observed_stuck_row_becomes_unknown(self):
        """実測された形: live / running / 38 分静止。"""
        assert _derive("running", 2308) == bus_liveness.STATE_UNKNOWN

    def test_fresh_running_stays_running(self):
        assert _derive("running", 10) == bus_liveness.STATE_RUNNING

    def test_boundary_is_not_corrected(self):
        """境界ちょうどは訂正しない (疑う側に倒さない)。"""
        assert _derive("running", RUNNING_LIMIT) == bus_liveness.STATE_RUNNING
        assert _derive("running", RUNNING_LIMIT + 1) == bus_liveness.STATE_UNKNOWN

    def test_long_single_tool_call_is_not_corrected(self):
        """PreToolUse と PostToolUse の間は declared_at が凍る。手元の最長実測
        (テスト全走 3.5 分) や CI 待ちを unknown にしてはならない。"""
        for seconds in (210, 600, 1200):
            assert _derive("running", seconds) == bus_liveness.STATE_RUNNING, seconds


class TestWaitStatesAreNeverAgedOut:
    """ここが退行すると、この MS が surface したい「長く待っている行」が消える。"""

    @pytest.mark.parametrize("state", ["awaiting_human", "blocked", "idle"])
    @pytest.mark.parametrize("age", [2308, 86400, 30 * 86400])
    def test_non_running_declarations_are_untouched(self, state, age):
        assert _derive(state, age) == state

    def test_terminated_stays_terminal(self):
        assert _derive("terminated", 30 * 86400) == bus_liveness.STATE_TERMINATED


class TestOptIn:
    @pytest.mark.parametrize("limit", [None, 0, -1])
    def test_disabled_limit_keeps_the_old_behaviour(self, limit):
        assert _derive("running", 2308, limit=limit) == bus_liveness.STATE_RUNNING

    def test_existing_positional_callers_are_unaffected(self):
        """新しい引数は keyword-only の既定 None なので、旧呼び出しは不変。"""
        assert bus_liveness.derive_state(
            "running", _ago(2308), True, _now(), WINDOW) \
            == bus_liveness.STATE_RUNNING


class TestNotLivePathUnchanged:
    def test_stale_and_not_live_is_still_interrupted(self):
        assert _derive("running", 2308, live=False) == bus_liveness.STATE_INTERRUPTED

    def test_fresh_and_not_live_still_trusts_the_declaration(self):
        assert _derive("running", 10, live=False) == bus_liveness.STATE_RUNNING


class TestServerWiring:
    @classmethod
    def setup_class(cls):
        from pathlib import Path
        cls.src = (Path(bus_liveness.__file__).resolve().parents[1]
                   / "server" / "app.py").read_text(encoding="utf-8")

    def test_server_passes_the_threshold(self):
        i = self.src.index("bus_liveness.derive_state(")
        block = self.src[i:i + 400]
        assert "running_stale_after_seconds=" in block, (
            "server が閾値を渡していない = 止まった作業中が訂正されない")

    def test_threshold_is_generous_enough_for_a_long_tool_call(self):
        """短い既定値は正常な長時間作業を unknown にする。"""
        import re
        m = re.search(r'BEACON_RUNNING_DECL_STALE_AGE_S", "(\d+)"', self.src)
        assert m, "閾値の既定が読めない"
        assert int(m.group(1)) >= 900, (
            "既定が短すぎる — 長いテスト実行や CI 待ちを「止まった」と誤判定する")
