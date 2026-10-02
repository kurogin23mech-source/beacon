"""存在しない種別名で判断記録を絞り込んだとき、黙って 0 件を返さない (ms-166 e-6633)。

## なぜ要るか

``beacon decision list --kind <名前>`` は、渡された種別名を無検証でそのままサーバの
絞り込みに流していた。綴りを間違えた人 (または AI) には **0 件** が返る。受け取った側は
「この種別の判断は 1 件も記録されていない」と読むが、実際には **問い合わせ自体が的を
外していた**。これは e-6603 が直した「通信ログを既定除外した結果 0 件なのに『判断記録が
無い』と読める」のと同型で、0 件という答えに 2 つの意味が潰れている。

## 拒否ではなく開示にした理由

種別の語彙は **意図的に開いている** (server/decision_event.py 冒頭の設計方針:
``build_decision_event`` は未知の kind も受け付け、空 kind だけを拒否する)。だから
許可リストで弾くと、誰かが新しい種別を書き始めた直後にその記録が読めなくなる。
ここでは「見覚えのある種別に無い」ことを **開示するだけ** で、問い合わせは通す。

## 偽警告を出さないことが設計の要点

既知集合を ``KNOWN_DECISION_KINDS`` だけで作ると、本番に実在する ``pr-intent`` (PR の
意図から導出) と ``triage`` (``beacon decision record`` で名付けられた) を「知らない
種別」と言ってしまい、**警告そのものが嘘になる**。e-6756 で捕獲台帳に載せた
「導出種別」「実行時に名付けられた種別」をここで消費して集合を作る。
この結合が効いていることを ``test_every_kind_written_in_code_is_recognized`` が
機械で固定する (= 台帳側に種別が増えたとき、警告側が置いていかれない)。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import capability_ledger as cl      # noqa: E402
import cmd_decision                 # noqa: E402
import decision_event as de         # noqa: E402
import decision_vocab as dv         # noqa: E402


class _ListClient:
    def __init__(self, rows=None):
        self._rows = rows or []

    def list_decisions(self, project_id, *, kind="", limit=100, since="",
                       session="", target=""):
        rows = [r for r in self._rows if not kind or r.get("kind") == kind]
        return {"decisions": rows, "count": len(rows)}


def _cloud(monkeypatch, rows=None):
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (_ListClient(rows), {"project_id": "p1"}))


def _env(monkeypatch, *, kind=None, json_mode=False):
    for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_LIMIT",
              "BEACON_DECISION_SESSION", "BEACON_DECISION_TARGET", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    if kind is not None:
        monkeypatch.setenv("BEACON_DECISION_KIND", kind)
    if json_mode:
        monkeypatch.setenv("BEACON_JSON", "1")


# ---------------------------------------------------------------------------
# 1. 人間向け出力 — 0 件の意味が潰れない
# ---------------------------------------------------------------------------

def test_unrecognized_kind_with_zero_rows_says_the_query_may_be_off(monkeypatch, capsys):
    _env(monkeypatch, kind="dispostion")   # disposition の綴り違い
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    out = capsys.readouterr().out
    assert "(決定なし)" in out
    assert "dispostion" in out, out
    assert "見覚えのある種別に含まれていません" in out, out
    # 次の一手として正しい綴りが読めること (AX 原則: エラーは次の一手を示す)。
    assert "disposition" in out, out


def test_recognized_kind_with_zero_rows_does_not_warn(monkeypatch, capsys):
    # 既知の種別で 0 件なら「その種別の判断が無い」が正しい答え。警告を出すと
    # 常に出る警告になり、読み手が見なくなる。
    _env(monkeypatch, kind="halt")
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    out = capsys.readouterr().out
    assert "(決定なし)" in out
    assert "見覚えのある種別" not in out, out


def test_unrecognized_kind_with_rows_does_not_warn(monkeypatch, capsys):
    # データが実在を証明している場合は黙る (語彙は開いているので、新しい種別を
    # 誰かが書き始めた直後という正当なケースがある)。
    _env(monkeypatch, kind="brand-new")
    _cloud(monkeypatch, rows=[{"decision_id": "dec-1", "kind": "brand-new",
                               "decision": "x", "decided_by": "autonomous-AI"}])
    cmd_decision.cmd_decision_list()
    out = capsys.readouterr().out
    assert "dec-1" in out
    assert "見覚えのある種別" not in out, out


def test_no_kind_filter_warns_nothing(monkeypatch, capsys):
    _env(monkeypatch)
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    assert "見覚えのある種別" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. 機械向け出力 — text と食い違えないこと (独立 AX レビュー PR#783 の AX-1)
# ---------------------------------------------------------------------------
#
# 初版は「語彙に宣言済か」という生の事実を kind_recognized という 1 つの可否に見える
# 名前で --json に常時載せ、人間向けには 0 件のときだけ警告していた。結果、宣言に
# 無いが実データがある種別で「kind_recognized: false なのに decisions が非空」という
# 食い違いが起きた。自動化経路の読み手は正しいデータを疑い、綴りを直そうと再試行し
# うる (この修正が防ごうとした誤診の、ちょうど裏返し)。
#
# 直した形: 2 つの別の事実を別の名前で出す。
#   kind_in_known_vocabulary — 宣言済か (生の事実)
#   kind_filter_suspect      — この応答を疑うべきか (= 宣言に無く かつ 0 件)
# 人間向けの警告は後者と同じ信号から出すので、2 つの面は食い違えない。

# (種別, 既存データの有無) → (宣言済か, 疑うべきか, 人間向けに警告が出るか)
_DISCLOSURE_CASES = [
    pytest.param("dispostion", False, False, True, True, id="未宣言-0件=疑う"),
    pytest.param("brand-new", True, False, False, False, id="未宣言-データ有=疑わない"),
    pytest.param("disposition", False, True, False, False, id="宣言済-0件=疑わない"),
    pytest.param("disposition", True, True, False, False, id="宣言済-データ有=疑わない"),
    pytest.param("pr-intent", False, True, False, False, id="導出種別-0件=疑わない"),
    pytest.param("triage", False, True, False, False, id="実行時命名-0件=疑わない"),
]


def _rows_for(kind):
    return [{"decision_id": "dec-1", "kind": kind, "decision": "x",
             "decided_by": "autonomous-AI"}]


@pytest.mark.parametrize("kind,has_rows,in_vocab,suspect,warns", _DISCLOSURE_CASES)
def test_json_and_text_never_disagree(monkeypatch, capsys, kind, has_rows,
                                      in_vocab, suspect, warns):
    rows = _rows_for(kind) if has_rows else []

    _env(monkeypatch, kind=kind, json_mode=True)
    _cloud(monkeypatch, rows=rows)
    cmd_decision.cmd_decision_list()
    out = json.loads(capsys.readouterr().out)
    assert out["kind_filter"] == kind
    assert out["kind_in_known_vocabulary"] is in_vocab, out
    assert out["kind_filter_suspect"] is suspect, out
    assert isinstance(out["recognized_kinds"], list) and out["recognized_kinds"]
    # 既存の形 (decisions / count) を壊していないこと。
    assert out["count"] == len(rows)

    _env(monkeypatch, kind=kind, json_mode=False)
    _cloud(monkeypatch, rows=rows)
    cmd_decision.cmd_decision_list()
    text = capsys.readouterr().out
    warned = "見覚えのある種別に含まれていません" in text
    assert warned is warns, (kind, has_rows, text)
    # 2 つの面が同じ信号から出ていること = 疑うべきときだけ両方が言う。
    assert warned is suspect, (
        "text と --json が食い違っています (AX-1 の回帰): "
        "text warned=" + repr(warned) + " / json suspect=" + repr(suspect))


def test_json_does_not_report_data_as_suspect(monkeypatch, capsys):
    """AX-1 の回帰ピン: 実データがある種別を「疑え」と言ってはならない。

    データ自身がその種別の実在を証明しているのに疑いの信号を立てると、自動化経路が
    正しい応答を捨てたり、綴りを直す無駄な再試行を回したりする。
    """
    _env(monkeypatch, kind="brand-new", json_mode=True)
    _cloud(monkeypatch, rows=_rows_for("brand-new"))
    cmd_decision.cmd_decision_list()
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["kind_filter_suspect"] is False, out
    # 生の事実は正直に出す (宣言は無い) — 疑いの信号とは別の名前で。
    assert out["kind_in_known_vocabulary"] is False, out


def test_json_omits_the_fields_when_no_kind_filter(monkeypatch, capsys):
    # 既定の出力を太らせない (--kind を使っていない読み手には無関係な情報)。
    _env(monkeypatch, json_mode=True)
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    out = json.loads(capsys.readouterr().out)
    for f in ("kind_filter", "kind_in_known_vocabulary", "kind_filter_suspect",
              "recognized_kinds"):
        assert f not in out, out


# ---------------------------------------------------------------------------
# 3. 偽警告が出ないことを機械で固定する (この修正の要点)
# ---------------------------------------------------------------------------

def test_every_kind_written_in_code_is_recognized():
    """コードが書いている全種別が既知集合に入っていること。

    e-6756 の抽出器を再利用する (= 抽出器の真値源を 2 つにしない)。これが落ちる
    ときは「警告が実在する種別を知らない種別と呼ぶ」状態なので、警告が嘘になる。
    """
    from test_decision_capture_coverage_ms166 import kinds_written_in_code
    recognized = cmd_decision._recognized_decision_kinds()
    unrecognized = sorted(k for k in kinds_written_in_code() if k not in recognized)
    assert not unrecognized, (
        "コードが書いているのに『見覚えのある種別』に入っていない種別があります "
        f"(--kind でそれを引いた人に偽の警告が出ます): {unrecognized}")


def test_recognized_set_consumes_the_ledger_not_only_the_vocabulary():
    # test-the-test 相当: 台帳の 2 セットを足している結合が load-bearing であることを
    # 示す。語彙だけで集合を作っていたら pr-intent / triage が落ちる。
    recognized = cmd_decision._recognized_decision_kinds()
    vocabulary_only = set(dv.KNOWN_DECISION_KINDS)
    from_ledger = sorted(set(cl.DECISION_CAPTURE_DERIVED_KINDS)
                         | set(cl.DECISION_CAPTURE_ADHOC_KINDS))
    assert from_ledger, "台帳側の宣言セットが空 — この結合を検査できていません"
    for k in from_ledger:
        assert k in recognized, k
        assert k not in vocabulary_only, (
            f"{k} が語彙に昇格しました — 台帳側の宣言から外してください (宣言が腐ります)")


# ---------------------------------------------------------------------------
# 4. 語彙の移設が単一ソースを壊していないこと
# ---------------------------------------------------------------------------

def test_vocabulary_has_one_source_across_cli_and_server():
    # e-6633 で KNOWN_DECISION_KINDS を lib/decision_vocab.py に移した (CLI が
    # server/ を import 経路に持たないため)。server 側は再エクスポートで、
    # 2 つに分かれていないことを固定する (DECIDED_BY が e-5652 で通った道と同じ)。
    assert de.KNOWN_DECISION_KINDS is dv.KNOWN_DECISION_KINDS
    assert de.DECISION_KINDS is dv.KNOWN_DECISION_KINDS
    assert cmd_decision._KNOWN_KINDS is dv.KNOWN_DECISION_KINDS
