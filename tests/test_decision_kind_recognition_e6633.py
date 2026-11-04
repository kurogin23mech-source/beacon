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
    #
    # ms-185 e-7070: 例を "halt" から "scope-approval" に替えた。halt は共同作業
    # 空間と一緒に引退して「見覚えのある種別」から外れたので、例に使うと**この
    # 試験が測りたいのと逆の分岐** (= 未知の種別だから警告する) を通ってしまう。
    # 測っているのは「既知かつ 0 件なら黙る」ことなので、例は常に既知側の種別で
    # なければならない。
    _env(monkeypatch, kind="scope-approval")
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

# (種別, 既存データの有無) → (宣言済か, 疑うべきか, 人間向けに警告が出るか, 引退か)
#
# ms-185 e-7320 (F1): 最後の列 (= 引退か) を足した。引退種別の開示を実装した初版は
# 辞書の membership と認識集合だけを固定していて、**その事実が実際の出力に届く経路を
# 1 つも測っていなかった**。独立レビュー 2 体が同じ穴を指摘し、実測で裏付けられた:
# 開示の 4 行 (--json の 2 field と text の ℹ 2 箇所) を丸ごと消しても、関連テスト
# 75 件は緑のまま、反転ハーネスも「8 件すべて期待どおり」を返した。つまり修正の目的
# そのものが消えても誰も気づかない状態だった。開示を足すなら、それが **利用者に届く
# 面** まで同じテーブルで往復させる。
_DISCLOSURE_CASES = [
    pytest.param("dispostion", False, False, True, True, False, id="未宣言-0件=疑う"),
    pytest.param("brand-new", True, False, False, False, False, id="未宣言-データ有=疑わない"),
    pytest.param("disposition", False, True, False, False, False, id="宣言済-0件=疑わない"),
    pytest.param("disposition", True, True, False, False, False, id="宣言済-データ有=疑わない"),
    pytest.param("pr-intent", False, True, False, False, False, id="導出種別-0件=疑わない"),
    pytest.param("triage", False, True, False, False, False, id="実行時命名-0件=疑わない"),
    # 引退種別: 綴りは正しいので疑わせてはならず、0 件の理由を別に開示する。
    pytest.param("trek-review", False, True, False, False, True, id="引退-0件=疑わず理由を出す"),
    pytest.param("halt", True, True, False, False, True, id="引退-データ有=記録を出しつつ引退も開示"),
]


def _rows_for(kind):
    return [{"decision_id": "dec-1", "kind": kind, "decision": "x",
             "decided_by": "autonomous-AI"}]


@pytest.mark.parametrize("kind,has_rows,in_vocab,suspect,warns,retired",
                         _DISCLOSURE_CASES)
def test_json_and_text_never_disagree(monkeypatch, capsys, kind, has_rows,
                                      in_vocab, suspect, warns, retired):
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
    # ms-185 e-7320 (F1): 引退の開示が --json に実際に届いていること。
    assert out["kind_retired"] is retired, out
    if retired:
        assert out["kind_retired_explanation"], (
            "引退と判定したのに説明文が空 — 読み手が種別の意味を解決できません")
    else:
        assert out["kind_retired_explanation"] == "", out

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
    # ms-185 e-7320 (F1): 引退の開示が text にも届いていること。件数の有無で分岐が
    # 2 つあるので、**両方の枝** をこのテーブルで通す (片方だけ直す誤りを捕まえる)。
    disclosed = "引退した種別" in text
    assert disclosed is retired, (
        "引退の開示が text に出ていません (または引退でないのに出ています): "
        + repr((kind, has_rows, text)))


