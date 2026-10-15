"""読み取りを名乗る入口が保存ファイルを触らないことを測る (ms-166 e-6937)。

経緯
----
クラウドモードでは ``.beacon/project.json`` は「クラウドの真値の手元ミラー」で、
:meth:`store_api.StoreApi.load_project` が毎回これを書き戻していた。結果として
**読み取りしかしない入口が必ずファイルを触っていた**。実測 (2026-10-15、本番の
beacon プロジェクトで mtime を前後比較):

====================================  ========
``beacon status --json``              書く ⚠
``beacon task list --ms ms-166``      書く ⚠
``beacon task list`` (``--ms`` なし)   書く ⚠ (+ exit 1)
``beacon pr sync --dry-run --json``   書く ⚠
``beacon note list --json``           書かない
``beacon doc list --scope core``      書かない
====================================  ========

note / doc の一覧が書かないのは、それらが子コレクションの口を読んでいて project
文書 (``load_project``) を通らないため。つまり「クラウドでは読み取りがミラーを
更新する」という一律の挙動であり、特定の 2 コマンドの癖ではなかった (起票時の
記録はここを取り違えていた)。

実害は 2 つ:

1. ``beacon pr sync --dry-run`` は「下見であって何も変えない」ことを **名前で**
   約束しているのに、保存ファイルの更新時刻が動く。レビューの原典が misleading と
   呼ぶ型 (= 名乗りと実態のズレが利用者を誤診させる) そのもの。
2. 原子的書き込みの途中半分 (``project.json.tmp``) が、全件テスト中に
   「テストがゴミを残した」と誤報される窓を作る。

対になる事実として、ローカル側の :class:`store_sqlite.SqliteStore` はミラーを
**保存のときだけ** 書く (読みでは書かない)。クラウド側だけが非対称だった。

この module が固定する契約
--------------------------
* 手元のミラーが既にクラウドと同じ内容なら、読み取りは **一時ファイルも作らず**
  ファイルを一切触らない (更新時刻も inode も動かない)。
* 読み取りの最中に実際に古かったときだけ追従し、そのときは **なぜ触ったのかを
  stderr で述べる** (``--json`` を機械で読む側の stdout は汚さない)。
* ミラーを更新する全ての呼び出し口が **立場 (read / save) を明示する**。既定値を
  置かないので、新しい口が「黙って更新してよい」側の挙動を相続できない。

空振り防止: 「書かない」試験だけだと、ミラー経路が丸ごと死んでも緑になる。
初回 (= ミラー不在) では必ず書くことを同じ module で測り、両側を固定する。
"""

import ast
import copy
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import store_api as store_api_mod
from store_api import (MIRROR_ORIGIN_READ, MIRROR_ORIGIN_SAVE,
                       MIRROR_REASON_CREATED, MIRROR_REASON_FAILED,
                       MIRROR_REASON_REFRESHED, StoreApi)

LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
STORE_API_PATH = os.path.join(LIB, "store_api.py")


class FakeClient:
    """ApiClient の代役。HTTP の境界を真似て毎回深いコピーを返す。"""

    def __init__(self, doc):
        self._doc = copy.deepcopy(doc)
        self.get_calls = 0

    def get_project(self, project_id):
        self.get_calls += 1
        return copy.deepcopy(self._doc)

    def put_project(self, project_id, data):
        self._doc = copy.deepcopy(data)
        return {"status": "ok"}

    # テスト側から「別の端末がクラウドを書き換えた」を起こす口。
    def remote_write(self, doc):
        self._doc = copy.deepcopy(doc)


def _make_store(doc, cache_path):
    s = StoreApi("http://test", "pid", "", mirror_path=str(cache_path))
    s._client = FakeClient(doc)
    StoreApi._load_baseline.pop("pid", None)
    return s


DOC = {"name": "p", "summary": "v1", "milestones": [{"id": "ms-1", "title": "t"}]}


def _stat(path):
    st = os.stat(str(path))
    return (st.st_mtime_ns, st.st_ino, st.st_size)


# --------------------------------------------------------------------------
# 空振り防止 — ミラー経路が生きていることを先に固定する
# --------------------------------------------------------------------------

