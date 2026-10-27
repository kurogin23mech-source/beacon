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

# 本番で実測した形。土台は 3 つの性質を同時に持たせる必要がある:
#
# 1. local にも cloud にも在る 1 件 (= note add は両方に書く) — 無いと
#    「足し算」と「store 間の重複除去」が同じ数になり、何も見ていない試験になる。
# 2. **local と cloud の孤児数が非対称** — 最初の版は両方 2 件で対称だったため、
#    cloud 行の件数を len(local_go) に取り違える変異が 7 件すべて緑のまま通った
#    (AX / 保守性の 2 体が独立に同じ穴を指摘、2026-10-27)。
# 3. **同一 store 内の重複行** — 鍵 (ts, text, session_id) が同じ行が 2 行ある
#    状態。集合で数えると 1 件に潰れるのに削除は 2 行消えるので、ここが無いと
#    「鍵の集合」実装の過少申告を見逃す (AX high finding)。
BOTH = {"ts": "2026-10-01T00:00:00+0900", "text": "in both stores",
        "session_id": "sv-DEAD"}
LOCAL_ONLY = {"ts": "2026-10-02T00:00:00+0900", "text": "local only",
              "session_id": "sv-DEAD"}
CLOUD_ONLY_A = {"ts": "2026-10-03T00:00:00+0900", "text": "cloud only A",
                "session_id": "sv-DEAD"}
CLOUD_ONLY_B = {"ts": "2026-10-05T00:00:00+0900", "text": "cloud only B",
                "session_id": "sv-DEAD"}
CLOUD_ONLY_C = {"ts": "2026-10-06T00:00:00+0900", "text": "cloud only C",
                "session_id": "sv-DEAD"}
KEPT = {"ts": "2026-10-04T00:00:00+0900", "text": "live session's",
        "session_id": "sv-LIVE"}

# local: BOTH, LOCAL_ONLY, KEPT          → 孤児 2 行
# cloud: BOTH, CLOUD_ONLY_A/B/C, KEPT    → 孤児 4 行
# 正味 = local 2 行 + cloud だけの 3 行 = 5 件 (local_go=2 ≠ cloud_go=4 で非対称)
EXPECTED_NET = 5


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """local の孤児 2 行 / cloud の孤児 4 行、重複 1 件。正味 5 件。"""
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
    monkeypatch.setattr(
        cmd_note, "_fetch_cloud_notes",
        lambda pid: ([BOTH, CLOUD_ONLY_A, CLOUD_ONLY_B, CLOUD_ONLY_C,
                      KEPT], ""))
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
            scope_sentence=scope_sentence)


# --- 件数: 正味いくつ消えるか -------------------------------------------------

def test_headline_count_is_the_net_not_one_store(stores, capsys):
    """冒頭の件数が、ストア間の重複を除いた正味 (= 実際に消える数) であること。

    local の孤児 2 行 / cloud の孤児 4 行 / 重複 1 件なので、足し算なら 6、
    正味は 5。直す前はさらに悪く「local の 2 件」だけを出していた。
    """
    _refuse()
    err = capsys.readouterr().err
    assert "Refusing to clear %d session note(s)" % EXPECTED_NET in err, err
    assert "clear 6 session" not in err, "2 ストアを足して重複を二重に数えている"
    assert "clear 2 session" not in err, "local の数しか出していない"
    assert "clear 4 session" not in err, "cloud の数しか出していない"


def test_per_store_counts_are_still_shown(stores, capsys):
    """内訳として各ストアの数も残し、**取り違えていない**こと。

    土台を非対称 (local 2 / cloud 4) にしてあるので、cloud 行に local の数を
    渡す取り違えはここで必ず落ちる。対称な土台だったとき、この変異は 7 件
    すべて緑のまま通っていた (独立 2 体が同じ穴を指摘)。
    """
    _refuse()
    err = capsys.readouterr().err
    assert "の 2 件 (moved to" in err, "local 行が local の件数でない: %s" % err
    assert "— 4 件。退避は" in err, "cloud 行が cloud の件数でない: %s" % err


def test_net_equals_distinct_doomed_notes(stores, capsys):
    """正味の数が「実際に消える別個のメモ」と一致すること。

    数字を直接書くのではなく、ストアの中身から導いて突き合わせる。fixture を
    増やしても試験が嘘にならない。
    """
    local = cmd_note._read_local_notes(str(stores / "session_notes.jsonl"))
    cloud, _ = cmd_note._fetch_cloud_notes("proj-1")
    # オラクルを production の `_note_key` から作らない (保守性レビュー
    # 2026-10-27: それでは「dedup が _note_key を使っているか」しか見ず、
    # `_note_key` 自体が劣化しても気づかない tautology になる)。土台は
    # こちらが書いた既知の行なので、(ts, text) の組を自前で数える。
    # `_note_key` の 3 フィールド構成自体は
    # tests/test_note_list_truth_source_e6655.py が独立に固定している。
    def _own_key(n):
        return (n.get("ts"), n.get("text"), n.get("session_id"))

    local_doomed = [n for n in local if _doomed(n)]
    local_keys = {_own_key(n) for n in local}
    expected = len(local_doomed) + sum(
        1 for n in cloud if _doomed(n) and _own_key(n) not in local_keys)
    assert expected == EXPECTED_NET, (expected, EXPECTED_NET)

    _refuse()
    err = capsys.readouterr().err
    assert "Refusing to clear %d session note(s)" % expected in err, (expected, err)


