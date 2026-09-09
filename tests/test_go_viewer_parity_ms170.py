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
    # 以下は Go 版だけが持つ欄なので、突き合わせからは外す
    # (それぞれの中身は専用の試験で確かめている)。
    #
    # unsupported    … 投影できなかった対象クラスの通知
    # local_sessions … このマシンで観測したセッション (ms-171)。Python 版は
    #                  この観測を行わないため、盤の共有契約には含めない。
    board.pop("unsupported", None)
    board.pop("local_sessions", None)
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


# --- プロジェクトが決まっていない状態 (2026-09-09 の要望) -------------------

@needs_go
def test_starts_and_offers_a_choice_when_no_project_is_found():
    """データが無い場所で起動しても、止まらずに選ばせること。

    実行ファイルを配ってもらった人にとって、起動場所に .beacon が無いのは異常では
    なく普通のこと。「見つかりません」で終わるのではなく、その場で場所を選べる
    状態にする。
    """
    import socket
    import time
    import urllib.error
    import urllib.request

    tmp = tempfile.mkdtemp()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [BINARY, "--path", tmp, "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):  # 立ち上がるのを待つ
            try:
                urllib.request.urlopen(base + "/api/state", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        state = json.loads(
            urllib.request.urlopen(base + "/api/state", timeout=5).read())
        assert state["has_project"] is False
        assert state["cwd"], "どこを探したのかが伝わらない"
        # クラウドから開く経路が使えること (e-6364 で対応済)。
        # 使えない入口を黙って並べない、という規則は変わらない。使えなくなった
        # 場合は cloud_reason で理由を伝えること。
        assert state["cloud_available"] is True
        # ログイン済みかどうかが分かること (画面でログインを促すかの判断に要る)。
        assert "cloud_signed_in" in state

        # 盤はまだ出せないが、理由は返す。
        try:
            urllib.request.urlopen(base + "/api/board", timeout=5)
            raise AssertionError("プロジェクト未選択なのに盤が返っている")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert "選ばれていません" in e.read().decode("utf-8")

        # 場所を渡すと、起動し直さずに開けること。
        body = json.dumps({"path": os.path.abspath(REPO)}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/open", data=body,
            headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())["ok"]

        board = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=10).read())
        assert board["targets"], "開いた後も盤が空のまま"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


@needs_go
def test_bad_path_is_refused_with_a_reason():
    """開けない場所を渡されたら、理由を返して元の状態を壊さないこと。"""
    import socket
    import time
    import urllib.error
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [BINARY, "--path", REPO, "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/board", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        body = json.dumps({"path": "Z:/存在しない場所"}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/open", data=body,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("開けない場所が受け入れられている")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "見つかりません" in e.read().decode("utf-8")

        # 元のプロジェクトは差し替わっていない。
        board = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=10).read())
        assert board["targets"], "失敗した切り替えで元の盤が壊れている"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@needs_go
def test_can_switch_projects_while_one_is_open():
    """開いている最中でも、別のプロジェクトに切り替えられること。

    最初に見た場所に縛られると、複数のプロジェクトを見たい人が起動し直すことに
    なる (2026-09-09 の要望)。
    """
    import socket
    import time
    import urllib.request

    import store_local
    import store_sqlite

    # 切り替え先として、別の場所に小さなプロジェクトを 1 つ作る。
    other = tempfile.mkdtemp()
    beacon_dir = os.path.join(other, ".beacon")
    os.makedirs(beacon_dir)
    with open(os.path.join(beacon_dir, "project.json"), "w", encoding="utf-8") as f:
        json.dump({
            "name": "別のプロジェクト", "objective": "切り替えの確認",
            "profession": "dev",
            "milestones": [{"id": "ms-1", "label": "最初の一歩",
                            "status": "in_progress", "entries": []}],
        }, f, ensure_ascii=False)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [BINARY, "--path", REPO, "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/board", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        first = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=10).read())
        assert first["project"]["name"] == "Beacon"

        # いま何を開いているかが分かること (切り替え画面で見せるのに要る)。
        state = json.loads(
            urllib.request.urlopen(base + "/api/state", timeout=5).read())
        assert state["has_project"] is True
        assert state["path"], "開いている場所が分からない"

        # 切り替える。
        body = json.dumps({"path": other}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/open", data=body,
            headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())["ok"]

        second = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=10).read())
        assert second["project"]["name"] == "別のプロジェクト"
        assert [t["id"] for t in second["targets"]] == ["ms-1"]

        # 元へも戻せること (行き止まりにしない)。
        body = json.dumps({"path": os.path.abspath(REPO)}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/open", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        back = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=10).read())
        assert back["project"]["name"] == "Beacon"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(other, ignore_errors=True)


@needs_go
def test_local_sessions_survive_switching_to_cloud():
    """クラウドに切り替えても、このマシンのセッションが消えないこと。

    名乗っているセッションの名簿と、このマシンで動いているセッションは別物で、
    後者は取得元と関係なく「この機械で何が動いているか」を表す。切り替えた瞬間に
    消えると「自分のセッションが居なくなった」ように見える (2026-09-10 の指摘)。

    クラウドに繋げない環境では確かめようがないので、その場合は飛ばす。
    """
    import socket
    import time
    import urllib.error
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [BINARY, "--path", REPO, "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/board", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        local = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=30).read())
        if not local.get("local_sessions"):
            pytest.skip("このマシンで観測できるセッションが無い環境")

        # クラウドに繋げるか (未ログイン / 不通なら確かめようがない)。
        try:
            projects = json.loads(urllib.request.urlopen(
                base + "/api/cloud/projects", timeout=30).read())
        except urllib.error.HTTPError:
            pytest.skip("クラウドにログインしていない環境")
        except Exception:
            pytest.skip("クラウドに繋げない環境")
        if not projects:
            pytest.skip("参加しているクラウドのプロジェクトが無い")

        body = json.dumps({"project_id": projects[0]["id"]}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/cloud/open", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=120)

        cloud = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=120).read())
        assert cloud["source"]["kind"] == "cloud"
        assert cloud.get("local_sessions"),             "クラウドに切り替えたら、このマシンのセッションが消えた"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@needs_go
def test_roster_is_fetched_for_a_cloud_linked_local_project():
    """ローカルで開いていても、クラウドに結び付いていれば名簿が出ること。

    盤の中身を手元から読んでいることと、名簿の在り処は別の話。ここを取りに
    行かないと「他の人のセッションが出てこない」ように見える (2026-09-10 の指摘)。

    このリポジトリはクラウドに結び付いているので、ログイン済みなら名簿が取れる。
    繋げない環境では確かめようがないので飛ばす。
    """
    import socket
    import time
    import urllib.error
    import urllib.request

    cloud_json = os.path.join(REPO, ".beacon", "cloud.json")
    if not os.path.exists(cloud_json):
        pytest.skip("クラウドに結び付いていないプロジェクト")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [BINARY, "--path", REPO, "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/board", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        # ログインしていない環境では名簿が取れないので、そこは確かめない。
        try:
            urllib.request.urlopen(base + "/api/cloud/projects", timeout=30)
        except Exception:
            pytest.skip("クラウドにログインしていない環境")

        board = json.loads(
            urllib.request.urlopen(base + "/api/board", timeout=60).read())
        # 盤そのものは手元から読んでいる。
        assert board["source"]["kind"] == "local"
        # それでも名簿は取りに行く欄が在る (誰も稼働していなければ空でよい)。
        assert "sessions" in board
    finally:
        proc.terminate()
        proc.wait(timeout=10)
