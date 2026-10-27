"""`note reap` の IO 層が、純粋関数に正しい入力を渡していること (ms-178 / e-7150).

独立した保守性レビュー (2026-10-26) が突いた穴がこれ。`lib/note_reap.py` の純粋関数
(resolve_live_ids / classify / is_doomed) は `test_note_reap_e7150.py` で網羅的に
固めてあったが、**その入力を本番で作る側** — `_collect_live_session_ids`
(cloud_mode を決め、実セッション層と API を叩き、例外を飲む) と、両者を配線する
`cmd_note_reap` — を参照するテストが 1 件も無かった。

レビュアーは実際に本番の配線を変異させて確かめている: `_collect_live_session_ids`
の戻り値の `cloud_mode` を `False` に固定する (= モジュールの docstring が「存在理由」
と呼んでいる保証そのものを無効化する) と、note / session 系 964 件が **全部通った**。

つまり当時のテストは「正しい入力を渡されたときの判定は正しい」ことしか証明しておらず、
「本番が正しい入力を渡している」ことは一度も証明していなかった。純粋層を完璧に
テストしたせいで差分が完成して見える、という形の抜けである。

このモジュールは **継ぎ目を端から端まで**動かす。各テストは、レビュアーが通してしまった
変異で落ちることを狙って書いてある (下の `test_cloud_mode_is_detected_from_the_project`
が、まさにその `cloud_mode` 固定を捕まえる担当)。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_note  # noqa: E402
import note_reap  # noqa: E402
import session as S  # noqa: E402

ORPHAN = {"ts": "2026-10-01T00:00:00+00:00", "text": "orphan",
          "session_id": "sv-DEAD"}
MINE = {"ts": "2026-10-25T00:00:00+00:00", "text": "mine",
        "session_id": "sv-ME"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A note store whose cloud-ness the test chooses by writing cloud.json."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}',
                                         encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.chdir(tmp_path)
    for var in ("BEACON_NOTE_REAP_YES", "BEACON_NOTE_REAP_UNTAGGED",
                "BEACON_NOTE_REAP_SINCE_MIN", "BEACON_JSON"):
        monkeypatch.delenv(var, raising=False)
    # 身元は固定する (この module の主題は liveness の集め方)。
    monkeypatch.setattr(cmd_note, "_resolve_session_id", lambda: "sv-ME")
    monkeypatch.setattr(S, "read_session", lambda: {"session_id": "sv-ME"})
    monkeypatch.setattr(S, "read_all_bridge_claims", lambda: [])
    notes = beacon / "session_notes.jsonl"
    notes.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (ORPHAN, MINE)),
        encoding="utf-8")
    return beacon


class _FakeClient:
    """A cloud client with the three methods `note reap` actually calls.

    `reap` consults the server for live sessions AND reads/re-posts the cloud
    note store (the store has no delete-one, so the survivors are re-posted).
    A fake that implements only `list_sessions` makes the command abort on the
    note leg, which looks like the behaviour under test failing — so the fake
    has to cover the whole surface the command touches.
    """

    def __init__(self, live_sids=(), notes=None):
        self.live_sids = list(live_sids)
        self.notes = list(notes or [])
        self.session_query = {}

    def list_sessions(self, project_id, **kw):
        self.session_query.update(kw)
        return [{"session_id": sid} for sid in self.live_sids]

    def list_notes(self, project_id):
        return list(self.notes)

    def add_note(self, project_id, note):
        self.notes.append(note)

    def clear_notes(self, project_id):
        self.notes = []


def _go_cloud(beacon, project_id="proj-1"):
    (beacon / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": project_id}),
        encoding="utf-8")


def _read(beacon):
    p = beacon / "session_notes.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- cloud_mode の検出 (レビュアーが通した変異を捕まえる担当) -------------------

def test_cloud_mode_is_detected_from_the_project(project):
    """cloud.json の有無が、そのまま `cloud_mode` になる。

    ここが固定値になると、cloud プロジェクトが local として扱われ、サーバの
    稼働中一覧を問い合わせないまま「孤児」を判定してしまう (= 別マシン / 別
    worktree で動いているセッションのメモを刈る)。レビュアーが
    `"cloud_mode": False` を固定して 964 件を通した穴がここ。
    """
    assert cmd_note._collect_live_session_ids(5)["cloud_mode"] is False

    _go_cloud(project)
    assert cmd_note._collect_live_session_ids(5)["cloud_mode"] is True


def test_cloud_mode_abort_is_reached_end_to_end(project, monkeypatch):
    """cloud でサーバが読めないとき、**コマンドが**中止して何も消さない。

    純粋関数が refusal を返すことは別 module が見ている。ここで見るのは
    「本番の配線がその refusal に到達し、削除まで行かない」こと。
    """
    _go_cloud(project)
    monkeypatch.setattr(cmd_note, "_note_api_client",
                        lambda: (None, "connection refused"))
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_reap()
    assert exc.value.code == 1
    assert len(_read(project)) == 2, "中止したのにメモが消えている"
    assert not (project / "session_notes.jsonl.bak").exists(), \
        "中止したのに退避を取っている (退避が取れたら消す設計なので危険)"


def test_server_live_ids_reach_the_decision(project, monkeypatch):
    """サーバが返した稼働中 sid が、実際に「残す」側に効く。

    サーバを呼んでいても戻り値を live 集合に入れ忘れれば、他マシンの稼働中
    セッションのメモが孤児になる。呼んだことではなく、効いたことを見る。
    """
    _go_cloud(project)

    client = _FakeClient(live_sids=["sv-DEAD"])  # 孤児だったはずの id が実は生きている
    monkeypatch.setattr(cmd_note, "_note_api_client", lambda: (client, ""))
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    cmd_note.cmd_note_reap()
    left = {n["text"] for n in _read(project)}
    assert left == {"orphan", "mine"}, \
        "サーバが生きていると答えた sid のメモが刈られた"


def test_liveness_window_is_passed_to_the_server(project, monkeypatch):
    """`--since-min` の値がサーバ問い合わせに届く (届かなければ指定は無意味)。"""
    _go_cloud(project)
    client = _FakeClient()
    monkeypatch.setattr(cmd_note, "_note_api_client", lambda: (client, ""))
    monkeypatch.setenv("BEACON_NOTE_REAP_SINCE_MIN", "99")
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    cmd_note.cmd_note_reap()
    assert client.session_query.get("since_minutes") == 99
    assert client.session_query.get("live_only") is True


def test_default_liveness_window_comes_from_the_constant(project, monkeypatch):
    """既定の窓は名前付き定数から来る (リテラルの写しが増えない)。"""
    _go_cloud(project)
    client = _FakeClient()
    monkeypatch.setattr(cmd_note, "_note_api_client", lambda: (client, ""))
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")
    cmd_note.cmd_note_reap()
    assert client.session_query.get("since_minutes") == \
        cmd_note.DEFAULT_REAP_LIVENESS_WINDOW_MIN


# --- local 源の fail-closed (M-3) ---------------------------------------------

def test_local_bridge_read_failure_aborts(project, monkeypatch):
    """local で bridge claim が読めないときも中止する。

    local では bridge claim が「この作業フォルダの**他の**稼働中セッション」を
    記録している唯一の場所なので、読めないことは不在の証拠ではない。以前は
    ここだけ `except: pass` で fail-open だったため、docstring の「生存一覧が
    取れなければ中止する」が cloud でだけ真という状態だった (独立レビュー指摘)。
    """
    def _boom():
        raise OSError("permission denied")

    monkeypatch.setattr(S, "read_all_bridge_claims", _boom)
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_reap()
    assert exc.value.code == 1
    assert len(_read(project)) == 2
    assert not (project / "session_notes.jsonl.bak").exists()


def test_bridge_error_is_reported_not_swallowed(project, monkeypatch):
    """読めなかった理由が `bridge_error` として上がってくる。"""
    def _boom():
        raise OSError("permission denied")

    monkeypatch.setattr(S, "read_all_bridge_claims", _boom)
    sources = cmd_note._collect_live_session_ids(5)
    assert "permission denied" in sources["bridge_error"]
    _, refusal = note_reap.resolve_live_ids(**sources)
    assert refusal == note_reap.REFUSE_BRIDGE_UNREADABLE


def test_cloud_mode_tolerates_a_bridge_failure(project, monkeypatch):
    """cloud では bridge が読めなくてもサーバが全セッションを見られるので進める。

    fail-closed を機械的に全源へ広げると、cloud で bridge が読めないだけで
    reap が永久に使えなくなる。どの源が必須かはモードで違う、を固定する。
    """
    _go_cloud(project)

    def _boom():
        raise OSError("permission denied")

    monkeypatch.setattr(S, "read_all_bridge_claims", _boom)
    monkeypatch.setattr(cmd_note, "_note_api_client",
                        lambda: (_FakeClient(live_sids=["sv-ME"]), ""))
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    cmd_note.cmd_note_reap()
    assert [n["text"] for n in _read(project)] == ["mine"]


# --- live な bridge claim が残す側に効く ---------------------------------------

def test_alive_bridge_claims_are_kept(project, monkeypatch):
    """生きている bridge claim の sid は刈られない。"""
    monkeypatch.setattr(S, "read_all_bridge_claims",
                        lambda: [{"session_id": "sv-DEAD", "pid": 4242}])
    monkeypatch.setattr(S, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    cmd_note.cmd_note_reap()
    assert {n["text"] for n in _read(project)} == {"orphan", "mine"}


def test_dead_bridge_claims_do_not_keep_notes_alive(project, monkeypatch):
    """死んだ claim は生存の根拠にならない (さもないと何も刈れない)。"""
    monkeypatch.setattr(S, "read_all_bridge_claims",
                        lambda: [{"session_id": "sv-DEAD", "pid": 4242}])
    monkeypatch.setattr(S, "_pid_alive", lambda pid: False)
    monkeypatch.setenv("BEACON_NOTE_REAP_YES", "1")

    cmd_note.cmd_note_reap()
    assert [n["text"] for n in _read(project)] == ["mine"]