def test_duplicate_rows_in_one_store_are_counted_as_rows(stores, capsys):
    """同一 store 内の重複行を 1 件に潰さない。

    `_note_key` は (ts, text, session_id) なので、同じ行が local に 2 行ある
    状態 (同じ秒に note add が二重実行された / 再送) は鍵としては 1 つ。鍵の
    集合で数えると 1 件と出るのに、削除は 2 行とも消える —— 直したはずの
    過少申告が別の入力で再発していた (AX レビューが実機で再現、2026-10-27)。

    ストア間の重複は相殺し、ストア内の重複は数える。方向が逆なので 1 つの
    集合演算では両立しない。
    """
    path = stores / "session_notes.jsonl"
    path.write_text(path.read_text(encoding="utf-8")
                    + json.dumps(LOCAL_ONLY, ensure_ascii=False) + "\n",
                    encoding="utf-8")  # LOCAL_ONLY が local に 2 行
    _refuse()
    err = capsys.readouterr().err
    assert "Refusing to clear %d session note(s)" % (EXPECTED_NET + 1) in err, (
        "store 内の重複行が潰れている: %s" % err)


def test_duplicate_rows_really_do_all_disappear(stores, capsys):
    """上の数が実態と合っていること — 実際に削除して行数で確かめる。

    「表示が増えた」だけでは、表示を増やしただけかもしれない。消した後の
    ファイルを数えて、重複行が両方消えていることまで見る。
    """
    path = stores / "session_notes.jsonl"
    path.write_text(path.read_text(encoding="utf-8")
                    + json.dumps(LOCAL_ONLY, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    before = len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])

    removed = cmd_note._selective_delete(
        _doomed, label="テスト対象", write_backups=lambda *a: "b",
        confirm=True, rerun_hint="h",
        on_unconfirmed=cmd_note.UNCONFIRMED_REFUSE,
        scope_sentence="このテストの対象")
    capsys.readouterr()

    after = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert before == 4 and len(after) == 1, (before, after)
    assert all(not _doomed(n) for n in after), after
    assert removed >= EXPECTED_NET + 1, removed


# --- cloud が読めないとき: 0 件と言わない -------------------------------------

def test_unreadable_cloud_is_not_reported_as_zero(stores, monkeypatch, capsys):
    """cloud を読めないとき「0 件」と言わない。

    `_fetch_cloud_notes` は失敗時に [] を返すので、そこから derive した数は
    「見られなかった」と「見たが空だった」の両方で 0 になる。同関数自身の
    docstring が『unreadable を empty と混同するのが defect そのもの』と
    書いているのに、私が新設した数値表示がそれを再発させていた
    (AX レビューがエラーを注入して再現、2026-10-27)。
    """
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: ([], "HTTPError: 500"))
    _refuse()
    err = capsys.readouterr().err
    assert "0 件" not in err, "読めない cloud を 0 件と報告した: %s" % err
    assert "件数不明" in err, err


def test_unreadable_cloud_headline_says_it_is_a_floor(stores, monkeypatch, capsys):
    """読めないときの冒頭の数は「少なくとも」と明示されること。

    素の数字を出すと、読み手は総数を知ったと受け取る。床であることを言えば
    待つ判断ができる。
    """
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: ([], "HTTPError: 500"))
    _refuse()
    err = capsys.readouterr().err
    assert "at least 2 session note(s)" in err, err   # local の孤児 2 行
    assert "総数は不明" in err, err


def test_readable_but_empty_cloud_still_says_zero(stores, monkeypatch, capsys):
    """逆方向: 本当に空のときは 0 件と言う (一律に件数不明にしていない)。"""
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))
    _refuse()
    err = capsys.readouterr().err
    assert "0 件" in err, err
    assert "件数不明" not in err, err


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
    import inspect
    sig = inspect.signature(cmd_note._selective_delete)
    assert "touches_other_sessions" not in sig.parameters, (
        "scope を 2 引数で表すと (False, \"\") という無効な状態が signature 上"
        "構築できてしまう。1 引数に畳んだはず")
    assert sig.parameters["scope_sentence"].default is None


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
