"""セッションの識別子が、1 セッションの間ずっと同じ値であること (ms-178 / e-7150).

サーバは ``(project_id, machine_id, parent_pid)`` を ``{machine_id}_{parent_pid}``
という lookup key にして session_id を引く (``server/firestore_client.py``)。
つまり ``_resolve_parent_pid()`` の戻り値は「ヒント」ではなく **そのセッションの
身元そのもの**で、呼び出しごとに変わると呼び出しごとに別セッションが生まれる。

その状態が実際に起きていた。``os.getppid()`` を最後の砦にしていたが、CLI 1 回分の
直接の親は harness が毎回作り直す短命なシェルなので、呼ぶたびに別値になる
(2026-10-25 実測: 同一セッション内の 2 回の Bash 呼び出しで 35125 → 36292)。
結果、1 つのセッションが書いた 5 件のメモが 5 つの別 session_id を持ち、各 sid に
埋まった時刻はそのメモ自身の書き込み時刻と一致していた。``beacon note clear`` は
「session_id が自分と一致するメモ」を探すので、自分が書いたものを 1 件も見つけ
られなくなり、残る手段は他セッションの引き継ぎメモまで消す
``--include-other-sessions`` だけになった (2026-10-01 以降 180 件が蓄積、うち 97%
が孤児)。

このモジュールは「安定していること」だけでなく **壊れたときに赤くなること** を
確かめる: getppid を呼び出しごとに別値へ差し替えても錨が動かないことを見る。
これをやらないと、getppid に戻しても緑のままのテストになる。
"""

from __future__ import annotations

import itertools
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import session as S  # noqa: E402

SID_A = "852017d9-bdb9-4b72-b432-b66d2e06cc79"
SID_B = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def churning_ppid(monkeypatch):
    """os.getppid を「呼ぶたび別値」にする = 実測した harness の挙動そのもの。

    錨がこの値を一切見ていないことを示すための仕掛け。見ていたら下の
    stability テストが落ちる。
    """
    counter = itertools.count(40000)
    monkeypatch.setattr(S.os, "getppid", lambda: next(counter))
    return counter


def test_env_parent_pid_wins(monkeypatch, churning_ppid):
    """bclaude 経由なら従来どおり BEACON_PARENT_PID がそのまま身元になる。"""
    monkeypatch.setenv(S.ENV_PARENT_PID, "12099")
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_A)
    assert [S._resolve_parent_pid() for _ in range(5)] == [12099] * 5


def test_anchor_is_stable_across_calls(monkeypatch, churning_ppid):
    """bclaude を経由しないセッションでも、呼び出しをまたいで同じ値になる。

    これが AC1 (自分が書いたメモを自分で消せる) の前提。getppid に依存して
    いた旧実装はここで 5 つの別値を返す。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_A)
    values = [S._resolve_parent_pid() for _ in range(5)]
    assert len(set(values)) == 1, values
    # 錨が churning な getppid を一切見ていないことの直接確認。
    assert values[0] not in range(40000, 40010)


def test_anchor_discriminates_between_sessions(monkeypatch):
    """別セッションは別の身元になる (= 取り違えて相乗りしない)。

    安定だけを確かめると「定数を返す」実装でも緑になるので、識別できることを
    対で固定する。

    ここは ``churning_ppid`` を **使わない**。使うと getppid 由来の旧実装でも
    「2 回の値が違う」ので緑になり、反転ハーネスで検出できない偽の緑になる
    (実際に一度そうなった)。getppid を固定した上で session id だけを動かし、
    差が session id から来ていることを示す。
    """
    monkeypatch.setattr(S.os, "getppid", lambda: 40000)  # 固定
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_A)
    a = S._resolve_parent_pid()
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_B)
    b = S._resolve_parent_pid()
    assert a != b, "session id を変えても身元が変わらない (識別できていない)"


def test_anchor_cannot_collide_with_a_real_pid(monkeypatch, churning_ppid):
    """錨は実 pid の範囲に降りてこない。

    降りてくると、同じマシンの「pid 由来の tuple」と衝突して 2 つの別セッション
    が同じ session_id を共有しうる。範囲を構造で分けておく。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    for sid in (SID_A, SID_B, "x", "0", "a" * 64):
        monkeypatch.setenv(S.ENV_SESSION_ID, sid)
        anchor = S._resolve_parent_pid()
        assert anchor > 0x40000000, (sid, anchor)
        assert anchor <= 0x7FFFFFFF, (sid, anchor)


def test_unresolvable_returns_zero_not_a_guess(monkeypatch, churning_ppid):
    """身元の手がかりが無いときは 0 (= 解決不能) を返し、推測しない。

    「とりあえず非ゼロ」を返すのが e-7150 の defect だった。間違った非ゼロは
    無い方より高い: 無ければ操作が止まってそう言うが、間違っていると静かに
    別セッションを作る / 別セッションの記録に混ざる。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.delenv(S.ENV_SESSION_ID, raising=False)
    assert S._resolve_parent_pid() == 0


def test_blank_env_values_fall_through(monkeypatch, churning_ppid):
    """空文字 / 空白のみ / 非数値は「設定されていない」と同じ扱い。"""
    monkeypatch.setenv(S.ENV_PARENT_PID, "   ")
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_A)
    anchor_from_sid = S._resolve_parent_pid()

    monkeypatch.setenv(S.ENV_PARENT_PID, "not-a-number")
    assert S._resolve_parent_pid() == anchor_from_sid

    monkeypatch.setenv(S.ENV_PARENT_PID, "0")
    assert S._resolve_parent_pid() == anchor_from_sid

    monkeypatch.setenv(S.ENV_PARENT_PID, "-5")
    assert S._resolve_parent_pid() == anchor_from_sid


def test_cloud_mint_refuses_when_identity_unresolvable(
        monkeypatch, churning_ppid, fake_cloud_config):
    """身元が解けないとき、cloud 経路は mint せずに失敗する。

    ここが「0 を返す」設計の出口。0 のまま mint を通すと、サーバに二度と引け
    ない (lookup key が毎回違う) セッションを量産する。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.delenv(S.ENV_SESSION_ID, raising=False)
    monkeypatch.chdir(fake_cloud_config.parent)

    def _must_not_call(*a, **k):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("身元が解けないのにサーバへ mint を要求した")

    monkeypatch.setattr(S, "_read_machine_cache", lambda: {})
    monkeypatch.setattr(S, "_write_machine_cache", _must_not_call)

    with pytest.raises(RuntimeError, match="parent_pid"):
        S._get_or_mint_session_via_server_impl()


def test_stable_anchor_makes_the_server_lookup_key_stable(
        monkeypatch, churning_ppid):
    """同一セッションの連続呼び出しが、サーバの同じ lookup key に落ちる。

    サーバ側の key は ``{machine_id}_{parent_pid}`` なので、ここが同じであれば
    「同じ tuple → 同じ session_id」が成立する = sid が churn しない。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.setenv(S.ENV_SESSION_ID, SID_A)
    keys = {"mc-1_%d" % S._resolve_parent_pid() for _ in range(5)}
    assert len(keys) == 1, keys
