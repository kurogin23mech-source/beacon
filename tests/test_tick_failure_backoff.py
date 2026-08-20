"""発火に失敗した対象を、失敗するほど後ろへ下げる仕組みの単体テスト (e-5366)。

2026-08-20 の本番停止では、trek_id を持たない Trek 3 件が 44 日間 (6 万回以上)
毎分同じ失敗を繰り返し、そのたびにバスへ通知を書いてサーバを落とした。原因は
「発火に成功したときだけ last_fired_at を進める」実装で、失敗が状態を進めない
ため永久に「期限到来」のまま残ったこと。ここではその再発防止の後退ロジックを
検証する。

外部依存なし (dict と時刻計算だけ) なので、DB も subprocess も要らず、どの OS
でも走る。これは意図的: この日、CLI を subprocess 起動するテスト 216 本が
Windows で軒並み OSError になり、開発機でテストが安全網として使えない状態が
判明したため、再発防止のロジックだけは純関数として置く。
"""
import datetime
import os
import sys

THIS = os.path.dirname(__file__)
LIB = os.path.normpath(os.path.join(THIS, "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import tick_scheduler as ts  # noqa: E402

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_失敗記録が無ければ発火を止めない():
    assert ts.failure_backoff_active({}, T0) is False
    assert ts.failure_backoff_active({"fire_failures": 0}, T0) is False


def test_1回目の失敗は1分待つ():
    meta = {}
    assert ts.record_failure(meta, _iso(T0)) == 1
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(seconds=30)) is True
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(minutes=1)) is False


def test_失敗を重ねるほど待ち時間が倍になる():
    meta = {}
    for _ in range(3):
        ts.record_failure(meta, _iso(T0))
    # 3 回失敗 → 2^(3-1) = 4 分
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(minutes=3)) is True
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(minutes=4)) is False


def test_待ち時間には上限があり永久停止にならない():
    meta = {}
    for _ in range(20):        # 2^19 分 = 約 364 時間 になってしまう回数
        ts.record_failure(meta, _iso(T0))
    cap = ts.FAILURE_BACKOFF_CAP_MINUTES
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(minutes=cap - 1)) is True
    assert ts.failure_backoff_active(meta, T0 + datetime.timedelta(minutes=cap)) is False


def test_成功したら数え直しになる():
    meta = {}
    for _ in range(5):
        ts.record_failure(meta, _iso(T0))
    ts.clear_failure(meta)
    assert meta == {}
    assert ts.failure_backoff_active(meta, T0) is False


def test_用途ごとに独立して数える():
    meta = {}
    ts.record_failure(meta, _iso(T0), key="idle")
    assert ts.failure_backoff_active(meta, T0, key="idle") is True
    assert ts.failure_backoff_active(meta, T0, key="fire") is False


def test_記録が壊れていても発火を止めない():
    # 壊れた記録でこの仕組み自体が発火を殺すのは本末転倒なので、
    # 判断できないときは「待たない」に倒す。
    for broken in ({"fire_failures": "abc"},
                   {"fire_failures": 3},                       # 時刻が無い
                   {"fire_failures": 3, "fire_last_failed_at": "not-a-date"},
                   {"fire_failures": 3, "fire_last_failed_at": ""}):
        assert ts.failure_backoff_active(broken, T0) is False


def test_今日の障害そのものを再現する():
    """id を持たない Trek が毎分失敗し続けた形が、後退で止まることを示す。"""
    meta = {}
    now = T0
    fired_attempts = 0
    for _ in range(60):                       # 60 分ぶん、毎分 tick が来る
        if not ts.failure_backoff_active(meta, now):
            fired_attempts += 1
            ts.record_failure(meta, _iso(now))   # 毎回失敗する対象
        now += datetime.timedelta(minutes=1)
    # 従来は 60 回とも試行し、そのたびバスへ書いていた。
    assert fired_attempts <= 8, f"60分で{fired_attempts}回も試行している"