def test_the_first_read_still_writes_the_mirror(tmp_path):
    """ミラー不在からの初回読み取りは書く (e-746 の約束はそのまま)。

    この試験が無いと、以下の「触らない」試験群はミラー経路を丸ごと削っても緑に
    なる = 偽の安全。
    """
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)

    s.load_project()

    assert cache.exists(), "初回読み取りでミラーが作られていない"
    assert json.loads(cache.read_text(encoding="utf-8"))["summary"] == "v1"


def test_creating_the_mirror_and_refreshing_it_are_said_differently(tmp_path, capsys):
    """「無かったので作成」と「古かったので追従」を同じ文面に畳まない。

    畳むと、初回作成しか起きていないのに「クラウドより古かった」と読める嘘を
    利用者に渡す (= この MS が消している名乗りのズレ)。
    """
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)

    s.load_project()
    created = capsys.readouterr().err
    assert MIRROR_REASON_CREATED in created
    assert MIRROR_REASON_REFRESHED not in created

    s._client.remote_write({"name": "p", "summary": "v2", "milestones": []})
    s.load_project()
    refreshed = capsys.readouterr().err
    assert MIRROR_REASON_REFRESHED in refreshed
    assert MIRROR_REASON_CREATED not in refreshed


# --------------------------------------------------------------------------
# 本題 — 同じ内容なら触らない
# --------------------------------------------------------------------------

def test_a_second_read_of_the_same_document_does_not_touch_the_mirror(tmp_path):
    """更新時刻・inode・サイズのどれも動かない (= 一切触っていない)。

    内容比較ではなく **ファイルを触ったか** で測る。内容で測ると、書き直して同じ
    内容になった場合と区別できない (それが直前の挙動そのものだった)。
    """
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()
    before = _stat(cache)

    for _ in range(3):
        s.load_project()

    assert _stat(cache) == before, (
        "クラウドの内容が同じなのに、読み取りがミラーを書き直している")


def test_a_second_read_does_not_reach_the_write_path_at_all(tmp_path, monkeypatch):
    """同じ内容のときは **原子的置き換えの口に触らない**。

    「作って消す」では全件テスト中の誤報の窓が残るので、消えたかではなく書き込みの
    契機そのものが無いことを測る。

    最初の版は ``builtins.open`` を見張っていた。だが書き込みを共有の primitive に
    寄せた時点で、そこは ``tempfile.mkstemp`` + ``os.fdopen`` を使うので
    ``builtins.open`` には現れない —— **その版はこの PR の途中で偽の緑に変わって
    いた** (独立レビューが同じ型を警告していた)。書き込みを行う 1 つの関数を直接
    見張る形にして、実装の下回りの選び方に依存しないようにする。
    """
    import local_writer

    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()

    calls = []
    real = local_writer._atomic_replace_bytes
    monkeypatch.setattr(local_writer, "_atomic_replace_bytes",
                        lambda path, out: calls.append(path) or real(path, out))

    s.load_project()

    assert calls == [], f"同じ内容なのに書き込み経路に入っている: {calls}"
    assert list(cache.parent.glob("*.tmp")) == []


def test_the_write_path_is_reached_when_the_content_differs(tmp_path, monkeypatch):
    """空振り防止: 上の見張りが「何も見ていない」のではないことを固定する。"""
    import local_writer

    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()

    calls = []
    real = local_writer._atomic_replace_bytes
    monkeypatch.setattr(local_writer, "_atomic_replace_bytes",
                        lambda path, out: calls.append(path) or real(path, out))

    s._client.remote_write({"name": "p", "summary": "v2", "milestones": []})
    s.load_project()

    assert calls == [str(cache)]


def test_a_read_that_finds_the_mirror_behind_refreshes_it_and_says_why(tmp_path, capsys):
    """他の端末が書いた後の読み取りでは追従し、**理由を述べる**。

    追従そのものは e-746 の約束 (手元のミラーが古いまま放置されると Tauri の初回
    描画が古い世界を映す)。黙ってやらないのがこの課題の分。
    """
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()
    before = _stat(cache)

    s._client.remote_write({"name": "p", "summary": "v2 (別端末が書いた)",
                            "milestones": []})
    s.load_project()

    assert _stat(cache) != before, "クラウドが変わったのにミラーが追従していない"
    assert json.loads(cache.read_text(encoding="utf-8"))["summary"].startswith("v2")
    err = capsys.readouterr()
    assert MIRROR_REASON_REFRESHED in err.err, (
        f"追従したのに理由を述べていない: stderr={err.err!r}")
    assert str(cache) in err.err, "どのファイルを触ったのかが分からない"
    assert err.out == "", "開示が stdout に出ている (--json を機械で読む側を汚す)"


