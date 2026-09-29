"""One place that answers "is this pid still running?" — safely on every OS.

なぜこのモジュールが要るか (ms-133 / e-6591)
--------------------------------------------
Beacon は「あの pid はまだ生きているか」を 5 箇所で訊く: bridge claim の所有者
判定 (``lib/session.py``)、Codex daemon の stale pidfile 判定
(``scripts/codex-receive-loop.py``)、bcodex watcher の wrapper 監視
(``scripts/bcodex-watcher.py``)、daemon の版ズレ検知 (``lib/version_skew.py``)、
context-monitor の state 掃除 (``beacon_cli/hooks/context_monitor.py``)。

5 箇所すべてが POSIX の定石 ``os.kill(pid, 0)`` を使っていた。ところが Windows
ではこれは**問い合わせにならない**: CPython の ``os.kill`` は signal 0 を含む
あらゆる signal を ``TerminateProcess`` に写すため、「生きてますか」と訊いた
時点で相手を**終了させる**。Windows の利用者は、CLI が claim の所有者を確認
しただけで自分の bridge や bcodex wrapper を落としていた。

e-6588 で 5 箇所のうち 1 つ (context_monitor) は呼び出し側を POSIX 限定に
gate して塞いだ。このモジュールは残りを塞ぐ — 問いに**両 OS で正しい単一の
実装**を与え、どの呼び出し側も platform の規則を覚えなくてよい形にする。
新しい生存判定が素の ``os.kill(pid, 0)`` に戻らないことは
``scripts/check-pid-liveness.py`` (strict drift guard) が機械的に見張る。

Windows 側は ``psutil`` ではなく ``ctypes`` 経由の ``OpenProcess`` +
``GetExitCodeProcess`` を使う。Beacon の CLI は意図的に標準ライブラリのみで
動く (pipx が compiled 依存なしで入れられる) ため、依存を増やさない。

判定不能時の方針: **保守側 = 生きている扱い**
---------------------------------------------
呼び出し側はいずれも「何かを引き継いでよいか」(claim / pidfile / state ファイル)
の判断にこれを使う。判定不能を「死んでいる」と推測すると生きている session の
claim を奪う。「生きている」と推測した場合の代償は掃除が次回まで遅れるだけ。
非対称なので、迷ったら True を返す。
"""
from __future__ import annotations

import os

# GetExitCodeProcess が「まだ動いている」ときに返す値 (winbase.h STILL_ACTIVE)。
_STILL_ACTIVE = 259
# OpenProcess に要求する最小の権限。PROCESS_QUERY_INFORMATION より弱く、
# 昇格していない呼び出し元でも他 user のプロセスに対して通ることがある。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# OpenProcess がその id のプロセスオブジェクトを見つけられなかった = 死んでいる。
# 他の失敗 (5 = ERROR_ACCESS_DENIED など) は「居るが覗けない」なので生存扱い。
_ERROR_INVALID_PARAMETER = 87


def pid_alive(pid: int) -> bool:
    """``pid`` がこのホストで生きているプロセスなら True。

    pid が int でない / bool / 0 以下 のときは False (= 判定対象として無効)。
    それ以外は platform ごとの probe に委ね、判定できなければ True に倒す。
    副作用は無い — とくに **Windows で対象プロセスを終了させない**ことが
    このモジュールの存在理由なので、ここから ``os.kill`` は決して呼ばれない。
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    """POSIX: ``kill -0``。ここが ``os.kill(pid, 0)`` の唯一の正規の置き場所。

    ``ProcessLookupError`` は死亡、``PermissionError`` は「別 user のプロセスだが
    存在する」= 生存。それ以外の OS 由来の異常は判定不能として生存に倒す。
    """
    try:
        os.kill(pid, 0)  # beacon: pid-liveness-primitive
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows: ``OpenProcess`` + ``GetExitCodeProcess``。``os.kill`` は使わない。

    既知の限界: 終了コードが偶然 259 (= STILL_ACTIVE) のプロセスは生存と読める。
    保守側 (= 生存) に外れるので、上の判定不能の方針と向きが一致している。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # ctypes が使えない / API 呼び出しが想定外に失敗した → 判定不能。
        return True
