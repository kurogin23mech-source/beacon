"""テストが本番のクラウドを読まないようにする (ms-166 e-6819)。

## なぜ要るか

本番を読むテストは破壊的ではないが、**結果がそのときの本番の状態に左右される**。
変更と無関係な理由で通ったり落ちたりし、再現もできない。

実例 (e-6852 を直す過程で観測): 2 つの単体テストが、作業項目の親を **開発者の実
プロジェクト** から解決していた。片方は使った id がたまたまその実データに在ったから
通っていただけで、別の人の手元では落ちる。どちらも stub して直した。

## どこで守るか — 扉 36 個ではなく廊下 1 箇所

クライアントの読み取り動詞は約 36 個ある。1 つずつ守るのは「1 つの扉を守ると残りが
抜け道になる」形 (bus で e-5194、判断記録で e-6637 が踏んだ)。``_request`` が全ての
読み取りが通る唯一の廊下なので、その **GET の脚** で問う。書き込みは各扉で既に
守られているので GET だけに限る (廊下で全部を断つと、書き込みの診断文言が読み取りの
ものに置き換わる)。

## 測った結果

ガードを入れて 3371 件を走らせ、**落ちたテストは 0 件**だった。つまりクラウド API を
読んでいたテストは無く、e-6819 の実害は ``load_project`` 系 (cwd から見つけた
ローカルのプロジェクトを読む経路) に集中していた。そちらは e-6852 (stub の追加) と
e-6820 (保存先を指す流儀の一本化) で塞いである。このガードは同じ病理のクラウド側を
**先に閉じておく** ための防御で、入った時点から緑。
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import _ast_structural as astx   # noqa: E402
import api_client                # noqa: E402
import cloud_write_guard         # noqa: E402


class TestProdReadRefused(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("BEACON_TEST_MODE")
        os.environ["BEACON_TEST_MODE"] = "1"
        self._hatch = os.environ.pop("BEACON_ALLOW_PROD_TEST_WRITE", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BEACON_TEST_MODE", None)
        else:
            os.environ["BEACON_TEST_MODE"] = self._saved
        if self._hatch is not None:
            os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = self._hatch
        else:
            os.environ.pop("BEACON_ALLOW_PROD_TEST_WRITE", None)

    def _prod(self):
        return api_client.ApiClient("https://beacon-ai.dev", token="x")

    def test_a_read_verb_is_refused(self):
        with self.assertRaises(cloud_write_guard.ProdWriteBlocked):
            self._prod().get_project("beacon-b95643")

    def test_the_message_points_at_fixture_data(self):
        # AX: エラーは次の一手を示す。どう隔離すればよいかが読めること。
        with self.assertRaises(cloud_write_guard.ProdWriteBlocked) as ex:
            self._prod().get_project("p")
        msg = str(ex.exception)
        self.assertIn("not reproducible", msg)
        for mechanism in ("load_project", "fake_cloud_config", "isolated_project"):
            self.assertIn(mechanism, msg)

    def test_local_target_is_not_refused(self):
        client = api_client.ApiClient("http://localhost:8000", token="x")
        try:
            client.get_project("p")
        except cloud_write_guard.ProdWriteBlocked as exc:  # pragma: no cover
            self.fail("local target で読み取りが拒否されました: " + str(exc))
        except Exception:
            pass   # 転送エラーは想定内

    def test_escape_hatch_allows_prod(self):
        os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = "1"
        try:
            self._prod().get_project("p")
        except cloud_write_guard.ProdWriteBlocked as exc:  # pragma: no cover
            self.fail("抜け道が効いていません: " + str(exc))
        except Exception:
            pass

    def test_noop_outside_test_context(self):
        os.environ.pop("BEACON_TEST_MODE", None)
        saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            self.assertFalse(
                cloud_write_guard.prod_test_write_blocked("https://beacon-ai.dev"))
        finally:
            if saved is not None:
                os.environ["PYTEST_CURRENT_TEST"] = saved


# --- 廊下に置いたことを構造で固定する ---------------------------------------

def _api_client_class():
    tree = ast.parse(open(os.path.join(_LIB, "api_client.py"), encoding="utf-8").read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "ApiClient")


def test_the_transport_asks_the_read_guard():
    methods = astx.functions_of(_api_client_class())
    called = astx.called_names(methods["_request"])
    assert "_guard_read" in called, (
        "_request が読み取りガードを通っていません — 36 ある読み取り動詞のどれかが"
        f"抜け道になります。呼ばれている: {sorted(called)}")


def test_the_read_guard_helper_delegates_to_the_single_rule():
    methods = astx.functions_of(_api_client_class())
    called = astx.called_names(methods["_guard_read"])
    assert "guard_prod_read" in called, sorted(called)


def test_no_read_bypasses_the_transport():
    """生 HTTP を自分で組む読み取りが無いこと (= 廊下を通らない読み取りが無い)。

    これが崩れると、廊下に置いたガードが「全部の読み取りを見ている」という前提が
    嘘になる — e-6637 で ``upload_document_image`` が扉判定をすり抜けたのと同じ形。
    """
    cls = _api_client_class()
    raw = {name for name, fn in astx.functions_of(cls).items()
           if astx.called_names(fn) & {"Request", "urlopen"}}
    # _request は廊下そのもの。upload_document_image は書き込み (POST) で、
    # e-6637 の負債台帳に記録済み。読み取りで生 HTTP を使うものは無い。
    assert raw == {"_request", "upload_document_image"}, (
        "生 HTTP を使うメソッドが変わりました — 読み取りが廊下を迂回していないか"
        f"確認してください: {sorted(raw)}")


def test_only_the_get_leg_is_guarded_for_reads():
    """書き込みの診断を読み取りのものに置き換えていないこと。

    廊下で method を問わず断つと、書き込み扉が持つ固有の文言 (追記専用だから
    消せない 等) が読み取りの文言に差し替わって、読み手が誤った次の一手を取る。
    """
    src = open(os.path.join(_LIB, "api_client.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = astx.function_named(tree, "_request")
    guarded_inside_if = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            if "_guard_read" in astx.called_names(node):
                guarded_inside_if.append(node.lineno)
    assert guarded_inside_if, (
        "読み取りガードが条件なしで呼ばれています — GET 以外も断つと書き込みの"
        "診断文言が置き換わります")
    assert "GET" in src[src.index("def _request"):src.index("def _request") + 1200], (
        "GET の脚で限定していることが読めません")


# ---------------------------------------------------------------------------
# 5. 拒否文言の共有部分が 1 箇所から来ていること (保守性レビュー PR#785 M-6)
# ---------------------------------------------------------------------------
#
# 「どう隔離すればよいか」の案内 (fixture 2 つ + 非本番の base_url) を各ガードが手書きして
# いたため、3 つ目のガードを足した瞬間に先の 2 つが古くなる形だった。_hatch_note で
# 学んだ教訓がもう一段大きいこの重複には適用されていなかった。1 箇所から作る。

_ISOLATION_MECHANISMS = ("fake_cloud_config", "isolated_project", "local/sandbox")


def _refusal_messages() -> dict:
    """各ガードの拒否文言を ``{関数名: 文言}`` で集める (本番 × テスト文脈で発火させる)。"""
    saved = os.environ.get("BEACON_TEST_MODE")
    hatch = os.environ.pop("BEACON_ALLOW_PROD_TEST_WRITE", None)
    os.environ["BEACON_TEST_MODE"] = "1"
    out = {}
    try:
        for name in ("guard_prod_bus_write", "guard_prod_decision_write",
                     "guard_prod_read"):
            fn = getattr(cloud_write_guard, name)
            try:
                fn("https://beacon-ai.dev")
            except cloud_write_guard.ProdWriteBlocked as exc:
                out[name] = str(exc)
    finally:
        if saved is None:
            os.environ.pop("BEACON_TEST_MODE", None)
        else:
            os.environ["BEACON_TEST_MODE"] = saved
        if hatch is not None:
            os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = hatch
    return out


def test_every_guard_offers_the_same_isolation_mechanisms():
    msgs = _refusal_messages()
    assert len(msgs) == 3, sorted(msgs)
    for name, msg in sorted(msgs.items()):
        for mechanism in _ISOLATION_MECHANISMS:
            assert mechanism in msg, (
                name + " の拒否文言に隔離手段 " + mechanism + " が出てきません — "
                "案内を手書きで分岐させると、次に手段が増減したとき 1 箇所だけ古くなります")


def test_the_shared_guidance_comes_from_one_place():
    """案内文が共有ヘルパから来ていること (手書きのコピーに戻っていないこと)。

    文字列一致ではなく **ヘルパを呼んでいるか** を構文木で見る (同じ PR の
    tests/_ast_structural が「substring は docstring の言及で素通りする」と明記)。
    """
    tree = ast.parse(open(os.path.join(_LIB, "cloud_write_guard.py"),
                          encoding="utf-8").read())
    for name in ("guard_prod_bus_write", "guard_prod_decision_write",
                 "guard_prod_read"):
        fn = astx.function_named(tree, name)
        assert fn is not None, name
        called = astx.called_names(fn)
        assert "_isolation_options_note" in called, (
            name + " が共有の案内ヘルパを呼んでいません — 手書きに戻っています。"
            "呼ばれている: " + repr(sorted(called)))


def test_the_sharing_guard_fails_when_a_guard_hand_rolls_the_guidance():
    # test-the-test: ヘルパを呼ばず案内を手書きしたガードを合成して、検出が拾うことを確かめる。
    src = (
        "def guard_prod_something(base_url):\n"
        "    raise ProdWriteBlocked(\n"
        '        "use the ``fake_cloud_config`` fixture or ``isolated_project``"\n'
        "    )\n"
    )
    fn = astx.function_named(ast.parse(src), "guard_prod_something")
    assert "_isolation_options_note" not in astx.called_names(fn), (
        "guard が緩い: 文言に fixture 名が出ているだけで通ってしまう")
