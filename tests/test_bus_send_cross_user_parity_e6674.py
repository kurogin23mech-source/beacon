"""cross-user DM の 2 旗が両フロントで同じ env を作ることの試験 (ms-160 e-6674).

`bin/beacon` は `--recipient-confirmed` (ms-110 e-3443) と `--to-user`
(ms-54 e-2934) を受けるが、Windows / pipx が通る `beacon_cli/dispatch.py` には
**どちらも存在しなかった** (grep して 0 件)。

実害は「不便」ではなく「できない」:

  * サーバは同意証跡 (recipient_confirmed claim) の無い cross-user DM を拒否する。
    旗を渡せない = 別ユーザー宛 DM が送れない。
  * `--to-user` は時差配信 (相手が次にセッションを開いたときに届く) の宛先指定。
    live なセッションが 1 つも無いときの唯一の経路で、これも送れない。

どちらも `/beacon-dm-send` Skill の手順書に書かれているので、Windows の利用者は
手順どおり叩いて「認識できない引数」で止まる。手順書に従うほど失敗する型。

旗の名前を見るだけの `scripts/check-cli-help-drift.py` はこれを検出しない
(片方のフロントに無い旗は「揃っている」と数えられない)。なので **旗 → env の
写像** を両フロントで実際に起動して突き合わせる。
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

WATCHED = ("BEACON_BUS_RECIPIENT_CONFIRMED", "BEACON_BUS_RECIPIENT_USER",
           "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_MANUAL",
           "BEACON_BUS_CHANNEL")

_STUB = '''\
import json, os, sys
keys = {k: v for k, v in os.environ.items() if k.startswith("BEACON_BUS_")}
print(json.dumps({"subcmd": sys.argv[1], "env": keys}, ensure_ascii=False))
'''


@pytest.fixture
def stub_tree(tmp_path):
    """commands.py を env 印字スタブに差し替えた影の木。

    bin/beacon は自分の位置から COMMANDS_PY を無条件に決めるので、bin/ ごと
    写して隣に stub lib/ を置く (e-5981 の試験と同じ手)。実データには触れない。
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
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    return subprocess.run(["bash", str(stub_tree / "bin" / "beacon"), *argv],
                          capture_output=True, text=True,
                          cwd=str(stub_tree), env=env)


def _run_python(stub_tree, argv):
    sys.path.insert(0, str(ROOT))
    import beacon_cli.dispatch as d
    captured = {}

    def _fake(root, subcmd, env_overrides, *, extra_args=None):
        captured["subcmd"] = subcmd
        captured["env"] = dict(env_overrides)
        return 0

    real, real_ensure = d._run_commands_py, d._ensure_project
    d._run_commands_py = _fake
    d._ensure_project = lambda: None
    try:
        rc = d.dispatch(stub_tree, list(argv))
    finally:
        d._run_commands_py = real
        d._ensure_project = real_ensure
    return rc, captured


def _bash_env(proc):
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)["env"]
    raise AssertionError("stub produced no JSON: " + proc.stdout + proc.stderr)


CASES = [
    (["bus", "send", "--channel", "dm", "--to", "sv-1",
      "--payload", '{"text":"x"}', "--recipient-confirmed"],
     {"BEACON_BUS_RECIPIENT_CONFIRMED": "1", "BEACON_BUS_RECIPIENT_SESSION": "sv-1"}),

    (["bus", "send", "--channel", "dm", "--to-user", "uid-9",
      "--payload", '{"text":"x"}'],
     {"BEACON_BUS_RECIPIENT_USER": "uid-9", "BEACON_BUS_RECIPIENT_CONFIRMED": ""}),

    (["bus", "send", "--channel", "dm", "--to", "sv-1",
      "--payload", '{"text":"x"}', "--manual", "--recipient-confirmed"],
     {"BEACON_BUS_MANUAL": "1", "BEACON_BUS_RECIPIENT_CONFIRMED": "1"}),

    (["bus", "send", "--channel", "dm", "--to", "sv-1", "--payload", '{"text":"x"}'],
     {"BEACON_BUS_RECIPIENT_CONFIRMED": ""}),
]


@pytest.mark.parametrize("argv,expected", CASES, ids=[c[0][3] + "/" + "+".join(
    a for a in c[0] if a.startswith("--")) for c in CASES])
def test_python_frontend_passes_the_cross_user_flags(stub_tree, argv, expected):
    """Windows / pipx 経路が旗を env へ写すこと。旗が parse できるだけでは
    足りない — 写されずに落ちるのが e-5981 で踏んだ形。"""
    rc, cap = _run_python(stub_tree, argv)
    assert rc == 0, cap
    for k, v in expected.items():
        assert cap["env"].get(k, "") == v, (k, cap["env"].get(k), cap["env"])


@pytest.mark.parametrize("argv,expected", CASES, ids=[c[0][3] + "/" + "+".join(
    a for a in c[0] if a.startswith("--")) for c in CASES])
def test_both_frontends_agree_on_the_cross_user_flags(stub_tree, argv, expected):
    """同じ argv が両フロントで同じ env を作ること。片側だけ直す drift を止める。"""
    bash_env = _bash_env(_run_bash(stub_tree, argv))
    _, cap = _run_python(stub_tree, argv)
    for k in expected:
        assert bash_env.get(k, "") == cap["env"].get(k, ""), (
            k, "bash=" + repr(bash_env.get(k)), "python=" + repr(cap["env"].get(k)))


def test_to_and_to_user_are_mutually_exclusive_on_both_frontends(stub_tree):
    """相互排他の規則が片方のフロントにしか無いと、もう片方は通してしまう。
    終了コードまで揃えて固定する。"""
    argv = ["bus", "send", "--channel", "dm", "--to", "sv-1",
            "--to-user", "uid-9", "--payload", "{}"]
    proc = _run_bash(stub_tree, argv)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "相互排他" in proc.stderr, proc.stderr
    rc, _ = _run_python(stub_tree, argv)
    assert rc == 2


def test_the_flags_the_server_gate_depends_on_are_read_by_the_implementation():
    """旗 → env → 実装 の最後の一歩。cmd_bus_send がこの env を実際に読むことを
    固定する (読まれない env を渡しても意味がない)。"""
    src = (ROOT / "lib" / "cmd_bus.py").read_text(encoding="utf-8")
    assert "BEACON_BUS_RECIPIENT_CONFIRMED" in src
    assert "BEACON_BUS_RECIPIENT_USER" in src
