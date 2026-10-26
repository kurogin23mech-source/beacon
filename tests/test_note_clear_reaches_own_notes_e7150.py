"""自分が書いたメモに自分の clear が届くこと (ms-178 / e-7150 AC1・AC5).

e-7150 の実害は「clear が壊れている」ではなく「clear が探す id に該当メモが 0 件」
だった。書き込みと削除は同じ ``_resolve_session_id()`` を通るのに、その戻り値が
呼び出しごとに変わっていたので、**同じ関数を通っているのに一致しない**という形で
壊れていた (2026-10-25 実測: 1 セッションの 5 件が 5 つの別 id、各 id に埋まった
時刻はそのメモ自身の書き込み時刻)。

再現条件を正しく置くこと自体がこのモジュールの要点である。最初に書いた版は
local mode で組んでいて、旧実装でも緑になった — ``CLAUDE_CODE_SESSION_ID`` を
採用する経路 (``get_or_mint_session`` step 1) が先に短絡して、壊れていた
tuple 経路を一度も通らなかったからである。実害が出るのは **cloud mode** で、
``get_or_mint_session_via_server`` が ``(machine_id, parent_pid)`` を身元として
サーバに問い合わせる経路。だからここでは cloud.json を置き、サーバの
``{machine_id}_{parent_pid}`` lookup (``server/firestore_client.py``) と同じ
規則で mint する偽サーバを使う: 同じ tuple なら同じ sid、新しい tuple なら
新しい sid。これで旧実装は赤、修正版は緑になる。
"""

from __future__ import annotations

import itertools
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_note  # noqa: E402
import session as S  # noqa: E402

SID = "852017d9-bdb9-4b72-b432-b66d2e06cc79"