def test_the_read_time_disclosure_is_said_once_per_process(tmp_path, capsys):
    """同じプロセスで何度追従しても開示は 1 回 (= 連続読みで騒がない)。"""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()
    capsys.readouterr()

    for i in range(3):
        s._client.remote_write({"name": "p", "summary": f"v{i + 2}",
                                "milestones": []})
        s.load_project()

    err = capsys.readouterr().err
    assert err.count(MIRROR_REASON_REFRESHED) == 1, (
        f"開示が繰り返されている: {err!r}")


def test_a_save_refreshes_the_mirror_without_the_read_time_note(tmp_path, capsys):
    """保存側は黙って更新してよい (利用者はファイルが変わることを期待している)。"""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()
    capsys.readouterr()

    s.save_project({"name": "p", "summary": "v9", "milestones": []})

    assert json.loads(cache.read_text(encoding="utf-8"))["summary"] == "v9"
    err = capsys.readouterr().err
    assert MIRROR_REASON_REFRESHED not in err
    assert MIRROR_REASON_CREATED not in err


def test_a_save_that_changes_nothing_also_does_not_touch_the_mirror(tmp_path):
    """内容比較は立場に関係なく効く (= 同じ内容の保存でもファイルを触らない)。"""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    s.load_project()
    before = _stat(cache)

    s.save_project(copy.deepcopy(DOC))

    assert _stat(cache) == before


# --------------------------------------------------------------------------
# 立場を黙って相続できないことを構造で固定する
# --------------------------------------------------------------------------

def test_every_mirror_call_site_states_its_origin():
    """``_write_mirror`` の全ての呼び出し口が ``origin=`` を明示する。

    ここを既定値で済ませると、読み側の新しい口が「黙って更新してよい」挙動を
    相続して、この課題が消した穴が黙って戻る。守る状態への **全ての書き手を
    列挙する** のが要点なので、呼び出しを構文木から数える (文字列一致にすると
    docstring の言及に反応する / 改行で跨ぐ呼び出しを見落とす)。
    """
    tree = ast.parse(io.open(STORE_API_PATH, encoding="utf-8").read())

    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name == "_write_mirror":
            calls.append(node)

    assert len(calls) >= 3, (
        f"ミラー更新の呼び出し口が {len(calls)} 個しか見えない — "
        "この試験が実表面を見ていない (読み 2 + 保存 1 が最低限)")
    for node in calls:
        kw = {k.arg for k in node.keywords}
        assert "origin" in kw, (
            f"lib/store_api.py:{node.lineno} の _write_mirror 呼び出しが "
            "origin= を明示していない")


