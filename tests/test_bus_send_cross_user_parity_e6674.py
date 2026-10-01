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

import argparse
import io
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
    """Drive the Python frontend in-process, capturing stderr as well as env.

    stderr matters as much as the env: the mutual-exclusion refusal is a
    message duplicated in two languages, and a test that only checks the exit
    code lets one of the two be reworded while staying green — the very drift
    class this file exists to close (maintainability review, PR #778 M-1).
    """
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
    buf = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = buf
    try:
        rc = d.dispatch(stub_tree, list(argv))
    finally:
        sys.stderr = real_stderr
        d._run_commands_py = real
        d._ensure_project = real_ensure
    captured["stderr"] = buf.getvalue()
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

    終了コードだけでなく **文面も** 突き合わせる。規則もメッセージも 2 言語に
    複製されているので、片方だけ書き換えても終了コードは変わらない — 文面を
    比較しない試験は、この file が塞ぐと宣言している drift をそのまま通す
    (独立レビュー PR #778 M-1)。
    """
    argv = ["bus", "send", "--channel", "dm", "--to", "sv-1",
            "--to-user", "uid-9", "--payload", "{}"]
    proc = _run_bash(stub_tree, argv)
    rc, cap = _run_python(stub_tree, argv)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert rc == 2
    assert "相互排他" in proc.stderr, proc.stderr
    assert proc.stderr.strip() == cap["stderr"].strip(), (
        "両フロントの拒否文が食い違っています:\n"
        "  bash  : " + repr(proc.stderr.strip()) + "\n"
        "  python: " + repr(cap["stderr"].strip()))


def test_the_flags_the_server_gate_depends_on_are_read_by_the_implementation():
    """旗 → env → 実装 の最後の一歩。cmd_bus_send がこの env を実際に読むことを
    固定する (読まれない env を渡しても意味がない)。"""
    src = (ROOT / "lib" / "cmd_bus.py").read_text(encoding="utf-8")
    assert "BEACON_BUS_RECIPIENT_CONFIRMED" in src
    assert "BEACON_BUS_RECIPIENT_USER" in src

# --- 独立レビュー (PR #778) の指摘に対する回帰 ------------------------------
#
# AX-1 (high) が本質的だった: 直したのは `bus send` だけで、Skill が canonical と
# 呼ぶ `dm send` は塞がっていなかった。bash では `dm send` が
# `exec ... bus send --channel dm "$@"` なので **物理的に** ずれようがないが、
# この shim は handler だけ再利用して parser を手で複製しており、その複製が
# ずれていた。つまり「片方のフロントを直して姉妹を置き去りにする」という、
# この課題自身が塞ごうとしている型を自分でもう一度踏んでいた。

DM_SEND_CASES = [
    (["dm", "send", "--to", "sv-1", "--payload", '{"text":"x"}',
      "--recipient-confirmed"],
     {"BEACON_BUS_RECIPIENT_CONFIRMED": "1", "BEACON_BUS_RECIPIENT_SESSION": "sv-1",
      "BEACON_BUS_CHANNEL": "dm"}),
    (["dm", "send", "--to-user", "uid-9", "--payload", '{"text":"x"}'],
     {"BEACON_BUS_RECIPIENT_USER": "uid-9", "BEACON_BUS_CHANNEL": "dm"}),
]


@pytest.mark.parametrize("argv,expected", DM_SEND_CASES,
                         ids=["dm-send/recipient-confirmed", "dm-send/to-user"])
def test_canonical_dm_send_takes_the_cross_user_flags_too(stub_tree, argv, expected):
    """`dm send` は Skill が canonical と呼ぶ入口。ここが塞がっていなければ、
    手順どおり叩いた Windows の利用者は結局止まる (独立レビュー AX-1)。"""
    rc, cap = _run_python(stub_tree, argv)
    assert rc == 0, cap
    for k, v in expected.items():
        assert cap["env"].get(k, "") == v, (k, cap["env"].get(k), cap["env"])


def test_dm_send_and_bus_send_declare_the_same_flag_surface():
    """2 つの verb の旗集合が一致すること (--channel は dm では暗黙なので除く)。

    手で複製すると必ずずれるので、両者が同じ宣言関数を通っていることを
    「集合が一致する」という観測可能な性質で固定する。片方にだけ旗を足したら
    赤くなる。"""
    sys.path.insert(0, str(ROOT))
    from beacon_cli import dispatch as d
    parser = d.build_parser()

    def _flags(path):
        node = parser
        for token in path:
            sub = next(a for a in node._actions
                       if isinstance(a, argparse._SubParsersAction))
            node = sub.choices[token]
        return {o for a in node._actions for o in a.option_strings}

    bus = _flags(["bus", "send"])
    dm = _flags(["dm", "send"])
    assert bus - dm == {"--channel"}, (
        "bus send にあって dm send に無い旗 (--channel 以外): " + repr(bus - dm - {"--channel"}))
    assert dm - bus == set(), "dm send にあって bus send に無い旗: " + repr(dm - bus)


@pytest.mark.parametrize("argv", [
    ["bus", "send", "--help"],
    ["bus", "listen", "-h"],
    ["dm", "send", "--help"],
])
def test_help_explains_instead_of_crashing(stub_tree, argv):
    """`beacon bus send --help` は argparse の
    `unrecognized arguments: --help` で落ちていた (独立レビュー AX-2)。
    新しい旗を学ぼうとした利用者が、引数解析ごと壊れていると誤認する。"""
    rc, cap = _run_python(stub_tree, argv)
    assert rc == 0, cap


def test_both_frontends_describe_bus_send_the_same_way():
    """同じコマンドの自己説明が 2 フロントで食い違わないこと (独立レビュー AX-3)。
    片方の help にしか無い旗は、どちらを引いたかで答えが変わる。"""
    bash_src = (ROOT / "bin" / "beacon").read_text(encoding="utf-8")
    line = next(l for l in bash_src.splitlines()
                if l.strip().startswith("Usage: beacon bus send"))
    for flag in ("--to-user", "--recipient-confirmed", "--manual"):
        assert flag in line, "bash の usage が " + flag + " を載せていません: " + line


def test_fanout_states_the_empty_recipient_user(stub_tree):
    """複数宛先の fan-out で BEACON_BUS_RECIPIENT_USER を明示的に空へ倒すこと
    (独立レビュー AX-4)。設定しないと呼び出し元の shell に export された古い値を
    子プロセスが継承しうる。--to と --to-user は排他なので正しい値は常に空。"""
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    env["BEACON_BUS_RECIPIENT_USER"] = "stale-leak"
    proc = subprocess.run(
        ["bash", str(stub_tree / "bin" / "beacon"), "bus", "send",
         "--channel", "dm", "--to", "sv-1", "--to", "sv-2",
         "--payload", '{"text":"x"}'],
        capture_output=True, text=True, cwd=str(stub_tree), env=env)
    seen = [json.loads(l)["env"] for l in proc.stdout.splitlines()
            if l.strip().startswith("{")]
    assert seen, proc.stdout + proc.stderr
    for e in seen:
        assert e.get("BEACON_BUS_RECIPIENT_USER", "") == "", (
            "呼び出し元の古い値が漏れています: " + repr(e.get("BEACON_BUS_RECIPIENT_USER")))