class _TupleMintingServer:
    """``{machine_id}_{parent_pid}`` → session_id を引く偽サーバ。

    本物の挙動をそのまま写してある: 同じ lookup key には同じ sid を返し、
    未知の key には新しい sid を mint する。だから「呼ぶたび別 sid」が出たら
    それは **身元が呼ぶたび変わっていた** ことの証明になる。
    """

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.heartbeats = 0

    def me_upsert_machine(self, *a, **k):
        return {"machine_id": "mc-test"}

    def me_heartbeat(self, project_id, machine_id, parent_pid, **k):
        self.heartbeats += 1
        key = "%s_%s" % (machine_id, parent_pid)
        minted = key not in self.rows
        if minted:
            self.rows[key] = "sv-%s-%04d" % (machine_id, len(self.rows) + 1)
        return {"session_id": self.rows[key], "minted": minted}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """cloud mode のプロジェクト + harness の churning な getppid。

    ``BEACON_PARENT_PID`` を外し ``CLAUDE_CODE_SESSION_ID`` だけを置くのが
    「bclaude を経由しないセッション」= 実際に壊れていた条件。
    """
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    # cloud.json を置いて tuple 経路を有効にする。notes 自体は local ストアに
    # 書かせたいので、cloud の notes API は下で無効化する。
    (beacon / "cloud.json").write_text(
        json.dumps({"project_id": "proj-1", "api_url": "https://api.test"}),
        encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.setenv(S.ENV_USE_CLOUD_FIRST, "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    for var in ("BEACON_JSON", "BEACON_NOTE_LIST_ALL", "BEACON_NOTE_LIST_LIMIT",
                "BEACON_NOTE_LIST_SINCE_DAYS", "BEACON_NOTE_CLEAR_ALL"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.setenv(S.ENV_SESSION_ID, SID)
    monkeypatch.setattr(S.os, "getppid", lambda c=itertools.count(50000): next(c))
    # bridge claim / codex pointer を介在させず、壊れていた mint 鎖だけを見る。
    monkeypatch.setattr(S, "read_bridge_session", lambda: {})
    monkeypatch.setattr(S, "read_codex_session_pointer", lambda: "")
    # 身元を tuple で引く偽サーバ。
    server = _TupleMintingServer()
    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", lambda: (server, {}))
    monkeypatch.setattr(S, "_read_machine_cache",
                        lambda: {"machine_id": "mc-test",
                                 "fingerprint": S._agent.get_actor().get("machine")})
    # notes は cloud ではなく local ファイルだけを真値にする (この試験の対象は
    # 身元の解決であって cloud 同期ではない)。
    monkeypatch.setattr(cmd_note, "_cloud_project_id", lambda: "")
    # worked-target の推定は cloud API を叩く (この試験の対象外)。推定関数と、
    # その引数を作る load_project の **両方** を外さないと実ネットワークに出る
    # — 偽ホストへの接続を試みて遅くなるだけでなく、ネットワークの有無で結果が
    # 変わる非 hermetic な試験になる。関数名だけ差し替えて「外した」と思うのが
    # ありがちな取りこぼしなので、引数側も塞ぐ。
    monkeypatch.setattr(cmd_note, "resolve_worked_target_ids", lambda *a, **k: [])
    monkeypatch.setattr(cmd_note, "load_project",
                        lambda *a, **k: {"name": "t", "milestones": []})

    path = beacon / "session_notes.jsonl"
    return type("Store", (), {"path": path, "server": server})()


def _read(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_note(monkeypatch, text):
    monkeypatch.setenv("BEACON_NOTE_TEXT", text)
    monkeypatch.setenv("BEACON_NOTE_CONTEXT", "")
    cmd_note.cmd_note_add()


def test_two_resolutions_in_one_session_agree(store):
    """書き込み時と削除時で同じ身元に解決される (壊れていた中心の性質)。"""
    at_write = S.resolve_active_session_id()
    at_clear = S.resolve_active_session_id()
    assert at_write and at_write == at_clear


def test_notes_written_in_one_session_share_one_id(store, monkeypatch):
    """1 セッションが連続して書いたメモが、全部同じ id を持つ。

    旧実装ではここが「3 件 3 id」になり、その時点で clear は到達不能だった。
    """
    for i in range(3):
        _write_note(monkeypatch, "note %d" % i)
    ids = {n.get("session_id") for n in _read(store.path)}
    assert len(ids) == 1, ids
    assert ids != {None}


def test_server_sees_one_session_not_one_per_call(store, monkeypatch):
    """サーバ側にも 1 セッションしか生まれない。

    「メモの id が揃った」だけでは、サーバに毎回新しいセッション行が積まれる
    形を見逃す。lookup key の数で直接見る。
    """
    for i in range(4):
        _write_note(monkeypatch, "note %d" % i)
    assert len(store.server.rows) == 1, store.server.rows


def test_clear_removes_exactly_this_sessions_notes(store, monkeypatch):
    """自分の分だけが消え、他セッションの引き継ぎメモは残る (AC1 + AC3)。"""
    for i in range(2):
        _write_note(monkeypatch, "mine %d" % i)
    rows = _read(store.path)
    rows.append({"ts": "zzz", "text": "someone else", "session_id": "sv-OTHER"})
    store.path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")

    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    cmd_note.cmd_note_clear()

    assert [n["text"] for n in _read(store.path)] == ["someone else"]


def test_session_end_leaves_this_session_with_zero_notes(store, monkeypatch):
    """AC5: clear 後、このセッションが書いたメモが 1 件も残っていない。

    判定は「**書き込み時に実際に付いた** id のメモが残っていないか」で見る。
    「いま解決した id のメモが 0 件」と書くと、id が呼び出しごとに変わる壊れた
    実装では *何も消えていなくても* 空振りで真になる (最初の版がこれで、旧実装
    でも緑だった)。消えたことを、消す前に観測した id で確かめる。
    """
    for i in range(4):
        _write_note(monkeypatch, "n%d" % i)
    written = _read(store.path)
    ids_at_write = {(n.get("session_id") or "") for n in written}
    assert written, "前提: メモが書かれている"

    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    cmd_note.cmd_note_clear()

    left = _read(store.path)
    survivors = [n for n in left
                 if (n.get("session_id") or "") in ids_at_write]
    assert survivors == [], (
        "session-end 後に自分のメモが残っている "
        "(書き込み時の id=%s / 残存=%d 件)" % (ids_at_write, len(survivors)))


def test_backup_is_written_so_the_clear_is_recoverable(store, monkeypatch):
    """消す前に退避が取られている (既存の保証を壊していない)。"""
    _write_note(monkeypatch, "recoverable")
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    cmd_note.cmd_note_clear()

    backup = pathlib.Path(str(store.path) + ".bak")
    assert backup.exists()
    assert "recoverable" in backup.read_text(encoding="utf-8")
