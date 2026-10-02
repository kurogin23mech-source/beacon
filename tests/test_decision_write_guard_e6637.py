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

# 負債台帳は tests/ 直下のデータファイル (このテストの隣) に置く。Python の定数に
# 53 件を埋め込むとレビュー差分で本体が読めなくなるため。
_DEBT_FILE = os.path.join(os.path.dirname(__file__), "_unguarded_api_doors.json")
_RECORDED_UNGUARDED_DOORS = frozenset(
    json.load(open(_DEBT_FILE, encoding="utf-8")))

_WRITE_VERBS = frozenset({"post", "put", "delete", "patch"})


def _called_names(fn: ast.AST) -> set:
    out = set()
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            f = sub.func
            out.add(getattr(f, "attr", None) or getattr(f, "id", ""))
    return out


def api_client_write_doors():
    """``({扉: ガード済みか}, [guard helper 名])`` を構文木から求める。"""
    tree = ast.parse(open(os.path.join(_LIB, "api_client.py"), encoding="utf-8").read())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ApiClient")
    methods = {fn.name: _called_names(fn) for fn in cls.body
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    helpers = {n for n, c in methods.items()
               if n.startswith("_guard") and any(x.startswith("guard_prod") for x in c)}

    def guarded(c):
        return any(x.startswith("guard_prod") for x in c) or bool(c & helpers)

    doors = {n: guarded(c) for n, c in methods.items()
             if (c & _WRITE_VERBS) and not n.startswith("_guard")}
    return doors, sorted(helpers)


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


def test_the_guard_detection_is_not_vacuous():
    # 検出器が「全部ガード済み」や「扉ゼロ」を返して無条件に緑になる形を防ぐ。
    doors, helpers = api_client_write_doors()
    assert len(doors) >= 40, "扉の検出が少なすぎます: " + repr(len(doors))
    guarded = {n for n, ok in doors.items() if ok}
    assert {"post_bus_event", "create_project", "record_decision"} <= guarded, (
        "既知のガード済み扉を検出できていません: " + repr(sorted(guarded)))
    assert "_guard_bus_write" in helpers and "_guard_decision_write" in helpers
