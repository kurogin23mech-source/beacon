"""新設した CLI 動詞が env を正しく写して両フロントで一致することの試験 (ms-160 e-5981).

なぜ「旗の名前が bash と python で揃っている」だけでは足りないか — この PR 自身が
その反例を踏んだ。``cmd_meeting_reschedule`` (python) は BEACON_MTG_CAL_NS /
BEACON_MTG_CAL_ACCT を読むのに、両フロントの ``meeting reschedule`` には対応する旗が
無く、CLI 経由では**カレンダーの割り当てを渡せなかった**。名前の一致を見る
``scripts/check-cli-help-drift.py`` はこれを検出できない (揃っていない旗は、両方に
無ければ揃っている)。抜けていたのは **旗 → env の写像**そのもの。

そこでこれらは、実際にコマンドを起動して ``commands.py`` に渡る env を捕まえ、

  * 動詞ごとに期待する env が揃って渡ること (写像の取りこぼしを検出)、
  * bash と Windows/pipx の 2 フロントが**同じ env** を作ること (片側だけ直す drift)、
  * 必須引数が欠けたときに非ゼロで止まること (silent no-op を作らない)、

を固定する。``commands.py`` は env をそのまま印字するスタブに差し替えるので、
プロジェクトの実データには一切触れない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 観測したい env だけを拾う (無関係な BEACON_* の揺れでテストが割れないように)。
WATCHED_PREFIXES = ("BEACON_SEND_", "BEACON_WATCH_", "BEACON_ACCOUNT_ID",
                    "BEACON_TS_", "BEACON_RFC822_MSGID", "BEACON_JSON")

_STUB = '''\
import json, os, sys
keys = {}
for k, v in os.environ.items():
    if k.startswith(("BEACON_SEND_", "BEACON_WATCH_", "BEACON_TS_")) or k in (
            "BEACON_ACCOUNT_ID", "BEACON_RFC822_MSGID", "BEACON_JSON"):
        keys[k] = v
print(json.dumps({"subcmd": sys.argv[1], "env": keys}, ensure_ascii=False))
'''


@pytest.fixture
def stub_tree(tmp_path):
    """commands.py を「渡された env を印字するだけ」に差し替えた、隔離された影の木。

    ``bin/beacon`` は ``BEACON_DIR="$(dirname $0)/.."`` で自分の位置から
    ``COMMANDS_PY`` を**無条件に決める**ので、env で差し替えることはできない
    (実際にこの試験を書く途中で、env 上書きのつもりが本物のプロジェクトへ
    書き込みが走った)。``bin/`` ごと temp へ写し、その隣に stub の ``lib/`` を
    置くことで、フロントエンドは本物のまま実データから構造的に切り離す。
    """
    shutil.copytree(ROOT / "bin", tmp_path / "bin")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "commands.py").write_text(_STUB, encoding="utf-8")
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "probe", "milestones": []}), encoding="utf-8")
    return tmp_path


def _run_bash(stub_tree, argv):
    """影の木の bin/beacon を叩く (本物の repo の lib/ には決して届かない)。"""
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    proc = subprocess.run(
        ["bash", str(stub_tree / "bin" / "beacon"), *argv],
        capture_output=True, text=True, cwd=str(stub_tree), env=env)
    return proc


def _run_python(stub_tree, argv):
    """Windows/pipx 経路。dispatch を同一プロセスで駆動し、spawn を捕まえる。"""
    sys.path.insert(0, str(ROOT))
    import beacon_cli.dispatch as d

    captured = {}

    def _fake(root, subcmd, env_overrides, *, extra_args=None):
        captured["subcmd"] = subcmd
        captured["env"] = dict(env_overrides)
        return 0

    real = d._run_commands_py
    real_ensure = d._ensure_project
    d._run_commands_py = _fake
    d._ensure_project = lambda: None
    try:
        rc = d.dispatch(stub_tree, list(argv))
    finally:
        d._run_commands_py = real
        d._ensure_project = real_ensure
    return rc, captured


# --- 動詞 → (argv, 期待する subcmd, 期待する env の部分集合) ---------------------
CASES = [
    (["watch", "set", "act-1", "--channel", "email",
      "--thread", "th-9", "--cadence", "30"],
     "watch_set",
     {"BEACON_WATCH_TARGET": "act-1", "BEACON_WATCH_CHANNEL": "email",
      "BEACON_WATCH_THREAD": "th-9", "BEACON_WATCH_CADENCE": "30"}),

    (["watch", "clear", "act-2"], "watch_clear",
     {"BEACON_WATCH_TARGET": "act-2"}),

    (["sales", "send-account", "add", "会社", "--email", "a@example.com"],
     "sales_account_add",
     {"BEACON_SEND_LABEL": "会社", "BEACON_SEND_EMAIL": "a@example.com"}),

    (["sales", "send-account", "remove", "会社"], "sales_account_remove",
     {"BEACON_SEND_LABEL": "会社"}),

    (["sales", "send-account", "route", "会社", "--service", "calendar",
      "--namespace", "mcp__google-calendar", "--alias", "work"],
     "sales_account_route",
     {"BEACON_SEND_LABEL": "会社", "BEACON_SEND_SERVICE": "calendar",
      "BEACON_SEND_NAMESPACE": "mcp__google-calendar", "BEACON_SEND_ALIAS": "work"}),

    (["sales", "send-account", "resolve", "会社", "--service", "gmail"],
     "sales_account_resolve",
     {"BEACON_SEND_LABEL": "会社", "BEACON_SEND_SERVICE": "gmail"}),

    # 署名: --clear は値と別スロットで渡り、判定は python 側に委ねる (共存拒否の唯一点)。
    (["sales", "send-account", "signature", "会社", "--clear"],
     "sales_account_signature",
     {"BEACON_SEND_LABEL": "会社", "BEACON_SEND_SIGNATURE_CLEAR": "1"}),

    (["sales", "send-account", "transcript-source", "set", "acc-1",
      "--type", "drive_folder", "--folder-id", "F1", "--naming", "N", "--tool", "T"],
     "sales_account_transcript_source_set",
     {"BEACON_ACCOUNT_ID": "acc-1", "BEACON_TS_TYPE": "drive_folder",
      "BEACON_TS_FOLDER_ID": "F1", "BEACON_TS_NAMING": "N", "BEACON_TS_TOOL": "T"}),

    (["sales", "send-account", "transcript-source", "get", "acc-2"],
     "sales_account_transcript_source_get",
     {"BEACON_ACCOUNT_ID": "acc-2"}),

    (["sales", "identity", "set", "会社"], "sales_identity_set",
     {"BEACON_SEND_IDENTITY": "会社"}),

    (["sales", "identity", "check", "--from", "a@example.com", "--label", "会社"],
     "sales_identity_check",
     {"BEACON_SEND_FROM": "a@example.com", "BEACON_SEND_LABEL": "会社"}),

    (["sales", "gmail-permalink", "--from", "a@example.com",
      "--msgid", "<x@mail>"], "sales_gmail_permalink",
     {"BEACON_SEND_FROM": "a@example.com", "BEACON_RFC822_MSGID": "<x@mail>"}),
]


@pytest.mark.parametrize("argv,subcmd,expected", CASES,
                         ids=[" ".join(c[0][:3]) for c in CASES])
def test_bash_frontend_maps_flags_to_env(stub_tree, argv, subcmd, expected):
    """bash 側が各旗を期待する env に写すこと (写像の取りこぼしを検出)。"""
    proc = _run_bash(stub_tree, argv)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["subcmd"] == subcmd
    for k, v in expected.items():
        assert got["env"].get(k) == v, f"{k}: {got['env'].get(k)!r} != {v!r}"


@pytest.mark.parametrize("argv,subcmd,expected", CASES,
                         ids=[" ".join(c[0][:3]) for c in CASES])
def test_python_frontend_maps_flags_to_env(stub_tree, argv, subcmd, expected):
    """Windows/pipx 側も同じ写像であること (片側だけ直す drift を止める)。"""
    rc, cap = _run_python(stub_tree, argv)
    assert rc == 0, cap
    assert cap["subcmd"] == subcmd
    for k, v in expected.items():
        assert cap["env"].get(k) == v, f"{k}: {cap['env'].get(k)!r} != {v!r}"


@pytest.mark.parametrize("argv,subcmd,expected", CASES,
                         ids=[" ".join(c[0][:3]) for c in CASES])
def test_both_frontends_agree(stub_tree, argv, subcmd, expected):
    """同じ入力に対し bash と python が **同一の env** を作ること。

    meeting reschedule のカレンダー旗欠落は、片側にだけ旗があれば起きる型。
    名前の一致 (cli-help-drift) ではなく写した値そのものを突き合わせる。
    """
    proc = _run_bash(stub_tree, argv)
    assert proc.returncode == 0, proc.stderr
    bash_env = json.loads(proc.stdout.strip().splitlines()[-1])["env"]
    _, cap = _run_python(stub_tree, argv)
    py_env = {k: v for k, v in cap["env"].items()
              if k.startswith(WATCHED_PREFIXES[:-1]) or k in
              ("BEACON_ACCOUNT_ID", "BEACON_RFC822_MSGID", "BEACON_JSON")}
    for k in set(bash_env) | set(py_env):
        assert bash_env.get(k, "") == py_env.get(k, ""), (
            f"{k}: bash={bash_env.get(k)!r} python={py_env.get(k)!r}")


# --- 必須引数が欠けたら止まること (silent no-op を作らない) --------------------
MISSING = [
    (["watch", "set", "act-1"], "--channel なし"),
    (["watch", "clear"], "work-item-id なし"),
    (["sales", "send-account", "add", "会社"], "--email なし"),
    (["sales", "send-account", "remove"], "label なし"),
    (["sales", "send-account", "route", "会社", "--service", "gmail"], "--namespace なし"),
    (["sales", "send-account", "resolve"], "--service なし"),
    (["sales", "send-account", "signature"], "label なし"),
    (["sales", "send-account", "transcript-source", "get"], "acc-id なし"),
    (["sales", "identity", "set"], "identity なし"),
    (["sales", "identity", "check"], "--from なし"),
    (["sales", "gmail-permalink", "--msgid", "<x@mail>"], "--from なし"),
]


@pytest.mark.parametrize("argv,why", MISSING, ids=[m[1] for m in MISSING])
def test_bash_refuses_when_a_required_argument_is_missing(stub_tree, argv, why):
    proc = _run_bash(stub_tree, argv)
    assert proc.returncode != 0, f"{why}: 黙って通ってしまった ({proc.stdout!r})"
    assert "Usage:" in proc.stdout + proc.stderr


@pytest.mark.parametrize("argv,why", MISSING, ids=[m[1] for m in MISSING])
def test_python_refuses_when_a_required_argument_is_missing(stub_tree, argv, why):
    rc, cap = _run_python(stub_tree, argv)
    assert rc != 0, f"{why}: 黙って通ってしまった ({cap!r})"
    assert "subcmd" not in cap, f"{why}: 拒否したはずが commands.py を呼んでいる"
