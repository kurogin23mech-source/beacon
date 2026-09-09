"""Go 版ビューワーと Python 版が同じ盤を出すことの突き合わせ (ms-170 e-6362)。

なぜ要るか
----------
「Beacon が入っていない PC でも見られる」ために、盤の読み取りを Python と Go で
二重に持つことにした (SPEC KIGmqQTIanqUypbtmrTs の 2026-09-09 改訂)。二重化を選んだ
以上、**両者がズレていないことを機械で確かめ続ける**のが唯一の歯止めになる。

実際、この突き合わせは書いている最中に 3 件のズレを捕まえた:

1. SQLite の種別名を大文字だと思い込んでいた (実際は小文字)。エラーは出ず、
   ただ「対象 0 件」と表示された — 気づきにくい壊れ方の典型。
2. 元データの ``target_date`` が null の対象で、Go が空文字に丸めていた。
3. SQLite から組み立て直すときの並び順が文字列順になっており、ms-1 の次が ms-10
   になっていた (読む先によって盤の並びが変わる状態)。

Go が無い環境では丸ごと飛ばす。CI は Python だけで回るので、ここで落ちないこと。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, "lib"))

VIEWER_DIR = os.path.join(REPO, "viewer")
PROJECT_JSON = os.path.join(REPO, ".beacon", "project.json")


def _go_binary():
    """ビルド済みのビューワーを探す。無ければ (Go があれば) その場で建てる。"""
    for name in ("viewer.exe", "viewer"):
        path = os.path.join(VIEWER_DIR, name)
        if os.path.exists(path):
            return path
    go = shutil.which("go")
    if not go:
        return None
    out = os.path.join(VIEWER_DIR, "viewer.exe" if os.name == "nt" else "viewer")
    proc = subprocess.run([go, "build", "-o", out, "./..."],
                          cwd=VIEWER_DIR, capture_output=True)
    return out if proc.returncode == 0 else None


BINARY = _go_binary()

needs_go = pytest.mark.skipif(
    BINARY is None or not os.path.exists(PROJECT_JSON),
    reason="Go 版ビューワー、またはローカルのプロジェクトデータが無い環境",
)


def _go_board(path):
    proc = subprocess.run([BINARY, "--path", path, "--json"],
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    board = json.loads(proc.stdout.decode("utf-8"))
    # ``unsupported`` は Go 版だけが持つ「投影できなかったもの」の通知欄。
    # Python 版には無いので、突き合わせからは外す (存在自体は別の試験で確かめる)。
    board.pop("unsupported", None)
    return board


def _python_board(project_json):
    import store_local
    import view_model
    data = store_local.LocalStore(project_json).load_project()
    return view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)


@needs_go
def test_go_and_python_produce_the_same_board_from_json():
    """同じデータを読ませたら、Go 版と Python 版の盤が 1 項目も違わないこと。"""
    go = _go_board(os.path.join(REPO, "."))
    py = _python_board(PROJECT_JSON)
    assert go == py


@needs_go
def test_go_reads_sqlite_and_json_identically():
    """読む先が SQLite でも JSON でも、出てくる盤が同じであること。

    ms-148 でローカルの真値は SQLite に移ったが、旧い JSON も残る。読む先で盤が
    変わると「どちらが本当か」が分からなくなる。
    """
    import store_local
    import store_sqlite

    tmp = tempfile.mkdtemp()
    try:
        project_json = os.path.join(tmp, "project.json")
        shutil.copy(PROJECT_JSON, project_json)
        data = store_local.LocalStore(project_json).load_project()
        store_sqlite.SqliteStore(project_json).populate_if_empty(data)
        assert os.path.exists(os.path.join(tmp, "project.db")), "SQLite が作られていない"

        from_sqlite = _go_board(tmp)
        from_json = _go_board(os.path.join(REPO, "."))
        assert from_sqlite == from_json
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_go
def test_target_order_is_numeric_not_lexicographic():
    """ms-1 の次は ms-2 であって ms-10 ではないこと。

    組み立て直しで並び順が失われるため、Python と同じ規則で並べ直す必要がある。
    """
    ids = [t["id"] for t in _go_board(os.path.join(REPO, "."))["targets"]]
    numeric = [int(i.split("-")[1]) for i in ids
               if i.startswith("ms-") and i.split("-")[1].isdigit()]
    assert numeric == sorted(numeric), "対象の並びが数の順になっていない"


@needs_go
def test_unsupported_target_classes_are_announced_not_hidden():
    """投影できない対象クラスがあるとき、黙って省略せず件数を伝えること。

    Beacon 本体のプロジェクトは記述子で定義された対象クラスを持つので、ここでは
    必ず通知が出る。出なくなったら、それは silent に省略している状態。
    """
    proc = subprocess.run([BINARY, "--path", os.path.join(REPO, "."), "--json"],
                          capture_output=True)
    board = json.loads(proc.stdout.decode("utf-8"))
    unsupported = board.get("unsupported")
    assert unsupported is not None, "未対応クラスがあるのに通知されていない"
    assert unsupported["target_class_count"] >= 1
    assert unsupported["target_class_names"], "何が未対応なのか名前が出ていない"


@needs_go
def test_viewer_does_not_modify_the_data():
    """ビューワーは読むだけで、Beacon のデータを変えないこと。

    注意: SQLite を読むと ``-wal`` / ``-shm`` という補助ファイルが隣に作られる。
    これは SQLite が読み手にも作るもので、データそのものは変わらない。``immutable=1``
    を付ければ作られなくなるが、その場合 **まだ本体に反映されていない最新の変更が
    見えず、黙って古い盤を表示する**。静かに古いものを見せるほうが有害なので、
    補助ファイルが増えることを受け入れている。

    したがってここで確かめるのは「データのファイルが変わっていないこと」であって、
    「フォルダの中身が 1 つも増えないこと」ではない。
    """
    import store_local
    import store_sqlite

    tmp = tempfile.mkdtemp()
    try:
        project_json = os.path.join(tmp, "project.json")
        shutil.copy(PROJECT_JSON, project_json)
        data = store_local.LocalStore(project_json).load_project()
        store_sqlite.SqliteStore(project_json).populate_if_empty(data)

        data_files = ("project.db", "project.json")

        def snapshot():
            out = {}
            for name in data_files:
                path = os.path.join(tmp, name)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        out[name] = hashlib.sha256(f.read()).hexdigest()
            return out

        before = snapshot()
        _go_board(tmp)
        after = snapshot()

        assert before == after, "ビューワーがデータを書き換えている"
        assert before, "確かめる対象のデータが無い (試験の前提が崩れている)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 受け取った人が「置いて実行するだけ」で動くか (e-6363 の実地修正) -------

@needs_go
def test_finds_beacon_from_a_subdirectory():
    """プロジェクトの奥のフォルダで実行しても、上にさかのぼって見つけること。

    エクスプローラーからダブルクリックすると、起動場所が実行ファイルの置き場所に
    なる。上へ探さないと「見つかりません」で終わり、しかも窓が即座に閉じるので
    利用者は理由すら読めない (2026-09-09 に実際に起きた)。
    """
    proc = subprocess.run([BINARY, "--json"], cwd=VIEWER_DIR, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    board = json.loads(proc.stdout.decode("utf-8"))
    assert board["targets"], "上にさかのぼって .beacon を見つけられていない"


@needs_go
def test_missing_project_explains_what_to_do():
    """見つからないときは、何をすればよいかまで伝えること。"""
    tmp = tempfile.mkdtemp()
    try:
        proc = subprocess.run([BINARY, "--path", tmp, "--json"],
                              capture_output=True)
        assert proc.returncode != 0, "見つからないのに成功として終わっている"
        msg = proc.stderr.decode("utf-8", "replace")
        assert "見つかりません" in msg
        assert "--path" in msg, "次に何をすればよいかが書かれていない"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
