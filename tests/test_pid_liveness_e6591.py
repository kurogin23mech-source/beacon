"""ms-133 / e-6591 — 生存確認が Windows で相手プロセスを殺してはならない。

Python の ``os.kill`` は Windows で signal 0 を含むあらゆる signal を
``TerminateProcess`` に写す。だから POSIX の定石 ``os.kill(pid, 0)`` は Windows
では「生きてますか」ではなく「死んでください」になる。Beacon はこの問いを 5 箇所
で持っており、e-6591 の時点で 4 箇所が素の ``os.kill`` を呼んでいた:

  lib/session.py                      bridge claim の所有者判定
  scripts/codex-receive-loop.py       stale pidfile 判定
  scripts/bcodex-watcher.py           bcodex wrapper 監視
  lib/version_skew.py                 daemon の版ズレ検知
  beacon_cli/hooks/context_monitor.py state 掃除 (e-6588 で POSIX gate 済み)

修正は単一 probe ``lib/pid_liveness.py`` への集約。ここで固定するのは 2 つ:
  A. probe の契約 (POSIX の意味論を保つ / Windows 経路は os.kill を触らない)
  B. drift guard が **本当に drift で赤くなる** こと (= test-the-test)。
     緩い検査は「守っているつもりの偽の安全」になるので、guard 自身を合成 drift
     に当てて赤を実証する。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check-pid-liveness.py"

import pid_liveness  # noqa: E402  (conftest puts lib/ on sys.path)


# --------------------------------------------------------------------------- #
# A. probe の契約
# --------------------------------------------------------------------------- #

def test_live_process_reads_alive():
    assert pid_liveness.pid_alive(os.getpid()) is True


def test_reaped_child_reads_dead():
    """終了して回収済みの子 pid は死亡と読めること (= 掃除が前に進む)。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_liveness.pid_alive(proc.pid) is False


@pytest.mark.parametrize("bad", [0, -1, True, False, None, "123", 1.0])
def test_non_pid_inputs_are_rejected(bad):
    """pid として無効な入力は False。bool は int の subclass なので明示的に弾く。"""
    assert pid_liveness.pid_alive(bad) is False


def test_permission_error_counts_as_alive(monkeypatch):
    """別 user のプロセス (EPERM) は「居る」= 生存。claim を奪わないため。"""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(PermissionError()))
    assert pid_liveness.pid_alive(4242) is True


def test_unknown_oserror_degrades_to_alive(monkeypatch):
    """判定不能は保守側 = 生存。死亡と推測すると生きた claim を奪う。"""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(OSError("weird")))
    assert pid_liveness.pid_alive(4242) is True


def test_windows_path_never_calls_os_kill(monkeypatch):
    """**この test が e-6591 の核心。**

    ``os.name == "nt"`` のとき probe は ``os.kill`` を一切呼んではならない。
    呼べば相手プロセスが終了する。os.kill を爆弾に差し替えて、踏んだら落ちる形で
    固定する (Mac 上でも Windows 経路の安全性を検証できる唯一の方法)。
    """
    def _bomb(*_a, **_k):
        raise AssertionError(
            "Windows 経路が os.kill を呼びました — これは対象プロセスを "
            "TerminateProcess で終了させます (e-6591 の退行)"
        )

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", _bomb)
    # ctypes 経路は Mac には無い → except で保守側 True に倒れる。
    # 重要なのは戻り値ではなく「os.kill を踏まなかった」こと。
    assert pid_liveness.pid_alive(4242) is True


def test_session_pid_alive_delegates_to_the_probe(monkeypatch):
    """lib/session.py の bridge claim 判定が probe 経由になっていること。"""
    import session

    calls: list[int] = []
    monkeypatch.setattr(pid_liveness, "pid_alive", lambda pid: calls.append(pid) or True)
    assert session._pid_alive(4242) is True
    assert calls == [4242]


# --------------------------------------------------------------------------- #
# B. drift guard が本当に drift で赤くなるか (test-the-test)
# --------------------------------------------------------------------------- #

def _run_guard(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD), "--strict", "--root", str(root)],
        capture_output=True, text=True,
    )


def test_guard_is_green_on_the_real_tree():
    res = _run_guard(ROOT)
    assert res.returncode == 0, res.stdout + res.stderr


def test_guard_goes_red_on_a_new_bare_probe(tmp_path):
    """新しく素の os.kill(pid, 0) を書いたら落ちること。"""
    (tmp_path / "newcode.py").write_text(textwrap.dedent("""
        import os
        def is_running(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
    """), encoding="utf-8")
    res = _run_guard(tmp_path)
    assert res.returncode == 1
    assert "is_running" in res.stderr


def test_guard_goes_red_on_from_os_import_kill(tmp_path):
    """``from os import kill`` で属性参照を避けた抜け道も塞がれていること。"""
    (tmp_path / "sneaky.py").write_text(
        "from os import kill\ndef alive(pid):\n    kill(pid, 0)\n    return True\n",
        encoding="utf-8",
    )
    res = _run_guard(tmp_path)
    assert res.returncode == 1
    assert "alive" in res.stderr


def test_guard_ignores_real_signals(tmp_path):
    """本物の kill (SIGTERM 等) は正当な操作なので false positive にしない。"""
    (tmp_path / "stopper.py").write_text(
        "import os, signal\ndef stop(pid):\n    os.kill(pid, signal.SIGTERM)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 0


def _write_context_monitor(tmp_path: Path, *, gated: bool) -> None:
    hooks = tmp_path / "beacon_cli" / "hooks"
    hooks.mkdir(parents=True)
    gate = '    if os.name != "posix":\n        return\n' if gated else ""
    (hooks / "context_monitor.py").write_text(
        "import os\n"
        "def _pid_alive(pid):\n"
        "    try:\n"
        "        os.kill(pid, 0)\n"
        "        return True\n"
        "    except ProcessLookupError:\n"
        "        return False\n"
        "\n"
        "def _prune_stale_state_files(state_dir, keep):\n"
        + gate +
        "    for f in state_dir:\n"
        "        if _pid_alive(f):\n"
        "            continue\n",
        encoding="utf-8",
    )


def test_guard_accepts_the_allowlisted_site_while_its_gate_stands(tmp_path):
    _write_context_monitor(tmp_path, gated=True)
    assert _run_guard(tmp_path).returncode == 0


def test_guard_goes_red_when_the_allowlisted_gate_is_removed(tmp_path):
    """allowlist はコメントではなく gate の実在を根拠にしていること。

    ここが緩いと「allowlist に載っているから安全」という名ばかりの guard になる:
    誰かが POSIX gate を外しても素通りしてしまう。
    """
    _write_context_monitor(tmp_path, gated=False)
    res = _run_guard(tmp_path)
    assert res.returncode == 1
    assert "_prune_stale_state_files" in res.stderr
