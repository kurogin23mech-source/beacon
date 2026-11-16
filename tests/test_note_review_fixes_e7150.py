"""独立レビュー (2026-10-26) の指摘を固定する (ms-178 / e-7150).

AX と保守性の 2 体が別々に走って挙げた指摘のうち、挙動として固定できるものをここに
集める。どれも「走らせて初めて見えた」類で、差分を読むだけでは出なかった。

共通する型が 1 つある: **宣言した不変条件が、実際には 1 経路しか守っていない**。
- 空振りの案内 (AX-2): `touches_other_sessions=False` を渡しているのに、空振り経路が
  その引数を読まず、`--include-other-sessions` を勧めていた。`reap` が存在する理由を
  `reap` 自身が勧める形。
- fail-closed (M-3、別 module で固定): docstring は「生存一覧が取れなければ中止する」
  と宣言していたのに、local の源だけ `except: pass` だった。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_note  # noqa: E402
import session as S  # noqa: E402

UNSAFE_VERB = "--include-other-sessions"


@pytest.fixture
def project(tmp_path, monkeypatch):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}',
                                         encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.chdir(tmp_path)
    for var in ("BEACON_NOTE_REAP_YES", "BEACON_NOTE_REAP_UNTAGGED",
                "BEACON_NOTE_REAP_SINCE_MIN", "BEACON_NOTE_CLEAR_YES",
                "BEACON_NOTE_CLEAR_ALL", "BEACON_JSON",
                "BEACON_NOTE_LIST_ALL", "BEACON_NOTE_LIST_LIMIT",
                "BEACON_NOTE_LIST_SINCE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cmd_note, "_resolve_session_id", lambda: "sv-ME")
    monkeypatch.setattr(S, "read_session", lambda: {"session_id": "sv-ME"})
    monkeypatch.setattr(S, "read_all_bridge_claims", lambda: [])
    return beacon


def _seed(beacon, rows):
    (beacon / "session_notes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")


# --- AX-2: 安全な口が危険な口を勧めない ---------------------------------------

def test_reap_empty_path_does_not_recommend_the_unsafe_verb(project, capsys):
    """刈るものが無いとき、`--include-other-sessions` を勧めない。

    勧めていた。`reap` の存在理由は「あの旗を使わずに片付けられること」なので、
    自分の空振り経路でその旗を勧めるのは設計の裏切りで、しかも従った利用者は
    稼働中セッションの引き継ぎメモを失う。AX レビューが実機で踏んだ。
    """
    _seed(project, [{"ts": "2026-10-25T00:00:00+00:00", "text": "mine",
                     "session_id": "sv-ME"}])
    import os
    os.environ["BEACON_NOTE_REAP_YES"] = "1"
    try:
        cmd_note.cmd_note_reap()
    finally:
        os.environ.pop("BEACON_NOTE_REAP_YES", None)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert UNSAFE_VERB not in combined, combined


def test_scoped_clear_empty_path_does_not_recommend_the_unsafe_verb(
        project, capsys):
    """`note clear` の空振り経路も同じ (こちらは差分より前から在った穴)。

    同じ `total == 0` 分岐を共有しており、`touches_other_sessions=False` を
    渡しているのは `clear` も同じ。1 箇所直せば両方閉じる形にした。
    """
    _seed(project, [{"ts": "2026-10-25T00:00:00+00:00", "text": "other",
                     "session_id": "sv-OTHER"}])
    import os
    os.environ["BEACON_NOTE_CLEAR_YES"] = "1"
    try:
        cmd_note.cmd_note_clear()
    finally:
        os.environ.pop("BEACON_NOTE_CLEAR_YES", None)
    combined = "".join(capsys.readouterr())
    assert UNSAFE_VERB not in combined, combined


def test_the_unsafe_verb_is_still_recommended_where_it_applies(project, capsys):
    """逆方向: 全セッション分を対象にする呼び出しでは案内が残る。

    「危険な旗の名前を出さない」ことを目的にすると、本当に必要な場面でも
    案内が消えて利用者が詰む。消したのは **scope が否定している呼び出しだけ**
    であることを固定する (= 一律の禁止と区別する)。
    """
    _seed(project, [{"ts": "2026-10-25T00:00:00+00:00", "text": "other",
                     "session_id": "sv-OTHER"}])
    import os
    # 全セッション分を対象にする呼び出し (= 旧来の全件 clear) を直接叩く。
    # ms-178 e-7174 で scope は 1 引数に畳まれた: scope_sentence を渡さない
    # (= None) ことが「全セッション横断」の意味になる。
    removed = cmd_note._selective_delete(
        lambda n: False, label="テスト", write_backups=lambda *a: "b",
        confirm=True, rerun_hint="h",
        on_unconfirmed=cmd_note.UNCONFIRMED_REFUSE)
    del removed, os
    combined = "".join(capsys.readouterr())
    assert UNSAFE_VERB in combined, combined


def test_remaining_notes_are_not_called_other_sessions(project, capsys):
    """残ったメモを「他セッション分」と呼ばない。

    その束には自セッションのメモと未記録のメモも入る。数は合っていても
    中身の説明が嘘だと、利用者は「他人の分が溜まっている」と誤解する。
    """
    _seed(project, [{"ts": "2026-10-25T00:00:00+00:00", "text": "mine",
                     "session_id": "sv-ME"},
                    {"ts": "2026-10-25T00:00:01+00:00", "text": "untagged"}])
    import os
    os.environ["BEACON_NOTE_REAP_YES"] = "1"
    try:
        cmd_note.cmd_note_reap()
    finally:
        os.environ.pop("BEACON_NOTE_REAP_YES", None)
    combined = "".join(capsys.readouterr())
    assert "他セッション分" not in combined, combined
    assert "2 件" in combined, combined


# --- AX-3: local mode で効かないフラグは黙って捨てない -------------------------

def test_liveness_window_in_local_mode_says_it_has_no_effect(project, capsys):
    """`--since-min` は local では効かないので、そう言う。

    受理して解析して黙って捨てていた。遅いセッションを守るつもりで窓を広げた
    利用者は、守れていないことを知る手段が無かった。
    """
    _seed(project, [{"ts": "2026-10-01T00:00:00+00:00", "text": "orphan",
                     "session_id": "sv-DEAD"}])
    import os
    os.environ["BEACON_NOTE_REAP_SINCE_MIN"] = "99"
    os.environ["BEACON_NOTE_REAP_YES"] = "1"
    try:
        cmd_note.cmd_note_reap()
    finally:
        os.environ.pop("BEACON_NOTE_REAP_SINCE_MIN", None)
        os.environ.pop("BEACON_NOTE_REAP_YES", None)
    combined = "".join(capsys.readouterr())
    assert "--since-min" in combined and "ローカル" in combined, combined


def test_no_notice_when_the_flag_was_not_given(project, capsys):
    """指定していないときに注意書きを出さない (常に出る注意は読まれなくなる)。"""
    _seed(project, [{"ts": "2026-10-01T00:00:00+00:00", "text": "orphan",
                     "session_id": "sv-DEAD"}])
    import os
    os.environ["BEACON_NOTE_REAP_YES"] = "1"
    try:
        cmd_note.cmd_note_reap()
    finally:
        os.environ.pop("BEACON_NOTE_REAP_YES", None)
    assert "--since-min" not in "".join(capsys.readouterr())


# --- AX-5: 数値でない値を黙って既定値に置き換えない ---------------------------

@pytest.mark.parametrize("env,flag", [
    ("BEACON_NOTE_LIST_SINCE_DAYS", "--since-days"),
    ("BEACON_NOTE_LIST_LIMIT", "--limit"),
])
def test_non_numeric_value_is_reported(project, capsys, monkeypatch, env, flag):
    """`--limit twenty` が黙って既定値に落ちない。

    同じファイルに「省略した件数を必ず申告する」機構を持ちながら、ここには
    配線されていなかった。既定値に落ちること自体は変えていない (落とさないと
    一覧が出せない) — 落としたと **言う** ようにした。
    """
    monkeypatch.setenv(env, "twenty")
    cmd_note._list_window()
    err = capsys.readouterr().err
    assert flag in err and "twenty" in err, err


def test_blank_value_is_not_reported(project, capsys, monkeypatch):
    """未指定 / 空白は誤りではないので黙る。"""
    monkeypatch.setenv("BEACON_NOTE_LIST_LIMIT", "   ")
    days, limit = cmd_note._list_window()
    assert limit == cmd_note.DEFAULT_LIST_LIMIT
    assert capsys.readouterr().err == ""


def test_one_helper_owns_the_numeric_rule():
    """環境変数を数値として読むのが 1 箇所であること。

    3 つのフラグが各自 `try/except ValueError: pass` を書くと、4 つ目が黙る
    実装で入ってくる。

    この試験は 2 回書き直している。どちらの失敗も「規則ではなく字面を測って
    いた」ので記録しておく:
    1. `src.count("except ValueError") == 1` — docstring 内の同じ語句と、
       日付文字列の解釈 (別の関心事) まで数えて赤くなった。
    2. 「`int(...)` の引数に `os.environ.get(...)` が入っている関数」を探す —
       helper は env 名を **引数で受ける** ので構文的に一致せず、該当 0 件に
       なった (= 何も見ていない試験)。

    実際に守りたいのは「環境変数を読み、かつそれを数値に変える関数」が helper
    だけ、という形。下はそれを書いている。
    """
    import ast

    tree = ast.parse((ROOT / "lib" / "cmd_note.py").read_text(encoding="utf-8"))

    def _contains(fn, pred) -> bool:
        return any(pred(sub) for sub in ast.walk(fn))

    def _is_env_read(node) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ")

    def _is_int_cast(node) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "int")

    converters = sorted({
        fn.name for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _contains(fn, _is_int_cast)
    })
    assert converters == ["_int_or_notice"], (
        "環境変数を数値に変える経路が helper の外にある: %s" % converters)
    # 試験自身が空振りしていないこと (上の 2 度目の失敗がこれで防げる)。
    helper = next(fn for fn in ast.walk(tree)
                  if isinstance(fn, ast.FunctionDef)
                  and fn.name == "_int_or_notice")
    assert _contains(helper, _is_env_read) and _contains(helper, _is_int_cast)


# --- M-2: 既定値の写しが実装から導かれていること -----------------------------

def test_help_text_derives_the_defaults_from_the_constants():
    """help の数字が定数から入る (手打ちの写しが残らない)。

    レビュアーは定数を 10/50 に変えてから `3 日 / 20 件` と書いてある全ての
    写しが無検知で残ることを実測した。help はもう写しではない。
    """
    import importlib
    import commands

    importlib.reload(cmd_note)
    rows = {r["command"]: r["description"] for r in commands._help_registry()}
    listed = rows["beacon note list"]
    assert "last %d days" % cmd_note.DEFAULT_LIST_SINCE_DAYS in listed, listed
    assert "at most %d notes" % cmd_note.DEFAULT_LIST_LIMIT in listed, listed
    reaped = rows["beacon note reap --yes"]
    assert ("default %d" % cmd_note.DEFAULT_REAP_LIVENESS_WINDOW_MIN) in reaped, \
        reaped


DOC_COPIES = [
    "CLAUDE.md",
    "README.md",
    "skills/beacon-note.md",
    "shared/skills/beacon-note/SKILL.md",
    "plugins/beacon/skills/beacon-note/SKILL.md",
    "skills/beacon-session-start.md",
    "shared/skills/beacon-session-start/SKILL.md",
    "plugins/beacon/skills/beacon-session-start/SKILL.md",
]


@pytest.mark.parametrize("rel", DOC_COPIES)
def test_doc_copies_state_the_current_defaults(rel):
    """既定値を書いているドキュメントが、現在の定数と一致していること。

    prose は f-string にできないので、代わりに **数字が合っていること** を
    機械で見る。定数を変えた人はここで赤くなり、どのファイルを直すべきか
    (= 名前が出る) が分かる。レビュアーが 10/50 に変えて 901 件が通った穴が
    これで閉じる。
    """
    path = ROOT / rel
    if not path.exists():
        pytest.skip("%s がない" % rel)
    text = path.read_text(encoding="utf-8")
    if "直近" not in text and "last 3 days" not in text and "last " not in text:
        pytest.skip("%s は既定値を書いていない" % rel)
    days = str(cmd_note.DEFAULT_LIST_SINCE_DAYS)
    limit = str(cmd_note.DEFAULT_LIST_LIMIT)
    assert ("直近 %s 日" % days) in text or ("last %s days" % days) in text, (
        "%s の『直近 N 日』が定数 %s と合っていない" % (rel, days))
    assert ("%s 件" % limit) in text or ("%s notes" % limit) in text, (
        "%s の『最大 N 件』が定数 %s と合っていない" % (rel, limit))
