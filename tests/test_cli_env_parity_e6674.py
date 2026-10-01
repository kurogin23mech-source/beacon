"""旗 → env の写像が両フロントで揃っていることの機械検査 (ms-160 e-6674).

旗の **名前** を比べる check-cli-help-drift.py はこの級を原理的に見られない。
片方のフロントに無い旗は「不一致」ではなく「不在」で、両方に無ければ揃って
いると数えられるため。実際に落ちていたのは旗そのものではなく、argv を環境変数へ
写す対応表の方で、それは 2 言語で手書きされている。

実測された代償: beacon doc add --force / search --source / deploy record
--version / cloud list --json ほか約 45 件が bash では通り python では
`unrecognized arguments` で落ちる。PR #778 の bus send --recipient-confirmed は
Windows から cross-user DM を **送れない** 状態を作っていた。

この試験が固定するのは 3 つ:
  1. 現状が緑であること (= KNOWN_GAPS で既存の負債を許容した状態)
  2. 新しい drift を入れると赤くなること (= 検出力の実測)
  3. 直した負債の行を残すと赤くなること (= stale 検出。残った行は同じ場所の
     次の退行を黙って免除する)
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _checker():
    spec = importlib.util.spec_from_file_location(
        "cli_env_parity", ROOT / "scripts" / "check-cli-env-parity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_new_env_parity_drift_today():
    mod = _checker()
    rows = mod.collect()
    assert rows == [], (
        "新しい 旗→env の drift です。該当フロントに写像を足すか、"
        "フロントが渡すべき値でないなら AMBIENT_ENV に入れてください: " + repr(rows))


def test_the_backlog_is_recorded_not_hidden():
    """KNOWN_GAPS は『判定済みで許した』ではなく『記録された負債』。空にすり替えて
    検査を無意味にしていないことを確認する。"""
    mod = _checker()
    assert len(mod.KNOWN_GAPS) > 0
    for row in mod.KNOWN_GAPS:
        assert len(row) == 3 and row[2] in ("bash", "python"), row


def _shadow(tmp_path):
    dst = tmp_path / "repo"
    (dst).mkdir()
    for part in ("lib", "bin", "beacon_cli"):
        shutil.copytree(ROOT / part, dst / part)
    return dst


def test_a_new_unpassed_env_is_reported(tmp_path):
    """test-the-test: 新しい動詞が env を読むのにどちらのフロントも渡さない状態を
    作ると報告されること。検出力を実測しないガードは、何も検出しないガードと
    見分けがつかない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    # a verb that reads a brand-new env name, dispatched by both frontends
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_NEWLY_UNWIRED', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow: python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    disp = repo / "beacon_cli" / "dispatch.py"
    disp.write_text(disp.read_text(encoding="utf-8")
                    + '\ndef _handle_probe_e6674(root, args):\n'
                      '    return _run_commands_py(root, "probe_e6674_demo", {})\n',
                    encoding="utf-8")
    rows = mod.collect(repo)
    names = {(v, e) for v, e, _ in rows}
    assert ("probe_e6674_demo", "BEACON_NEWLY_UNWIRED") in names, rows


def test_an_env_the_frontend_does_pass_is_not_reported(tmp_path):
    """逆向き: フロントが実際に渡している env は報告しない。誤検出するガードは
    無視されるようになり、無視されたガードは何も守らない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_WIRED_OK', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow\nBEACON_WIRED_OK="$x" python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    disp = repo / "beacon_cli" / "dispatch.py"
    disp.write_text(disp.read_text(encoding="utf-8")
                    + '\ndef _handle_probe_e6674(root, args):\n'
                      '    env = {"BEACON_WIRED_OK": args.x}\n'
                      '    return _run_commands_py(root, "probe_e6674_demo", env)\n',
                    encoding="utf-8")
    names = {(v, e) for v, e, _ in mod.collect(repo)}
    assert ("probe_e6674_demo", "BEACON_WIRED_OK") not in names, mod.collect(repo)


def test_ambient_env_is_not_reported(tmp_path):
    """運用者 / 試験用の env (doctor の skip 等) は、そもそもフロントが渡す値では
    ないので報告しない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_DEBUG', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow: python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    names = {(v, e) for v, e, _ in mod.collect(repo)}
    assert ("probe_e6674_demo", "BEACON_DEBUG") not in names


def test_stale_known_gap_is_reported(monkeypatch):
    """直った負債の行を残すと報告されること。残った行は同じ場所の次の退行を
    黙って免除するので、stale 検出は負債リストと対になっている
    (姉妹ガード check-cli-help-drift / check-print-before-save と同じ規律)。"""
    mod = _checker()
    mod.KNOWN_GAPS.add(("verb_that_does_not_exist", "BEACON_NOPE", "bash"))
    assert mod.main() == 1
