"""`note clear` は既定で自セッションのメモだけを消す (ms-160 e-6714).

cloud のメモはプロジェクトの全セッションが共有する 1 つのストアで、`clear` は
それを丸ごと消していた。片付けた側は自分の分を消したつもりで、他のセッションの
引き継ぎメモまで持っていく (2026-09-28: fork の 3 件が親の片付けで消失。
2026-10-01: 28 件中 10 件が他セッション分)。

失ったものより重いのは **session-end の手順が完遂できなくなること** だった。
「メモを doc へ昇格してから clear」は、誰かが並走している限り安全に実行できず、
結果メモが溜まり続けた。既定を自セッションに絞ると、周りが何をしていても手順が
最後まで通る。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

ROWS = [
    {"ts": "t1", "text": "mine 1", "session_id": "sv-MINE"},
    {"ts": "t2", "text": "other A", "session_id": "sv-OTHER1"},
    {"ts": "t3", "text": "mine 2", "session_id": "sv-MINE"},
    {"ts": "t4", "text": "other B", "session_id": "sv-OTHER2"},
    {"ts": "t5", "text": "untagged legacy"},
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    b = tmp_path / ".beacon"
    b.mkdir()
    (b / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    (b / "session_notes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ROWS), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    import cmd_note
    monkeypatch.setattr(cmd_note, "_resolve_session_id", lambda: "sv-MINE")
    return tmp_path, b / "session_notes.jsonl"


def _texts(f):
    return [json.loads(l)["text"] for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_default_clears_only_this_sessions_notes(project, monkeypatch):
    cwd, notes = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    monkeypatch.delenv("BEACON_NOTE_CLEAR_ALL", raising=False)
    import cmd_note
    cmd_note.cmd_note_clear()
    assert _texts(notes) == ["other A", "other B", "untagged legacy"], _texts(notes)


def test_untagged_notes_are_not_assumed_to_be_mine(project, monkeypatch):
    """session_id の無い古いメモは「たぶん自分の」であって自分のではない。
    消す側に倒すと、この課題が塞ぐはずの事故をそのまま起こす。"""
    cwd, notes = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    import cmd_note
    cmd_note.cmd_note_clear()
    assert "untagged legacy" in _texts(notes)


def test_all_still_clears_everything(project, monkeypatch):
    cwd, notes = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    monkeypatch.setenv("BEACON_NOTE_CLEAR_ALL", "1")
    import cmd_note
    cmd_note.cmd_note_clear()
    # Nothing survives, so the file goes away — the post-condition
    # `note restore` was written against (ms-178 e-6656).
    assert not notes.exists(), _texts(notes)
    assert (cwd / ".beacon" / "session_notes.jsonl.bak").exists()


def test_all_names_whose_notes_it_will_take(project, monkeypatch, capsys):
    """件数だけでは利用者が損得を測れない。どのセッションの分が何件かを
    提示してから消す (AC: 何件・どのセッション分を提示)。

    確認なしは *拒否* であって成功した下見ではないので、stderr に出して
    非 0 で終わる (ms-178 の『stake を隠す gate は gate ではない』を継承)。"""
    cwd, _ = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_ALL", "1")
    monkeypatch.delenv("BEACON_NOTE_CLEAR_YES", raising=False)
    import cmd_note
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_clear()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "内訳 (セッション別)" in err, err
    assert "sv-OTHER1" in err and "sv-OTHER2" in err, err
    assert "← このセッション" in err, err
    assert "他セッションの引き継ぎメモが含まれます" in err, err


def test_all_without_confirmation_changes_nothing(project, monkeypatch):
    cwd, notes = project
    before = notes.read_text(encoding="utf-8")
    monkeypatch.setenv("BEACON_NOTE_CLEAR_ALL", "1")
    monkeypatch.delenv("BEACON_NOTE_CLEAR_YES", raising=False)
    import cmd_note
    with pytest.raises(SystemExit):
        cmd_note.cmd_note_clear()
    assert notes.read_text(encoding="utf-8") == before


def test_an_unresolvable_session_refuses_rather_than_guessing(project, monkeypatch):
    """自分の id が分からないとき「たぶん自分の分」を消すのは、この scoping が
    止めようとしている失敗そのもの。拒否して明示経路を案内する。"""
    cwd, notes = project
    before = notes.read_text(encoding="utf-8")
    import cmd_note
    monkeypatch.setattr(cmd_note, "_resolve_session_id", lambda: "")
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_clear()
    assert exc.value.code == 1
    assert notes.read_text(encoding="utf-8") == before


def test_backup_keeps_the_restore_contract_and_avoids_the_purge_path(project, monkeypatch):
    """スコープ付きの削除でも `note restore` が読む退避 (.jsonl.bak) を書く。

    最初の実装はここを独自の 1 ファイルに置き換えてしまい、e-6656 が用意した
    復元経路を黙って切っていた。purge の退避とは別パスであることも併せて固定
    する (片方がもう片方の復元経路を壊さないため)。"""
    cwd, notes = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    import cmd_note
    bak = pathlib.Path(cmd_note._get_notes_path() + ".bak")
    assert str(bak) != cmd_note._purge_backup_path()
    cmd_note.cmd_note_clear()
    saved = bak.read_text(encoding="utf-8")
    assert "other A" in saved and "mine 1" in saved, "退避は削除前の全文であること"


def test_session_end_routine_completes_with_other_sessions_present(project, monkeypatch):
    """AC3: 並走セッションの有無に関わらず手順が完遂できること。
    他セッションのメモが居る状態で、自分の分だけを消し切れる。"""
    cwd, notes = project
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    import cmd_note
    cmd_note.cmd_note_clear()
    mine = [json.loads(l) for l in notes.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert not [n for n in mine if n.get("session_id") == "sv-MINE"]
    assert len(mine) == 3, "他セッション分は 1 件も失われていない"


def test_both_frontends_pass_the_all_flag():
    """旗が片方のフロントにしかないと、もう片方の利用者は全件消せない
    (この repo の恒常的な drift 族 — ms-160 e-6674 / PR #778)。"""
    bash = (ROOT / "bin" / "beacon").read_text(encoding="utf-8")
    assert "BEACON_NOTE_CLEAR_ALL" in bash
    disp = (ROOT / "beacon_cli" / "dispatch.py").read_text(encoding="utf-8")
    assert "BEACON_NOTE_CLEAR_ALL" in disp
    sys.path.insert(0, str(ROOT))
    from beacon_cli import dispatch
    args = dispatch.build_parser().parse_args(["note", "clear", "--all", "--yes"])
    assert args.note_all is True and args.assume_yes is True
