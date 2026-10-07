"""テスト実行が本番の判断記録を汚さないようにする (ms-166 e-6637)。

## なぜ要るか

判断記録 (decision = 誰が何をなぜ決めたかの記録) の流れは **追記専用** で、削除経路が
無い。だからテストが書いた行は後から取り消せない。実測すると、ある 1 セッションの
判断記録 100 件のうち約 90 件がそのセッション自身のテスト実行由来の偽データ
(``ms-9`` / ``opp-3`` / ``ms-T`` ほか) だった。つまり **人間が「AI が何を判断したか」を
確かめるために読む面が、ほぼ作り話で埋まっていた**。これは Beacon の本質的価値
(AI の判断の監査可能性) が成立しない状態そのもの。

## どこを守るか — 呼び出し元ではなく絞り所

``api_client.ApiClient.record_decision`` が追記専用の流れに入る唯一の扉。``lib/`` だけで
11 箇所 / 7 ファイルから呼ばれており、呼び出し元の層に置くと 8 箇所と今後増える経路が
漏れる。これは bus で e-5194 が見つけ e-5216 が絞り所で閉じた
「1 つの扉を守るガードは、残りを抜け道として残す」と同じ形。

判定は新しく作らず ``lib/cloud_write_guard`` の既存の連鎖 (テスト文脈か ×
本番 URL か × 抜け道の環境変数) を使う。「テスト中かを見る分岐を本番コードに入れるか」は
e-4029 で既に決着しており、新しい方針判断ではなく **配線漏れ**だった。

## 失敗契約との関係

判断記録の書き込みは ``commands_shared.best_effort_decision_write`` に包まれており、
例外を警告にして飲む (呼び出し元のフローを壊さないため)。``ProdWriteBlocked`` は
その広い網に落ちるが、**「書き込み失敗」と報告してはならない** — ガードが正しく働いた
のであって、endpoint の問題ではない。読み手を存在しない障害の捜索に送り出さないよう、
受け口が種別を見分けて別の文言で報告する (``ProdWriteBlocked`` が固有型である理由そのもの、
e-5300)。再 raise はしない: 監査の副作用が呼び出し元のフローを壊さない契約を保つ。

## このファイルが pin するもの

1. 扉が守られている — 本番 × テスト文脈で拒否、local / sandbox では素通り、抜け道は効く。
2. 受け口がガード拒否を「失敗」と誤報告しない。
3. **新しい書き込み扉が無言で未ガードで増えない** (負債台帳 + 一方通行のラチェット)。
4. 3 のラチェットが実際に機能すること (= test-the-test)。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
import unittest

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import _ast_structural as astx  # noqa: E402  (tests/ の共有プリミティブ)
import api_client            # noqa: E402
import cloud_write_guard     # noqa: E402
import commands_shared       # noqa: E402


# ---------------------------------------------------------------------------
# 1. 扉が守られている
# ---------------------------------------------------------------------------

class TestRecordDecisionGuarded(unittest.TestCase):
    """``record_decision`` は本番への追記をテスト文脈で拒否する。

    形は ``TestApiClientBusPostGuarded`` (bus 側) に揃えている — 同じ絞り所の思想を
    別の流れに適用したものなので、読み手が 2 つを並べて読めるようにする。
    """

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

    def _prod_client(self):
        return api_client.ApiClient("https://beacon-ai.dev", token="x")

    def test_record_decision_blocks_prod_in_test_context(self):
        with self.assertRaises(cloud_write_guard.ProdWriteBlocked):
            self._prod_client().record_decision(
                "beacon-b95643", {"kind": "task-done", "decision": "done"})

    def test_refusal_message_says_the_stream_cannot_be_undone(self):
        # AX: エラーは次の一手を示す。追記専用で取り消せないことと、local mode の
        # 作法 (fake_cloud_config / 本番でない base_url) が読めること。
        with self.assertRaises(cloud_write_guard.ProdWriteBlocked) as ex:
            self._prod_client().record_decision("p", {"kind": "x", "decision": "y"})
        msg = str(ex.exception)
        self.assertIn("append-only", msg)
        self.assertIn("fake_cloud_config", msg)
        self.assertIn("BEACON_ALLOW_PROD_TEST_WRITE", msg)

    def test_local_target_is_not_refused(self):
        # local / sandbox は素通り (転送エラーで落ちるのは構わないが、ガード拒否は困る)。
        client = api_client.ApiClient("http://localhost:8000", token="x")
        try:
            client.record_decision("p", {"kind": "x", "decision": "y"})
        except cloud_write_guard.ProdWriteBlocked as exc:  # pragma: no cover
            self.fail("local target でガードが拒否しました: " + str(exc))
        except Exception:
            pass   # 転送エラーは想定内

    def test_escape_hatch_allows_prod(self):
        # 本当に本番へ書く必要があるテストは明示的に opt-in できる (ただし追記専用なので
        # 後片付けの相手が無いことは拒否文言が警告している)。
        os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = "1"
        try:
            self._prod_client().record_decision("p", {"kind": "x", "decision": "y"})
        except cloud_write_guard.ProdWriteBlocked as exc:  # pragma: no cover
            self.fail("抜け道が効いていません: " + str(exc))
        except Exception:
            pass   # 転送エラーは想定内

    def test_noop_outside_test_context(self):
        # 通常の CLI / 自律実行は一切影響を受けない。テスト文脈の判定は
        # BEACON_TEST_MODE と PYTEST_CURRENT_TEST の **両方** を見るので両方外す
        # (既存 tests/test_cloud_write_guard.py と同じ作法)。
        os.environ.pop("BEACON_TEST_MODE", None)
        saved_pytest = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            self.assertFalse(
                cloud_write_guard.prod_test_write_blocked("https://beacon-ai.dev"))
            # 実際に扉を叩いても拒否されない (転送エラーは想定内)。
            try:
                self._prod_client().record_decision("p", {"kind": "x", "decision": "y"})
            except cloud_write_guard.ProdWriteBlocked as exc:  # pragma: no cover
                self.fail("テスト文脈外で拒否されました: " + str(exc))
            except Exception:
                pass
        finally:
            if saved_pytest is not None:
                os.environ["PYTEST_CURRENT_TEST"] = saved_pytest


# ---------------------------------------------------------------------------
# 2. 受け口がガード拒否を「失敗」と誤報告しない
# ---------------------------------------------------------------------------

def test_seam_reports_a_guard_refusal_as_the_guard_working(caplog):
    with caplog.at_level(logging.WARNING):
        with commands_shared.best_effort_decision_write("task-done for task=e-1"):
            raise cloud_write_guard.ProdWriteBlocked("refusing to append ...")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "refused by the prod-test-write guard" in msgs, msgs
    assert "the guard working, not a failure" in msgs, msgs
    # 「失敗」の文言を出してはならない (存在しない endpoint 障害の捜索に送り出す)。
    assert "decision write failed" not in msgs, msgs


def test_seam_still_reports_a_real_failure_as_a_failure(caplog):
    # 区別が効いていること = 本物の失敗は従来どおり「失敗」と出る。
    with caplog.at_level(logging.WARNING):
        with commands_shared.best_effort_decision_write("task-done for task=e-1"):
            raise RuntimeError("502 endpoint down")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "decision write failed" in msgs, msgs
    assert "prod-test-write guard" not in msgs, msgs


def test_seam_does_not_break_the_caller_on_a_guard_refusal():
    # 監査の副作用が呼び出し元のフローを壊さない契約は維持する (再 raise しない)。
    reached = []
    with commands_shared.best_effort_decision_write("x"):
        raise cloud_write_guard.ProdWriteBlocked("nope")
    reached.append(True)
    assert reached == [True]


# ---------------------------------------------------------------------------
# 3. 新しい書き込み扉が無言で未ガードで増えない (負債台帳 + 一方通行ラチェット)
# ---------------------------------------------------------------------------
#
# e-6637 を直す過程で測ると、``ApiClient`` の書き込み扉は 61 件あり、ガード済みは
# 8 件 (プロジェクト 2 / bus 5 / 判断記録 1 = 今回追加) だけだった。残り 53 件は
# **テストから本番に書ける扉として開いている**。e-4029 / e-5194 / e-5216 / e-6637 と
# 4 回にわたり「扉を 1 つずつ見つけて塞ぐ」をやってきたので、ここで数え方を変える:
# 今日の未ガードを **記録された負債**として台帳に固定し、**新しい扉だけをゲートする**
# (#780 / e-6833 が同じ形を取った)。
#
# 53 件を今この PR で塞がないのは、どの扉に本番テスト書き込みガードが必要かの判断
# (例: me_heartbeat は無害かもしれない / trek 系は別の隔離機構を持つかもしれない) が
# 扉ごとに違い、e-6637 の範囲を超えるため。**覆えていない範囲を黙って覆えたことに
# しない**ために台帳として可視化する。
#
# ラチェットは一方通行: 扉がガードされたらこの一覧から削除する。直った項目を残すと
# 同じ場所の次の退行を黙って通す (#781 の既知漏れ一覧で親が実際に踏んだ病理)。

_WRITE_VERBS = frozenset({"post", "put", "delete", "patch"})
# 生 HTTP を自分で組む印 (ms-166 e-6637 / PR#785 独立 AX レビュー AX-1)。
# 初版の検出器は post/put/delete/patch という **verb 名** だけを見ていたため、
# ``upload_document_image`` (urllib.request.Request で multipart POST する本番書き込み)
# を扉として一度も見ていなかった — ガード済みでも負債でもなく「存在しない」扱いで、
# 非空性テスト (len(doors) >= 40) は緑のまま通った。覆域が呼び出しの **形** に
# 依っていたので、形を変えた扉が素通りした。これはこのファイルが防ぐはずだった
# 「緑のガードが実は何も見ていない」そのもの。
_RAW_HTTP_MARKERS = frozenset({"Request", "urlopen"})
# 生 HTTP を組む唯一の共有経路 = 廊下であって扉ではない。読み取り (GET) も通るので
# ここにガードを置くと読みまで止まる。これ以外に生 HTTP を組むメソッドが現れたら
# それは扉なので doors に入る (下の guard がそれを機械で確かめる)。
_RAW_TRANSPORT = frozenset({"_request"})

# 輸送路が **書き込みを** 守っていると数えてよい呼び出し名 (ms-166 e-6854)。
#
# 初版は「輸送路が何らかのガードを呼んでいるか」で判定していた。_request には
# 読み取り用の _guard_read が在るので、**書き込みの受け止めを丸ごと外しても
# 「輸送路はガード済み」と判定され、62 の扉が覆われているまま緑になった**
# (mutation テストで発覚)。覆域の軸が「ガードが本体のどこかに在るか」= 表層で、
# 「書き込み脚が守られているか」= 意味ではなかった。
_WRITE_GUARD_NAMES = frozenset({"_guard_write", "guard_prod_write"})

# 負債台帳の各項目に要る鍵と、許す triage の値。
_LEDGER_TRIAGE_VALUES = frozenset({"pending", "exempt"})


def _load_debt_ledger():
    """負債台帳を ``{method: 項目}`` で返す。

    項目は ``{"method", "triage", ...}``。bare な文字列配列ではなく項目にしたのは
    (PR#785 独立 AX レビュー AX-4)、「どの扉を未ガードのまま許すか」の判断が扉ごとに
    違うのに、台帳が名前しか運んでいなかったため — 次の読み手が判断を再導出するしか
    なかった。``triage`` は ``pending`` (まだ仕分けていない / 担当 task を持つ) か
    ``exempt`` (ガード不要と判断済み、``reason`` 必須) のどちらか。
    """
    rows = json.load(open(_DEBT_FILE, encoding="utf-8"))
    return {r["method"]: r for r in rows}


_DEBT_FILE = os.path.join(os.path.dirname(__file__), "_unguarded_api_doors.json")
_DEBT_LEDGER = _load_debt_ledger()
_RECORDED_UNGUARDED_DOORS = frozenset(_DEBT_LEDGER)


def api_client_write_doors():
    """``({扉: ガード済みか}, [guard helper 名])`` を構文木から求める。

    「呼ばれている名前を集める」プリミティブは ``tests/_ast_structural`` の共有物を
    使う (保守性レビュー PR#785 M-3: 同じ分岐を 3 つの構造ガードが手書きしていた)。
    ここが持つのは **何を扉と見なし、何をガードと見なすかの述語だけ**。

    扉 = 書き込み verb (``post`` / ``put`` / ``delete`` / ``patch``) を呼ぶか、
    **生 HTTP を自分で組む** メソッド (共有の輸送路を除く)。後者を入れないと、形を
    変えた書き込みが覆域から静かに外れる (AX-1 の実害)。
    ガード済み = ``guard_prod*`` を直接呼ぶか、それを呼ぶ ``_guard*`` helper を経由する。
    """
    tree = ast.parse(open(os.path.join(_LIB, "api_client.py"), encoding="utf-8").read())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ApiClient")
    methods = {name: astx.called_names(fn)
               for name, fn in astx.functions_of(cls).items()}
    helpers = {n for n, c in methods.items()
               if n.startswith("_guard") and any(x.startswith("guard_prod") for x in c)}

    def guarded(c):
        return any(x.startswith("guard_prod") for x in c) or bool(c & helpers)

    # ms-166 e-6854: **廊下経由のガードを覆域に数える。**
    #
    # 初版はメソッド自身の本体がガードを呼ぶかだけを見ていた。そのため共有の輸送路
    # (_request) の非 GET 脚に受け止めを置いても、62 の扉は「未ガード」と数えられ
    # 続けた — 実際には守られているのに台帳を空にできない状態。覆域の軸が
    # 「どこに書いてあるか」で、「実際に通るか」ではなかった。
    #
    # 輸送路が守られているなら、そこを通る扉は守られている。ただし **生 HTTP を
    # 自分で組む扉は輸送路を通らない** ので、この恩恵を受けない (自分でガードを
    # 呼ぶ必要がある)。この区別を落とすと、形を変えた扉が廊下の覆域に紛れて
    # 素通りする (AX-1 が指摘したのと同じ形)。
    transport_guarded = {n for n in _RAW_TRANSPORT
                         if methods.get(n, set()) & _WRITE_GUARD_NAMES}
    # 薄い verb ラッパ (post / put / …) は輸送路を呼ぶだけなので、そこを通る扉も
    # 同じ恩恵を受ける。名前で決め打ちせず「輸送路を呼んでいるか」で求める。
    routes_to_transport = {n for n, c in methods.items()
                           if c & _RAW_TRANSPORT} | set(_RAW_TRANSPORT)

    def covered(name, c):
        if guarded(c):
            return True
        if c & _RAW_HTTP_MARKERS:
            return False      # 輸送路を通らない扉は自分で守るしかない
        return bool(transport_guarded) and bool(c & routes_to_transport)

    doors = {n: covered(n, c) for n, c in methods.items()
             if ((c & _WRITE_VERBS) or (c & _RAW_HTTP_MARKERS))
             and n not in _RAW_TRANSPORT and not n.startswith("_guard")}
    return doors, sorted(helpers)


def raw_http_methods():
    """生 HTTP を自分で組む ``ApiClient`` メソッドの集合 (輸送路も含む)。"""
    tree = ast.parse(open(os.path.join(_LIB, "api_client.py"), encoding="utf-8").read())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ApiClient")
    return {name for name, fn in astx.functions_of(cls).items()
            if astx.called_names(fn) & _RAW_HTTP_MARKERS}


def test_record_decision_is_wired_to_the_guard():
    doors, helpers = api_client_write_doors()
    assert doors.get("record_decision") is True, (
        "record_decision が prod-test-write ガードを通っていません (ms-166 e-6637)")
    assert "_guard_decision_write" in helpers, (
        "判定を 1 箇所に置く helper が在りません — 呼び出し地点に 2 行の前置きを"
        "コピーすると次の author に暗黙の同期義務を課します (_guard_bus_write と同じ分界)")


def test_no_new_unguarded_write_door():
    doors, _ = api_client_write_doors()
    unguarded = {n for n, ok in doors.items() if not ok}
    new = sorted(unguarded - _RECORDED_UNGUARDED_DOORS)
    assert not new, (
        "ガードを通らない新しい書き込み扉が増えました (ms-166 e-6637 のラチェット): "
        + repr(new) + "。テストから本番に書ける扉なので、_guard_* helper を通すか、"
        "本当に不要なら tests/_unguarded_api_doors.json に理由を添えて足してください。")


def test_fixed_doors_are_dropped_from_the_debt_list():
    # 一方通行: ガードされた扉が一覧に残ると、同じ場所の次の退行を黙って通す。
    doors, _ = api_client_write_doors()
    stale = sorted(n for n, ok in doors.items()
                   if ok and n in _RECORDED_UNGUARDED_DOORS)
    assert not stale, (
        "ガード済みになった扉が負債一覧に残っています (免除が腐り、同じ場所の次の退行を"
        "黙って通します): " + repr(stale) + " — tests/_unguarded_api_doors.json から"
        "削除してください。")


def test_debt_list_names_only_real_methods():
    # 改名で一覧が腐ると、ゲートが効いているつもりで実は何も見ていない形になる。
    doors, _ = api_client_write_doors()
    ghosts = sorted(_RECORDED_UNGUARDED_DOORS - set(doors))
    assert not ghosts, (
        "負債一覧に実在しない扉が載っています (改名で腐りました): " + repr(ghosts))


def test_the_ratchet_actually_fails_on_a_new_door():
    # test-the-test: 未ガードの新しい扉を合成して、ラチェットが捕まえることを確かめる。
    doors = {"brand_new_write": False, "record_decision": True}
    new = sorted({n for n, ok in doors.items() if not ok} - _RECORDED_UNGUARDED_DOORS)
    assert new == ["brand_new_write"], new


def test_every_raw_http_method_is_a_door_or_the_declared_transport():
    """生 HTTP を自分で組むメソッドは、扉か宣言済みの輸送路のどちらかであること。

    PR#785 独立 AX レビュー AX-1 の回帰ピン。初版は ``len(doors) >= 40`` しか言って
    いなかったので、verb 名を使わない扉 (``upload_document_image``) が覆域から静かに
    外れていても緑だった。「十分な数が見えている」は「見落ちが無い」を意味しない。
    """
    doors, _ = api_client_write_doors()
    unaccounted = sorted(raw_http_methods() - set(doors) - _RAW_TRANSPORT)
    assert not unaccounted, (
        "生 HTTP で書き込むメソッドが扉として数えられていません (覆域の外に居るので"
        "ラチェットが見ません): " + repr(unaccounted) + "。扉なら検出器の述語に入れ、"
        "共有の輸送路なら _RAW_TRANSPORT に理由付きで宣言してください。")


def test_the_raw_http_door_found_by_the_review_is_accounted_for():
    """生 HTTP の扉が「説明されていない」状態に戻ったら落ちる。

    この試験は当初「upload_document_image が負債台帳に載っていること」を固定していた。
    それは **その時点の状態** (未ガードで台帳に記録済) であって不変条件ではない。
    e-6854 でこの扉を実際にガードしたら、台帳から外すのが正しい状態なのに、この
    試験が「台帳に載っていない」と言って落ちた。

    試験名が言っているのは accounted_for = **説明されていること**。守られているか、
    理由付きで免除されているか、どちらかであればよい。「見えていない」が唯一の
    不正な状態 (AX-1 が見つけたのはまさにそれ: 扉として一度も数えられていなかった)。
    """
    doors, _ = api_client_write_doors()
    assert "upload_document_image" in doors, (
        "upload_document_image が扉として見えていません (AX-1 の退行)")
    accounted = (doors["upload_document_image"]
                 or "upload_document_image" in _RECORDED_UNGUARDED_DOORS)
    assert accounted, (
        "upload_document_image が守られてもおらず台帳にも載っていません — "
        "未ガードの本番書き込み扉を台帳からも落とすと、誰も見ない状態に戻ります")


def test_the_transport_is_not_counted_as_a_door():
    # 廊下にガードを置くと読み取りまで止まるので、扉として数えてはならない。
    doors, _ = api_client_write_doors()
    for name in _RAW_TRANSPORT:
        assert name not in doors, name


def test_the_raw_http_guard_actually_fails_on_an_unaccounted_door(tmp_path):
    # test-the-test: 生 HTTP で書くが verb を呼ばないメソッドを合成し、検出器が
    # それを扉として拾うことを確かめる (初版が見落とした形そのもの)。
    src = (
        "import urllib.request\n"
        "\n"
        "class ApiClient:\n"
        "    def upload_something(self, pid, body):\n"
        "        req = urllib.request.Request(self._base_url, data=body, method='POST')\n"
        "        with urllib.request.urlopen(req) as r:\n"
        "            return r.read()\n"
    )
    p = tmp_path / "fake_client.py"
    p.write_text(src, encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef))
    names = {n: astx.called_names(fn) for n, fn in astx.functions_of(cls).items()}
    raw = {n for n, c in names.items() if c & _RAW_HTTP_MARKERS}
    verbs = {n for n, c in names.items() if c & _WRITE_VERBS}
    assert raw == {"upload_something"}, raw
    assert verbs == set(), (
        "この合成例は verb を呼ばない形でなければ回帰ピンにならない: " + repr(verbs))


# --- 負債台帳の形が腐らないようにする (AX-4) ---------------------------------

def test_debt_ledger_entries_carry_a_triage_state():
    for name, row in sorted(_DEBT_LEDGER.items()):
        assert row.get("triage") in _LEDGER_TRIAGE_VALUES, (
            name + " の triage が " + repr(sorted(_LEDGER_TRIAGE_VALUES))
            + " のいずれでもありません: " + repr(row))


def test_exempt_entries_state_why():
    # 「ガード不要」と判断した扉は理由を持たなければならない。理由の無い免除は
    # 「書き忘れ」と区別できず、次の読み手が判断を再導出するしかなくなる。
    missing = sorted(n for n, r in _DEBT_LEDGER.items()
                     if r.get("triage") == "exempt" and not (r.get("reason") or "").strip())
    assert not missing, (
        "ガード不要と宣言した扉に理由がありません: " + repr(missing))


def test_pending_entries_name_the_task_that_will_triage_them():
    # 未仕分けの項目が担当 task を持たないと、台帳が「置き場所」になって誰も戻らない。
    orphan = sorted(n for n, r in _DEBT_LEDGER.items()
                    if r.get("triage") == "pending" and not (r.get("owner_task") or "").strip())
    assert not orphan, (
        "未仕分けの扉に担当 task がありません: " + repr(orphan))


def test_the_guard_detection_is_not_vacuous():
    # 検出器が「全部ガード済み」や「扉ゼロ」を返して無条件に緑になる形を防ぐ。
    doors, helpers = api_client_write_doors()
    assert len(doors) >= 40, "扉の検出が少なすぎます: " + repr(len(doors))
    # 「十分な数が見えている」は「見落ちが無い」を意味しない (AX-1)。形の違う扉が
    # 覆域から外れていないことは test_every_raw_http_method_is_a_door_... が見る。
    assert raw_http_methods(), "生 HTTP の検出が空です (印の綴り違いの疑い)"
    guarded = {n for n, ok in doors.items() if ok}
    assert {"post_bus_event", "create_project", "record_decision"} <= guarded, (
        "既知のガード済み扉を検出できていません: " + repr(sorted(guarded)))
    assert "_guard_bus_write" in helpers and "_guard_decision_write" in helpers


# ---------------------------------------------------------------------------
# 4. 同じ出来事が経路によって別の診断にならない (PR#785 独立 AX レビュー AX-2)
# ---------------------------------------------------------------------------
#
# 絞り所 ``record_decision`` に至る経路は 2 系統ある: 監査の副作用として呼ぶ
# best-effort 経路 (共有の受け口を通る) と、記録そのものが目的の前景コマンド
# ``beacon decision record``。ガードが拒否したという **同じ出来事** が、前者では
# 「ガードが働いた」、後者では「Error: failed to record decision」になっていた。
# 読み手 (人でも AI でも) は後者を endpoint の障害と受け取り、再試行や調査に向かう。
# 分類は例外の型で 1 回だけ決める。
#
# 責務の違いは残す: 前景コマンドは記録できなかったなら非ゼロで落ちるのが正しい
# (飲むのは副作用経路だけ)。揃えるのは「何が起きたか」の説明。

def _cmd_decision_record_excepts():
    """``cmd_decision_record`` の except 節が捕まえる型の名前 (出現順)。"""
    tree = ast.parse(open(os.path.join(_LIB, "cmd_decision.py"), encoding="utf-8").read())
    fn = astx.function_named(tree, "cmd_decision_record")
    assert fn is not None, "cmd_decision_record が見つかりません"
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                t = h.type
                if t is None:
                    out.append("bare")
                elif isinstance(t, ast.Name):
                    out.append(t.id)
                else:
                    out.append(getattr(t, "attr", "?"))
    return out


def test_foreground_record_command_classifies_a_guard_refusal():
    # 前景コマンドが ProdWriteBlocked を名前で見分けていること (型で分類する)。
    src = open(os.path.join(_LIB, "cmd_decision.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = astx.function_named(tree, "cmd_decision_record")
    called = astx.called_names(fn)
    assert "ProdWriteBlocked" in {
        getattr(n, "attr", None) or getattr(n, "id", None)
        for node in ast.walk(fn) if isinstance(node, ast.Attribute)
        for n in [node]
    } | called, (
        "cmd_decision_record が ProdWriteBlocked を見分けていません — ガード拒否が"
        "「Error: failed」として報告され、読み手を存在しない障害の捜索に送り出します")


def test_foreground_record_command_does_not_call_a_guard_refusal_a_failure(capsys):
    # 実挙動: ガード拒否のとき stderr が「失敗」ではなく「ガード」と言い、次の一手を示す。
    import cmd_decision

    class _Refusing:
        def record_decision(self, project_id, decision):
            raise cloud_write_guard.ProdWriteBlocked(
                "refusing to append a decision to the production cloud ...")

    saved_cloud = cmd_decision._is_cloud_mode
    saved_client = cmd_decision._get_api_client
    env_keys = ("BEACON_DECISION_WHAT", "BEACON_DECISION_RATIONALE",
                "BEACON_DECISION_KIND", "BEACON_DECISION_EVIDENCE", "BEACON_JSON")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    try:
        cmd_decision._is_cloud_mode = lambda: True
        cmd_decision._get_api_client = lambda: (_Refusing(), {"project_id": "p1"})
        os.environ["BEACON_DECISION_WHAT"] = "何かを決めた"
        os.environ["BEACON_DECISION_RATIONALE"] = "理由"
        # 一級の判断記録は根拠の link が必須 (この経路の既存契約)。
        os.environ["BEACON_DECISION_EVIDENCE"] = "commit:abc1234"
        os.environ.pop("BEACON_JSON", None)
        with pytest.raises(SystemExit) as ex:
            cmd_decision.cmd_decision_record()
        assert ex.value.code == 1, "記録できなかったので非ゼロで落ちるのが正しい"
        err = capsys.readouterr().err
        assert "Refused:" in err, err
        assert "ガード" in err, err
        assert "Error: failed to record decision" not in err, (
            "ガード拒否を『失敗』と呼んでいます (AX-2 の退行): " + err)
    finally:
        cmd_decision._is_cloud_mode = saved_cloud
        cmd_decision._get_api_client = saved_client
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_specific_branch_precedes_the_broad_one():
    # ProdWriteBlocked は RuntimeError の下なので、広い except が先に在ると
    # 特定の分岐が死んだコードになる。順序が load-bearing であることを固定する。
    order = _cmd_decision_record_excepts()
    assert "Exception" in order, order
    # ProdWriteBlocked は except 節ではなく isinstance で分類しているので、
    # 広い except の **中** で先に判定していることを本文で確かめる。
    src = open(os.path.join(_LIB, "cmd_decision.py"), encoding="utf-8").read()
    i_check = src.index("ProdWriteBlocked")
    i_generic = src.index('"Error: failed to record decision')
    assert i_check < i_generic, (
        "汎用の失敗メッセージがガード判定より先に出ています (特定の分岐が死にます)")


# --- 廊下の受け止めを「振る舞い」で留める (ms-166 e-6854) --------------------
#
# 構文木の検査は覆域を数えるのに要るが、それだけだと軸を間違えたときに気づけない。
# 実際に間違えた: 初版は「輸送路が何らかのガードを呼ぶか」で判定していたので、
# 書き込みの受け止めを丸ごと外しても緑のままだった (読み取り用のガードが残って
# いたため)。**実際に書いてみて断られるか** を測るのが、軸の取り違えに強い。

def _prod_client():
    """本番 URL を向いた ApiClient (テスト文脈 = conftest が BEACON_TEST_MODE を立てる)。"""
    import api_client
    return api_client.ApiClient("https://beacon-ai.dev")


def test_a_door_with_no_guard_of_its_own_is_still_refused_by_the_corridor():
    """固有のガードを持たない扉が、廊下の受け止めで断られること。

    add_note は 53 件の負債側に居たメソッドの 1 つで、自分ではガードを呼ばない。
    ここが断られるなら、同じ形の扉 (= 共有の輸送路を通る書き込み) は全部断られる。
    """
    import cloud_write_guard
    import pytest as _pytest
    with _pytest.raises(cloud_write_guard.ProdWriteBlocked) as ei:
        _prod_client().add_note("p-test", "テストからの書き込み")
    assert "beacon-ai.dev" in str(ei.value)


def test_the_raw_multipart_door_is_refused_too():
    """共有の輸送路を通らない扉 (multipart を自分で組む) も断られること。

    廊下に受け止めを置いてもここは素通りするので、個別にガードを呼んでいる。
    """
    import cloud_write_guard
    import pytest as _pytest
    with _pytest.raises(cloud_write_guard.ProdWriteBlocked):
        # 存在しないパスを渡しても、ファイルを読む前に断られる
        _prod_client().upload_document_image("p-test", "/nonexistent/a.png")


def test_reading_prod_is_refused_with_the_read_wording_not_the_write_one():
    """読み取りは読み取りの文言で断られること (診断が入れ替わらない)。

    廊下で全部を同じ文言で断ると、書き込みの診断が読み取りのものに置き換わる
    (あるいは逆)。脚ごとに別のガードを置いている理由がこれ。
    """
    import cloud_write_guard
    import pytest as _pytest
    with _pytest.raises(cloud_write_guard.ProdWriteBlocked) as ei:
        _prod_client().get_project("p-test")
    assert "read" in str(ei.value).lower() or "読" in str(ei.value)


def test_the_specific_guards_still_own_their_wording():
    """固有のガードを持つ扉は、その固有の診断で断られること (受け止めが勝たない)。

    判断記録は追記専用で取り消せない、という情報は汎用の文言には無い。背景の
    説明が受け止めの一般論に置き換わると、著者は「なぜここが特別なのか」を
    失う。
    """
    import cloud_write_guard
    import pytest as _pytest
    with _pytest.raises(cloud_write_guard.ProdWriteBlocked) as ei:
        _prod_client().record_decision("p-test", {"what": "x"})
    assert "append-only" in str(ei.value), (
        "判断記録の固有の診断 (追記専用) が汎用の文言に置き換わっています: "
        + str(ei.value))
