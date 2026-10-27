"""削除前の確認が、消す量と対象を正しく述べること (ms-178 / e-7174).

本番データで `beacon note reap` の下見を走らせて見つけた (2026-10-27)。実測:

    Refusing to clear 49 session note(s) ...   ← 冒頭
      孤児 (どの稼働中セッションにも属さない): 175 件   ← 内訳

local ファイルに孤児 49 件、共有 cloud ストアに 175 件。重複除去すると実際に
消えるのは 175 件なのに、ゲートは先に読んだストアの数だけを出していた —— 3.5 倍の
過少申告。その 2 行上のコメント自身が「賭け金を隠す確認ゲートはゲートではない」と
書いている。

もう 1 つ、cloud の 1 行が `note reap` では逆のことを言っていた:
「対象はこのセッションが書いたメモだけです」。reap の対象は **終了した他セッション**
のメモである。`note clear` のために書かれた文を reap が共用し、scope が追随して
いなかった形で、この同じファイルが別件 (AX-2) として既に注記している型と同型。

だから固定するのは 2 つ: **数は 2 ストアを重複除去した正味**、**対象は呼び出し側が
自分の言葉で名乗る** (既定文を置くと、まさに今回のように 2 つの verb が 1 文を共用して
片方で偽になる)。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_note  # noqa: E402

# 本番で実測した形。local にも cloud にも在る 1 件 (= note add は両方に書く) を
# 必ず含める: 重複が無いと「足し算」と「重複除去」が同じ数になり、試験が何も
# 見ていないことになる。
BOTH = {"ts": "2026-10-01T00:00:00+0900", "text": "in both stores",
        "session_id": "sv-DEAD"}
LOCAL_ONLY = {"ts": "2026-10-02T00:00:00+0900", "text": "local only",
              "session_id": "sv-DEAD"}
CLOUD_ONLY = {"ts": "2026-10-03T00:00:00+0900", "text": "cloud only",
              "session_id": "sv-DEAD"}
KEPT = {"ts": "2026-10-04T00:00:00+0900", "text": "live session's",
        "session_id": "sv-LIVE"}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """local に 3 件 / cloud に 3 件、重複 1 件。孤児は正味 3 件。"""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}',
                                         encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.chdir(tmp_path)
    (beacon / "session_notes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n"
                for r in (BOTH, LOCAL_ONLY, KEPT)), encoding="utf-8")
    monkeypatch.setattr(cmd_note, "_cloud_project_id", lambda: "proj-1")
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: ([BOTH, CLOUD_ONLY, KEPT], ""))
    return beacon


def _doomed(note):
    return (note.get("session_id") or "") == "sv-DEAD"


def _refuse(scope_sentence="このテストの対象"):
    """確認なしの削除を 1 回試みる。

    この経路は「拒否」なので非 0 で終了する (= 成功した下見ではない、という
    e-6654 以来の契約)。だから SystemExit を受け止める必要がある — 受け止めずに
    assert を書くと、assert に到達しないまま SystemExit で落ち、失敗の理由が
    「出力が違う」ではなく「例外」になって読めなくなる。
    """
    with pytest.raises(SystemExit):
        cmd_note._selective_delete(
            _doomed, label="テスト対象", write_backups=lambda *a: "b",
            confirm=False, rerun_hint="h",
            on_unconfirmed=cmd_note.UNCONFIRMED_REFUSE,
            touches_other_sessions=False, scope_sentence=scope_sentence)


# --- 件数: 正味いくつ消えるか -------------------------------------------------

def test_headline_count_is_the_net_not_one_store(stores, capsys):
    """冒頭の件数が、重複除去後の正味 (= 実際に消える数) であること。

    local 2 件 / cloud 2 件 / 重複 1 件なので、足し算なら 4、正味は 3。
    直す前はさらに悪く「local の 2 件」だけを出していた。
    """
    _refuse()
    err = capsys.readouterr().err
    assert "Refusing to clear 3 session note(s)" in err, err
    assert "clear 4 session" not in err, "2 ストアを足して重複を二重に数えている"
    assert "clear 2 session" not in err, "片方のストアの数しか出していない"


def test_per_store_counts_are_still_shown(stores, capsys):
    """内訳として各ストアの数も残す (正味だけだと退避先の規模が分からない)。"""
    _refuse()
    err = capsys.readouterr().err
    assert "の 2 件" in err, err      # local leg
    assert "2 件。退避は" in err, err  # cloud leg


def test_net_equals_distinct_doomed_notes(stores, capsys):
    """正味の数が「実際に消える別個のメモ」と一致すること。

    数字を直接書くのではなく、ストアの中身から導いて突き合わせる。fixture を
    増やしても試験が嘘にならない。
    """
    local = cmd_note._read_local_notes(str(stores / "session_notes.jsonl"))
    cloud, _ = cmd_note._fetch_cloud_notes("proj-1")
    expected = len({cmd_note._note_key(n)
                    for n in list(local) + list(cloud) if _doomed(n)})
    _refuse()
    err = capsys.readouterr().err
    assert "Refusing to clear %d session note(s)" % expected in err, (expected, err)


# --- 対象: 呼び出し側が自分の言葉で名乗る ------------------------------------

def test_cloud_line_uses_the_callers_scope_sentence(stores, capsys):
    """cloud の 1 行が、呼び出し側が渡した scope をそのまま述べること。"""
    _refuse(scope_sentence="対象は終了したセッションが書いたメモだけです")
    err = capsys.readouterr().err
    assert "対象は終了したセッションが書いたメモだけです" in err, err


def test_scoped_caller_without_a_scope_sentence_is_refused(stores):
    """scope を名乗らない scoped 呼び出しは構造的に通らない。

    既定文を置くと、まさに今回のように 2 つの verb が 1 文を共用して片方で偽に
    なる。既定を無くして、呼び出し側に言わせる。
    """
    with pytest.raises(ValueError, match="scope_sentence"):
        cmd_note._selective_delete(
            _doomed, label="x", write_backups=lambda *a: "b", confirm=False,
            rerun_hint="h", on_unconfirmed=cmd_note.UNCONFIRMED_REFUSE,
            touches_other_sessions=False)


def test_the_two_real_callers_say_different_things(stores):
    """`note clear` と `note reap` が違う scope を名乗っていること。

    同じ文を渡していたら、どちらかで偽になる (それが e-7174 の defect)。
    実際の呼び出しを構文木で読んで、2 つが別の文字列を渡していることを見る。
    """
    import ast

    tree = ast.parse((ROOT / "lib" / "cmd_note.py").read_text(encoding="utf-8"))
    sentences = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_selective_delete"):
            for kw in node.keywords:
                if kw.arg == "scope_sentence":
                    sentences.append(ast.literal_eval(kw.value))
    assert len(sentences) == 2, sentences
    assert len(set(sentences)) == 2, (
        "2 つの verb が同じ scope 文を共用している (片方で偽になる): %s" % sentences)
    joined = " ".join(sentences)
    assert "このセッション" in joined and "終了したセッション" in joined, sentences


def test_reap_names_ended_sessions_end_to_end(stores, monkeypatch, capsys):
    """`cmd_note_reap` を実際に走らせて、cloud の 1 行が reap の対象を述べること。

    呼び出し側が何を渡しているかではなく、出力に何が出たかで見る。
    """
    import session as S
    monkeypatch.setattr(cmd_note, "_resolve_session_id", lambda: "sv-LIVE")
    monkeypatch.setattr(S, "read_session", lambda: {"session_id": "sv-LIVE"})
    monkeypatch.setattr(S, "read_all_bridge_claims", lambda: [])

    class _Client:
        def list_sessions(self, pid, **kw):
            return [{"session_id": "sv-LIVE"}]

    monkeypatch.setattr(cmd_note, "_note_api_client", lambda: (_Client(), ""))
    monkeypatch.delenv("BEACON_NOTE_REAP_YES", raising=False)

    with pytest.raises(SystemExit):
        cmd_note.cmd_note_reap()
    err = capsys.readouterr().err
    assert "終了したセッション" in err, err
    assert "このセッションが書いたメモだけ" not in err, (
        "reap が note clear 用の文面を出している: %s" % err)