def test_the_origin_argument_has_no_default():
    """既定値を置かない = 書き忘れが実行時に落ちる (黙って片側に倒れない)。"""
    tree = ast.parse(io.open(STORE_API_PATH, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_write_mirror")
    names = [a.arg for a in fn.args.kwonlyargs]
    assert "origin" in names, "origin はキーワード専用引数であること"
    default = fn.args.kw_defaults[names.index("origin")]
    assert default is None, "origin に既定値があると立場を黙って相続できる"


def test_an_unknown_origin_is_refused(tmp_path):
    """知らない立場は拒否する (綴り間違いが片側の挙動に黙って倒れない)。"""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)
    with pytest.raises(ValueError) as exc:
        s._write_mirror(DOC, origin="readonly")
    assert MIRROR_ORIGIN_READ in str(exc.value)
    assert MIRROR_ORIGIN_SAVE in str(exc.value)


# --------------------------------------------------------------------------
# 入口の実測 — 読み取りしかしない verb がファイルを触らない
# --------------------------------------------------------------------------

def test_a_read_only_verb_does_not_touch_the_mirror(tmp_path, monkeypatch, capsys):
    """全ての verb が通る継ぎ目 (``commands_shared.load_project``) で測る。

    verb を 1 つずつ並べて測る形にしなかったのは、母集団が 346 動詞あり、並べた
    一覧が次の verb で古くなるため。読み取りが書く経路はここ 1 箇所なので、
    継ぎ目で閉じれば「読み取りを名乗る入口」全体に効く。
    """
    cache = tmp_path / ".beacon" / "project.json"
    store = _make_store(DOC, cache)
    store.load_project()           # 初回 = ミラー作成
    before = _stat(cache)
    capsys.readouterr()

    import commands_shared
    monkeypatch.setattr(commands_shared, "get_store", lambda *a, **kw: store)

    data = commands_shared.load_project()

    assert data["milestones"][0]["id"] == "ms-1"
    assert _stat(cache) == before, (
        "読み取りしかしていないのに保存ファイルが触られている")
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# 下見 (``--dry-run``) は記録そのものも書かない
# --------------------------------------------------------------------------

def test_the_dry_run_branch_returns_before_any_save():
    """``pr sync --dry-run`` の分岐が、保存に到達する前に戻ることを構文木で測る。

    ミラーを触らなくなっても、下見が記録そのものを書いてしまえば約束は破れている。
    文字列一致ではなく構文木で見るのは、``save_project`` の綴りが docstring や
    コメントに出てくるため (= 説明文に反応する偽の緑を避ける)。

    空振り防止: 下見の **外** には保存が在ることも同時に測る。保存呼び出しが丸ごと
    消えても緑になる形にはしない。
    """
    import ast as _ast
    path = os.path.join(LIB, "cmd_pr.py")
    tree = _ast.parse(io.open(path, encoding="utf-8").read())
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "cmd_pr_sync")

    def _calls(node):
        return {c.func.id for c in _ast.walk(node)
                if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}

    dry_branches = [n for n in fn.body
                    if isinstance(n, _ast.If)
                    and isinstance(n.test, _ast.Name) and n.test.id == "dry_run"]
    assert len(dry_branches) == 1, (
        f"cmd_pr_sync の下見分岐が {len(dry_branches)} 個 — この試験が実表面を "
        "見ていない (名前が変わった等)")
    branch = dry_branches[0]
    assert "save_project" not in _calls(branch), "下見の分岐が記録を保存している"
    assert any(isinstance(n, _ast.Return) for n in branch.body), (
        "下見の分岐が戻っていない — 先の保存に落ちる")
    assert "save_project" in _calls(fn), (
        "cmd_pr_sync に保存呼び出しが 1 つも無い — この試験が空振りしている")


# --------------------------------------------------------------------------
# 書くのに何も返さない組み合わせを消したことを固定する
# --------------------------------------------------------------------------

def test_listing_without_a_target_names_the_candidates(monkeypatch, capsys):
    """``task list`` を対象指定なしで叩いたとき、黙って空を返さない。

    実測 (2026-10-15): 進行中の target が複数ある状態では候補を名指しして終了
    コード 1 で止まる。黙って空の一覧を返すと、呼ぶ側が「データが無い」と誤読
    する (= 起票時の実害 3)。ここはその挙動を**固定する**ための退行試験。
    """
    import cmd_task

    data = {
        "name": "p", "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "A", "status": "in_progress", "entries": []},
            {"id": "ms-2", "title": "B", "status": "in_progress", "entries": []},
        ],
    }
    monkeypatch.setattr(cmd_task, "load_project", lambda: data)
    monkeypatch.setenv("BEACON_MS_ID", "")
    monkeypatch.setenv("BEACON_JSON", "1")

    with pytest.raises(ValueError) as exc:
        cmd_task.cmd_task_list()

    message = str(exc.value)
    assert "ms-1" in message and "ms-2" in message, (
        f"候補を名指ししていない: {message!r}")
    assert capsys.readouterr().out == "", "止まるべき呼び出しが一覧を出している"


# --------------------------------------------------------------------------
# 独立 AX レビュー (misleading): 開示文が測っていない原因を断定していた
# --------------------------------------------------------------------------

