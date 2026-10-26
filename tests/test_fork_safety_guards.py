"""fork 安全性に関する 2 つのガードが効いていることを測る。

背景 (2026-10-26 の実害): `pytest tests/ -n 4` を 2 本並走させた 8 分間に、
macOS が Python のクラッシュレポートを 25 件書いた。全件同じ指紋で、

    CoreFoundation: *** multi-threaded process forked ***
    libsystem_c:    crashed on child side of fork pre-exec
    grpc WorkStealingThreadPool → PollingResolver → ares_init_sysconfig_macos
      → dns_configuration_copy → os_log → xpc → _xpc_strict_close → SIGTRAP

= grpc のチャネルを持った worker が subprocess を起こし、fork() 直後 exec() 前の
子で grpc の atfork ハンドラが DNS 解決を再開して XPC を触った、という経路。
子は exec に到達しないので subprocess は実行されず、親は returncode -5 と空の
stdout を受け取る (= 理由不明の FAILED)。

ガードは 2 段:
  (1) GRPC_ENABLE_FORK_SUPPORT=0 — fork ハンドラ自体を発火させない
  (2) 本物の firestore.Client() 生成の禁止 — 生きた grpc チャネルを作らせない

どちらも conftest.py に置かれていて「忘れたら素通り」ではなく既定で効く。
この 2 本はその既定が外れたことに気づくためのテスト。
"""

import os
import subprocess
import sys

import pytest


def test_grpc_fork_support_is_disabled_for_the_suite():
    """(1) grpc の fork ハンドラが切れていること。

    切れていないと、grpc チャネルを持つ worker が subprocess を起こした瞬間に
    fork 後の子で macOS の fork 安全性違反が起き、間欠的にテストが落ちる。
    """
    assert os.environ.get("GRPC_ENABLE_FORK_SUPPORT") == "0"


def test_grpc_fork_support_propagates_to_subprocess_children():
    """(1-b) env が subprocess の子にも渡ること。

    テストの多くは `bin/beacon` を subprocess で起こす。その子がさらに grpc を
    使う経路に入っても同じガードが効いている必要があるため、env の伝播まで測る。
    """
    r = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ.get('GRPC_ENABLE_FORK_SUPPORT'))"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0"


def test_real_firestore_client_is_refused():
    """(2) 本物の firestore.Client() がテスト内で生成できないこと。

    stub の差し込み忘れが「実 Firestore への接続」に化けるのを、お願いではなく
    例外で閉じている。google-cloud-firestore 未インストール環境では守る対象が
    無いので skip。
    """
    firestore = pytest.importorskip("google.cloud.firestore")
    with pytest.raises(AssertionError) as ei:
        firestore.Client()
    assert "firestore.Client()" in str(ei.value)


@pytest.mark.allow_real_firestore
def test_opt_in_marker_lifts_the_firestore_guard():
    """(2-b) 実接続が主題のテストは marker で明示的に opt-in できること。

    ガードが「外せない」と、実接続を検証する正当なテストが書けなくなる。逃げ道は
    marker 1 つに限定し、--strict-markers で綴り間違いをエラーにしてある。
    """
    firestore = pytest.importorskip("google.cloud.firestore")
    # marker が効いていれば差し替えは起きていない = 本物のクラスがそのまま見える。
    # ここで実際に Client() を呼ぶと実接続になるので、差し替えの不在だけを測る。
    assert getattr(firestore.Client, "__name__", "") != "_refuse"
