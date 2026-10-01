"""ms-173 独立レビュー (AX-3 / 保守性 M-3) — fail-open な閾値の網羅を構造で固定する。

`<=0` でガードが無効化される (= fail-open) 閾値は、無効化されたことが起動時に見えなければ
「設定したのに何も起きない」silent no-op になる。警告ループへ手で追記する運用だと必ず
忘れる — 実際 `_RUNNING_DECL_STALE_AGE_S` を足したとき忘れ、独立レビューに指摘された。

固定する契約: **fail-open な閾値は `_FAIL_OPEN_THRESHOLDS` に登録されていなければならない。**
新しい閾値を足して登録を忘れたら、このテストが落ちる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "server" / "app.py"

#: fail-open 契約を持つ閾値の env 名。新設したらここと `_FAIL_OPEN_THRESHOLDS` の両方へ。
EXPECTED = {
    "BEACON_WS_ZOMBIE_POLL_AGE_S",
    "BEACON_WS_ZOMBIE_NO_HISTORY_AGE_S",
    "BEACON_RUNNING_DECL_STALE_AGE_S",
}


@pytest.fixture(scope="module")
def src() -> str:
    return APP.read_text(encoding="utf-8")


def _registered(src: str) -> set:
    """`_FAIL_OPEN_THRESHOLDS` に登録されている env 名を構造的に抜く。

    緩い substring 一致にしないのが要 (コメントや別の文脈の同名文字列で false-pass
    する)。タプル定義の領域を切り出してから名前を拾う。
    """
    i = src.index("_FAIL_OPEN_THRESHOLDS = (")
    block = src[i:src.index(")\n", i)]
    return set(re.findall(r'\("([A-Z_]+)",', block))


class TestFailOpenCoverage:
    def test_registry_exists(self, src):
        assert "_FAIL_OPEN_THRESHOLDS = (" in src, (
            "fail-open 閾値の登録先が無い — 手で警告ループに足す運用に戻っている")

    def test_every_expected_threshold_is_registered(self, src):
        missing = EXPECTED - _registered(src)
        assert not missing, (
            f"fail-open 閾値 {sorted(missing)} が未登録 — 0 以下にしても起動時に警告が出ず、"
            "ガードが黙って無効化される")

    def test_no_unexpected_registration(self, src):
        """登録側だけ増えてこの表が古くなるのも防ぐ (双方向)。"""
        extra = _registered(src) - EXPECTED
        assert not extra, (
            f"{sorted(extra)} が登録されているが EXPECTED に無い — 本テストの表を更新せよ")

    def test_warning_loop_iterates_the_registry(self, src):
        """ループが登録表を回していること (別のタプルを回していたら網羅が嘘になる)。"""
        assert "for _name, _val in _FAIL_OPEN_THRESHOLDS:" in src

    def test_each_registered_threshold_is_actually_fail_open(self, src):
        """登録された閾値が実際に `<=0` で無効化される形であること。
        そうでないものを登録すると、警告の意味が薄まる。"""
        for name in _registered(src):
            m = re.search(rf'os\.environ\.get\("{name}", "(\d+)"\)', src)
            assert m, f"{name} の既定値が読めない (env から読む形になっていない)"
            assert int(m.group(1)) > 0, f"{name} の既定が 0 以下 = 既定で無効"