def test_the_candidate_list_marks_which_kinds_are_retired(monkeypatch, capsys):
    """候補一覧で引退種別に印が付く (ms-185 e-7320 / F2)。

    この一覧は綴り違いを疑ったときに出すので、読み手には「書いてよい候補」に見える。
    無印で平らに並べると、産出する継ぎ目がもう無い種別を拾って新しい判断をそこに
    書いてしまう。**一覧から外す形は採らない** — 引退種別は過去の記録を引くための
    正しい綴りなので、綴りを直したい人の候補として必要 (外すと読む側の回復手段が消える)。
    """
    import decision_vocab as _dv

    _env(monkeypatch, kind="dispostion", json_mode=False)   # 本物の綴り違い = 一覧が出る
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    text = capsys.readouterr().out
    assert "見覚えのある種別:" in text, text
    for k in _dv.RETIRED_DECISION_KINDS:
        assert f"{k} (引退)" in text, (
            f"候補一覧の {k} に引退の印が付いていません: {text}")
    # 現役の種別には印が付かない (= 全部に付けて意味を失わせていない)。
    assert "disposition (引退)" not in text, text

    # --json 側は配列の形を変えず、別の名前で「どれが引退か」を運ぶ。
    _env(monkeypatch, kind="dispostion", json_mode=True)
    _cloud(monkeypatch, rows=[])
    cmd_decision.cmd_decision_list()
    out = json.loads(capsys.readouterr().out)
    assert all(isinstance(k, str) for k in out["recognized_kinds"]), (
        "recognized_kinds の要素の形を変えると既存の読み手が壊れます")
    assert set(out["retired_kinds"]) == set(_dv.RETIRED_DECISION_KINDS), out
    assert set(out["retired_kinds"]) <= set(out["recognized_kinds"]), out


def test_recording_a_retired_kind_is_disclosed_on_both_faces(monkeypatch, capsys):
    """引退種別で **新しい** 判断を書こうとしたら開示する (ms-185 e-7320 / F2 書き込み側)。

    読み side の印だけでは書き込みは止まらない。開示 (読み) と操作抑止 (書き) は
    別レイヤーなので、片方だけ閉じると同じ穴が残る。**拒否はしない** — 語彙は
    意図的に開いており (e-6633 の「拒否ではなく開示」)、誰かが新しい種別を書き
    始めた直後を塞がないため。だから『書けるが、引退だと告げられる』が正しい形。
    """
    import decision_vocab as _dv
    retired_kind = sorted(_dv.RETIRED_DECISION_KINDS)[0]

    class _RecClient:
        def record_decision(self, project_id, payload, **kw):
            return {"decision_id": "dec-x", "kind": payload.get("kind")}

    def _setup(json_mode):
        for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_WHAT",
                  "BEACON_DECISION_RATIONALE", "BEACON_DECISION_EVIDENCE",
                  "BEACON_DECISION_DECIDED_BY", "BEACON_JSON"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("BEACON_DECISION_KIND", retired_kind)
        monkeypatch.setenv("BEACON_DECISION_WHAT", "x")
        monkeypatch.setenv("BEACON_DECISION_EVIDENCE", "commit:deadbee")
        if json_mode:
            monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
        monkeypatch.setattr(cmd_decision, "_get_api_client",
                            lambda: (_RecClient(), {"project_id": "p1"}))

    _setup(json_mode=True)
    try:
        cmd_decision.cmd_decision_record()
    except SystemExit as exc:          # 記録自体が通らない環境なら開示も測れない
        pytest.skip(f"record 経路が走りませんでした (exit={exc.code})")
    out = json.loads(capsys.readouterr().out)
    assert out["kind_retired"] is True, out
    assert out["kind_retired_explanation"], out

    _setup(json_mode=False)
    cmd_decision.cmd_decision_record()
    text = capsys.readouterr().out
    assert "引退しています" in text, (
        "書き込み側で引退の開示が出ていません (読み側の印だけでは書き込みは"
        f"止まりません): {text}")


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
              "recognized_kinds", "kind_retired", "kind_retired_explanation",
              "retired_kinds"):
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
    # test-the-test 相当: 語彙 **以外** のソースを足している結合が load-bearing で
    # あることを示す。語彙だけで集合を作っていたら pr-intent / triage / 引退種別が
    # 落ちる。ms-185 e-7320 で 4 本目 (引退辞書) が入ったので、ここの列挙も揃える
    # ——「結合が効いているか」を測る唯一の場所がソースを数え落としていると、
    # 5 本目を足す人がここを見て『2 つだけ』と読む。
    recognized = cmd_decision._recognized_decision_kinds()
    vocabulary_only = set(dv.KNOWN_DECISION_KINDS)
    from_ledger = sorted(set(cl.DECISION_CAPTURE_DERIVED_KINDS)
                         | set(cl.DECISION_CAPTURE_ADHOC_KINDS))
    assert from_ledger, "台帳側の宣言セットが空 — この結合を検査できていません"
    retired = sorted(dv.RETIRED_DECISION_KINDS)
    assert retired, "引退辞書が空 — この結合を検査できていません"
    for k in from_ledger + retired:
        assert k in recognized, k
        assert k not in vocabulary_only, (
            f"{k} が語彙に昇格しました — 台帳側 / 引退辞書の宣言から外してください "
            f"(宣言が腐ります)")


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
