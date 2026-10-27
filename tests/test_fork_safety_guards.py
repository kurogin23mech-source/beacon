"""fork 安全性に関する 2 つのガードが効いていることを測る。

要約: grpc のチャネルを抱えた worker が別プロセスを起こすと、複製 (fork) 直後・
置き換え (exec) 前の子で grpc が macOS の禁じた経路に触り、子が強制終了する。
その結果「起こしたはずのプロセスが一度も動かないまま、理由不明の失敗になる」。

**根本原因の説明は tests/conftest.py の GRPC_ENABLE_FORK_SUPPORT のコメントが
真実源**。ここに同じ説明を書き写すと、診断が更新されたとき片方だけ古くなる。

ガードは 2 段 (どちらも conftest.py にあり、既定で効く):
  (1) GRPC_ENABLE_FORK_SUPPORT=0 — 複製時のハンドラ自体を発火させない
  (2) 本物の firestore.Client() 生成の禁止 — 生きたチャネルを作らせない

この 2 本はその既定が外れたことに気づくためのテスト。
"""

import os
import subprocess
import sys

import pytest


def test_guard_runs_before_grpc_is_imported():
    """(1-a) 設定がどの grpc の読み込みより先に置かれていること。

    grpc は起動時に一度だけこの設定を読む。先に grpc を読み込む plugin が入ったり
    conftest の並びが変わったりすると、設定が後追いになって黙って効かなくなる。
    conftest は設定の直前に「その時点で grpc が読み込まれていたか」を記録している
    ので、ここではその事実を固定する (コメントの約束ではなく測った値で守る)。
    """
    import conftest

    assert conftest._GRPC_IMPORTED_BEFORE_GUARD is False


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


def test_real_firestore_async_client_is_also_refused():
    """(2-c) 非同期版 (AsyncClient) も同じく拒否されること。

    片方だけ塞ぐと「非同期版に書き換えたらガードが外れる」経路が残り、塞いだ
    つもりの穴が別の名前で開く。いまのリポに AsyncClient の呼び出しは無いが、
    ガードの対象であることをここで固定する。
    """
    firestore = pytest.importorskip("google.cloud.firestore")
    if not hasattr(firestore, "AsyncClient"):
        pytest.skip("この版の google-cloud-firestore に AsyncClient が無い")
    with pytest.raises(AssertionError):
        firestore.AsyncClient()