def test_the_disclosure_does_not_blame_an_external_writer(tmp_path, capsys):
    """保存の直後に同じコマンドが読み戻しても「別の誰かが書いた」と言わない。

    ``commands_shared.verify_cloud_write_persisted`` は書き込みが本当に残ったかを
    確かめるため、``save_project`` の直後に *同じコマンドの中で* ``load_project``
    する (``milestone add`` / ``doc add`` が採用済み)。そしてサーバは PUT した本文を
    書き換えて保存する (owner 自動付与等 —— その関数の docstring が明記している)。
    つまり **自分の保存だけが原因で** ミラーとクラウドが食い違う。

    初版の文面は「クラウドより古かったので追従しました」で、これは測っていない
    原因 (= 外部の書き手) の断定だった。読み手は別セッションの書き込みを疑って
    無用な待ちや再実行に入る。言ってよいのは「最新ではなかった」までで、誰が
    書いたかは知らない。
    """
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(DOC, cache)

    # サーバ側の正規化を再現する: PUT した本文に owner を足して保存する。
    class NormalisingClient(FakeClient):
        def put_project(self, project_id, data):
            stored = copy.deepcopy(data)
            stored["owner"] = "auto-stamped-by-server"
            self._doc = stored
            return {"status": "ok"}

    s._client = NormalisingClient(DOC)
    s.load_project()
    capsys.readouterr()

    s.save_project({"name": "p", "summary": "v2", "milestones": []})
    s.load_project()          # = verify_cloud_write_persisted の読み戻し

    err = capsys.readouterr().err
    assert MIRROR_REASON_REFRESHED in err, (
        f"追従したのに何も述べていない: {err!r}")
    for blame in ("クラウドより古", "別のセッション", "他の端末"):
        assert blame not in err, (
            f"測っていない原因を断定している ({blame}): {err!r}")


# --------------------------------------------------------------------------
# 独立 AX レビュー (low): 置き換えが失敗したとき一時ファイルを残さない
# --------------------------------------------------------------------------

def test_a_failed_replace_leaves_no_temp_file(tmp_path, capsys):
    """ミラーのパスが普通のファイルでないとき、`.tmp` を残さず・毎回作らない。

    置き場がディレクトリ等だと ``os.replace`` が失敗する。以前はその経路で
    作りかけの一時ファイルをそのまま残し、読み取りのたびに作り直していた ——
    この PR が「一時ファイルの誤報の窓を閉じた」と名乗った実害が、その前提下では
    戻っていた。
    """
    cache = tmp_path / ".beacon" / "project.json"
    cache.parent.mkdir(parents=True)
    cache.mkdir()                      # ← 普通のファイルでない
    s = _make_store(DOC, cache)

    s.load_project()
    s.load_project()

    strays = [p.name for p in cache.parent.iterdir() if p.name != "project.json"]
    assert strays == [], f"置き換えに失敗した一時ファイルが残っている: {strays}"
    assert MIRROR_REASON_FAILED in capsys.readouterr().err, (
        "ミラーを更新できなかったことが黙って消えている")


# --------------------------------------------------------------------------
# 独立保守性レビュー (high): ミラーを書く口を 1 つに数える
# --------------------------------------------------------------------------

# project.json を書く口の台帳。ミラーの書き込み (write_mirror_if_changed) と、
# ローカルモードでは project.json 自身が真値源になる本書き込み (atomic_apply) の
# 2 つで、どちらも lib/local_writer.py に在る —— **書く作法を知っている場所は
# 1 モジュール** という形。新しい書き手が別の file に生えたら下の census が赤くなる。
MIRROR_WRITERS = {("local_writer", "write_mirror_if_changed"),
                  ("local_writer", "atomic_apply"),
                  ("local_writer", "_atomic_replace_bytes")}


