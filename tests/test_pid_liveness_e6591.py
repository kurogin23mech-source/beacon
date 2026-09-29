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
    """本物の kill (SIGTERM / literal 非 0 / SIGKILL) は正当なので false positive にしない。"""
    (tmp_path / "stopper.py").write_text(
        "import os, signal\n"
        "def stop(pid):\n"
        "    os.kill(pid, signal.SIGTERM)\n"
        "def hard(pid):\n"
        "    os.kill(pid, 9)\n"
        "def kill9(pid):\n"
        "    os.kill(pid, signal.SIGKILL)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 0


# --- literal 0 以外の抜け道 (2026-09-29 独立 AX レビューが実証した穴) ------- #
#
# 当初 guard は「第 2 引数が literal 0」だけを見ていた。独立 judge が合成入力を
# 実際に走らせて 2 つの素通りを実証し (定数化 / *args)、レビュー中に 3 つ目
# (キーワード) も確認された。literal だけを見る検査は「ok と出るのに Windows-unsafe
# な呼び出しが残る」偽の安全を作る。現在の guard は「signal が非 0 と *証明できない*
# 呼び出しはすべて検出」に倒しており、以下がその回帰テスト。

def test_guard_catches_named_constant_signal(tmp_path):
    """``_PROBE = 0`` のような定数化リファクタで素通りしないこと。

    AI が自発的にやりがちな『マジックナンバーの定数化』で穴が開く形。実行可能な
    Windows-unsafe 経路そのもの。
    """
    (tmp_path / "named.py").write_text(
        "import os\n_PROBE = 0\ndef alive(pid):\n    os.kill(pid, _PROBE)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 1


def test_guard_catches_starred_args(tmp_path):
    """``os.kill(*args)`` で引数が不透明な形も検出すること (実行可能な経路)。"""
    (tmp_path / "starred.py").write_text(
        "import os\nARGS = (4242, 0)\ndef alive():\n    os.kill(*ARGS)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 1


def test_guard_catches_keyword_signal(tmp_path):
    """``os.kill(pid, sig=0)`` も検出すること。

    CPython では実際には TypeError (posix.kill はキーワードを取らない) なので
    runtime の危険ではないが、検出しても害が無いので安全側に含めている。
    """
    (tmp_path / "kw.py").write_text(
        "import os\ndef alive(pid):\n    os.kill(pid, sig=0)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 1


def test_guard_catches_zero_valued_signal_name(tmp_path):
    """``signal.SIG_DFL`` は値 0 なので『本物の signal 名』として免除しないこと。"""
    (tmp_path / "sigdfl.py").write_text(
        "import os, signal\ndef probe(pid):\n    os.kill(pid, signal.SIG_DFL)\n",
        encoding="utf-8",
    )
    assert _run_guard(tmp_path).returncode == 1


def test_guard_ignores_uncallable_single_arg_form(tmp_path):
    """``os.kill(pid)`` は signal が無く TypeError になるだけなので騒がないこと."""
    (tmp_path / "onearg.py").write_text(
        "import os\ndef broken(pid):\n    os.kill(pid)\n", encoding="utf-8"
    )
    assert _run_guard(tmp_path).returncode == 0


# --- 共有 lib-dir リゾルバ (両 judge が指摘した重複の解消先) ---------------- #

def test_shared_resolver_prefers_source_then_wheel(tmp_path):
    """``lib/`` → ``_bundled_lib/`` の順、どちらも無ければ ``lib/`` に fail-open。

    PR #764 の独立レビュー 2 体が、この規則が bcodex-watcher に写されている点を
    指摘した (保守性 medium / AX low の consensus)。解消先がこの 1 関数。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from _install_paths import resolve_lib_dir

    src_root = tmp_path / "src"
    (src_root / "lib").mkdir(parents=True)
    assert resolve_lib_dir(src_root) == src_root / "lib"

    whl_root = tmp_path / "beacon_cli"
    (whl_root / "_bundled_lib").mkdir(parents=True)
    assert resolve_lib_dir(whl_root) == whl_root / "_bundled_lib"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_lib_dir(empty) == empty / "lib"


def test_daemon_resolver_delegates_to_the_shared_one(tmp_path):
    """codex-receive-loop の ``_resolve_lib_dir`` が共有リゾルバと同じ答えを返すこと。

    既存の 4 呼び出し元が使う名前は残しつつ、規則の定義は 1 箇所であることを固定する
    (重複を消したのに答えがズレたら意味がない)。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "crl_shared_resolver", ROOT / "scripts" / "codex-receive-loop.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sys.path.insert(0, str(ROOT / "scripts"))
    from _install_paths import resolve_lib_dir

    for layout in ("lib", "_bundled_lib"):
        root = tmp_path / layout
        (root / layout).mkdir(parents=True)
        assert mod._resolve_lib_dir(root) == resolve_lib_dir(root)


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
