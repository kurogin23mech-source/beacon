"""プロジェクトの保存先を指す流儀を 1 つに寄せる (ms-166 e-6820)。

## なぜ要るか

この repo は「プロジェクトの保存先はどこか」に **2 通り** で答えていた。

* ``commands_shared.get_project_file()`` は ``BEACON_PROJECT_FILE`` を読む
  — 保存庫 / bus の予算 / 送信記録 / ドキュメントがこちら。
* ``lib/session.py`` は ``Path.cwd()`` から組み、その環境変数を **見ていなかった**
  — session.json / cloud.json / bridge の claim / bridges/ / Codex の受信ループが
  こちら。

結果、**環境変数だけを設定したテストは後者の家族を本物の repo の .beacon/ に書いた**。
``tests/conftest.py`` の既知の漏れ一覧に ``session.json`` が載っていたのがその実害で、
``isolated_project`` fixture が「隔離には 2 手要る (env と chdir)」と説明していたのも
この二重流儀のため。片方しか隔離できない状態だった。

## 直した形

``session._beacon_dir()`` を **唯一の口** にした。``BEACON_PROJECT_FILE`` が設定されて
いればその親、無ければ従来どおり ``Path.cwd() / ".beacon"``。**既定の挙動は変えない** —
変わるのは環境変数を設定した文脈だけで、それがこの修正の目的。

cwd を **値として** 使う箇所 (worktree の識別 / 作業ディレクトリの申告) はこの口を
通さない。あれはパスの解決ではなく「自分がどこに居るか」の報告なので別物。
このファイルはその境界も固定する (全部を機械的に置換して識別を壊さないため)。
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import session  # noqa: E402


@pytest.fixture
def pointed_elsewhere(tmp_path, monkeypatch):
    """``BEACON_PROJECT_FILE`` を tmp の .beacon に向ける (chdir はしない)。

    chdir しないのが肝: 旧実装は cwd から組んでいたので、env だけでは逃げられず
    repo を書いていた。この fixture は「env だけ」の状況を再現する。
    """
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True, exist_ok=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    return beacon_dir


# --- 解決器そのもの ---------------------------------------------------------

def test_env_var_decides_the_beacon_dir(pointed_elsewhere):
    assert session._beacon_dir() == pointed_elsewhere


def test_default_is_unchanged_when_the_env_var_is_absent(monkeypatch):
    # 本番 / 通常の CLI 利用の挙動を変えていないこと。
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    assert session._beacon_dir() == Path.cwd() / ".beacon"


def test_blank_env_var_falls_back_to_cwd(monkeypatch):
    # 空文字 / 空白だけの設定は「未設定」と同じに扱う (半端な値で repo 外を指さない)。
    for value in ("", "   "):
        monkeypatch.setenv("BEACON_PROJECT_FILE", value)
        assert session._beacon_dir() == Path.cwd() / ".beacon"


# --- その家族のパスが全部ついてくる -----------------------------------------

_PATH_ACCESSORS = (
    ("session.json", lambda: session._session_json_path()),
    ("bridge.json", lambda: session._bridge_claim_path()),
    ("bridges", lambda: session._bridges_dir()),
)


def _resolve(name: str):
    """その名前を返す accessor を呼ぶ (名前が変わっていたら skip せず落とす)。"""
    for n, fn in _PATH_ACCESSORS:
        if n == name:
            return fn()
    raise AssertionError(name)


def test_every_path_in_the_family_follows_the_env_var(pointed_elsewhere):
    for name, _ in _PATH_ACCESSORS:
        got = _resolve(name)
        assert Path(got).parent == pointed_elsewhere, (name, got)


def test_the_cloud_marker_is_read_from_the_same_place(pointed_elsewhere):
    # cloud.json の有無判定も同じ口を通る (= 片方だけ repo を見る形にしない)。
    assert session._is_cloud_mode() is False
    (pointed_elsewhere / "cloud.json").write_text(
        json.dumps({"project_id": "p", "api_url": "https://api.test"}), encoding="utf-8")
    assert session._is_cloud_mode() is True


def test_the_codex_receive_loop_path_follows_too(pointed_elsewhere):
    got = Path(session._codex_session_pointer_path())
    assert got.parent.parent == pointed_elsewhere, got


# --- 境界: cwd を「値」として使う箇所は置換していない -----------------------

def test_cwd_is_still_used_where_it_means_where_am_i():
    """worktree 識別 / 作業ディレクトリの申告は cwd のままであること。

    全部を機械的に置き換えると「自分がどこに居るか」の報告が壊れる。パスの解決と
    識別の報告は別物なので、境界を固定する。
    """
    src = open(os.path.join(_LIB, "session.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    cwd_users = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and getattr(sub.func, "attr", "") == "cwd"):
                cwd_users.add(node.name)
    # 解決器自身 (fallback で使う) 以外に、cwd を値として使う箇所が残っていること。
    assert "_beacon_dir" in cwd_users, cwd_users
    assert len(cwd_users) >= 3, (
        "cwd を値として使う箇所まで置換された疑い (worktree 識別 / cwd 申告が"
        f"壊れていないか確認してください): {sorted(cwd_users)}")


def test_no_beacon_path_is_built_from_cwd_outside_the_resolver():
    """``.beacon`` の下のパスを cwd から直接組む箇所が残っていないこと。

    1 箇所でも残ると、そこだけ環境変数を無視して repo を書く — 今回直した穴が
    部分的に復活する形。構文木で「``Path.cwd() / <何か>`` を返す関数」を数え、
    解決器以外に無いことを固定する。
    """
    src = open(os.path.join(_LIB, "session.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "_beacon_dir":
            continue
        for sub in ast.walk(node):
            # Path.cwd() / X の形 (BinOp with Div) を探す
            if (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div)
                    and isinstance(sub.left, ast.Call)
                    and getattr(sub.left.func, "attr", "") == "cwd"):
                offenders.append(f"{node.name}:{sub.lineno}")
    # repo の識別そのもの (.git の有無を見る) は許す — パス解決ではない。免除は
    # 名前で宣言し、増えたらここに理由付きで足す形にしておく。
    _IDENTITY_ONLY = {"_in_linked_worktree"}
    offenders = [o for o in offenders if o.split(":")[0] not in _IDENTITY_ONLY]
    assert not offenders, (
        "cwd から .beacon 配下のパスを組んでいる箇所が残っています "
        f"(session._beacon_dir() を通してください): {offenders}")


def test_the_guard_actually_fails_on_a_reintroduced_site():
    # test-the-test: cwd から組む関数を合成して、上の検出が拾うことを確かめる。
    src = ("from pathlib import Path\n"
           "def leaked_path():\n"
           "    return Path.cwd() / '.beacon' / 'session.json'\n")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div)
                    and isinstance(sub.left, ast.Call)
                    and getattr(sub.left.func, "attr", "") == "cwd"):
                found.append(node.name)
    assert found == ["leaked_path"], found