def test_the_mirror_has_exactly_one_writer():
    """``.beacon/project.json`` を書き換える口が 1 つであることを数える。

    独立レビューが実測した: この PR はクラウド側の書き込み口だけを硬くしたが、
    同じ物理ファイルを書く **2 つ目の口** が既に在った
    (``commands_shared._mirror_auto_execute_channels_to_local`` —— 原子的でもなく、
    内容比較も開示も通らない)。しかも私の AST 検査は ``lib/store_api.py`` だけを
    歩いていたので、その 2 つ目が見えなかった。**守る状態への全ての書き手を
    列挙する** という既出の教訓を、同じ形でまた踏んだ。

    数え方: 本番コード (lib/ と server/) の中で「project.json を指す式」を書き込み用
    に開く / 原子的に置き換える呼び出しを構文木から拾い、台帳に載っている関数の外に
    無いことを見る。

    **この検査が見ていないもの** (= 残る限界、名乗りを広げないために明記する):
    置き場の綴りがここに挙げた名前のどれでもない場合 (例えば文字列リテラルを
    その場で組み立てる形) は見えない。綴りの列挙は狭すぎて破れる側の失敗なので、
    「ミラーの置き場を保持しうる名前」を下に列挙し、**その列挙自体が実表面を
    捉えていること** を件数で固定する。
    """
    import ast as _ast

    # ミラーの置き場を保持しうる名前。増えたら census が 0 件側に倒れないよう、
    # 下で「実際に 1 件以上見えている」ことを確かめる。
    PATH_NAMES = {"project_file", "cache_path", "mirror_path", "_mirror_path",
                  "_project_file", "pj_path", "project_json"}
    WRITE_CALLS = {"replace", "_atomic_replace_bytes", "mkstemp", "open"}

    roots = []
    for d in ("lib", "server"):
        base = os.path.join(os.path.dirname(__file__), "..", d)
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                roots.append((d + "/" + name, os.path.join(base, name)))

    def _mentions_mirror_path(node):
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Name) and sub.id in PATH_NAMES:
                return True
            if isinstance(sub, _ast.Attribute) and sub.attr in PATH_NAMES:
                return True
        return False

    offenders = []
    seen_any = 0
    for rel, path in roots:
        tree = _ast.parse(io.open(path, encoding="utf-8").read())
        stack = []

        def visit(node, chain):
            for child in _ast.iter_child_nodes(node):
                nxt = chain
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    nxt = chain + [child.name]
                if isinstance(child, _ast.Call):
                    fn = child.func
                    nm = fn.attr if isinstance(fn, _ast.Attribute) else getattr(fn, "id", "")
                    if nm in WRITE_CALLS and child.args and _mentions_mirror_path(child.args[0]):
                        writes = (nm != "open" or any(
                            isinstance(a, _ast.Constant) and isinstance(a.value, str)
                            and ("w" in a.value or "a" in a.value)
                            for a in child.args[1:]) or any(
                            k.arg == "mode" and isinstance(k.value, _ast.Constant)
                            and ("w" in str(k.value.value) or "a" in str(k.value.value))
                            for k in child.keywords))
                        if writes:
                            stack.append((rel, nxt[-1] if nxt else "", child.lineno))
                visit(child, nxt)

        visit(tree, [])
        for rel_, fname, lineno in stack:
            seen_any += 1
            if (rel_.split("/")[-1][:-3], fname) in MIRROR_WRITERS:
                continue
            offenders.append(f"{rel_}:{lineno} ({fname})")

    assert seen_any >= 1, (
        "ミラーへの書き込みを 1 件も見ていない — この検査が実表面を捉えていない "
        "(置き場の名前の列挙が古い可能性)")
    assert offenders == [], (
        "ミラーを書く口が台帳の外にある (原子性・内容比較・開示を通らない):\n  "
        + "\n  ".join(offenders))


def test_the_sqlite_sibling_also_skips_an_unchanged_mirror(tmp_path, monkeypatch):
    """ローカルの SQLite 側も同じ primitive を通る (= 毎回書かない)。

    独立保守性レビュー medium: 元の実害 (一時ファイルと更新時刻の churn) は
    「読みで書く」ではなく **「毎回書く」** という性質なので、クラウド側だけ直すと
    同じ症状を追う人が真似る先を失う。
    """
    import local_writer
    from store_sqlite import SqliteStore

    proj = tmp_path / ".beacon" / "project.json"
    proj.parent.mkdir(parents=True)
    doc = {"name": "p", "summary": "v1", "milestones": []}
    proj.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")

    store = SqliteStore(str(proj))
    store.populate_if_empty(doc)
    store.load_project()          # 保存の baseline を取る (= lost-update 検出用)

    calls = []
    real = local_writer._atomic_replace_bytes
    monkeypatch.setattr(local_writer, "_atomic_replace_bytes",
                        lambda path, out: calls.append(path) or real(path, out))

    store.save_project(copy.deepcopy(doc))      # 内容は同じ
    assert calls == [], f"同じ内容の保存でミラーを書き直している: {calls}"

    doc2 = dict(store.load_project(), summary="v2")
    store.save_project(doc2)                     # 内容が違う
    assert calls == [str(proj)], "内容が変わったのにミラーを書いていない"
