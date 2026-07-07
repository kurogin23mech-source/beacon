"""SessionStart hook: beacon の自動アップデート (ms-103)。

bclaude セッション開始時に呼ばれ、新しい版が出ていれば自動でインストールする。
サーバに新機能が入っても各ユーザーの CLI / bridge が古いままだと全面適用が遅れる
ため、起動を機に自然に最新へ追従させる。

契約 (= Claude Code SessionStart hook):
  * Stdin: SessionStart JSON (中身は使わないが pipe を詰まらせないよう drain する)。
  * Stdout: 空 (通知が要るときだけ 1 行。セッション開始をブロックしない)。
  * Return code: 常に 0 (更新可否に関わらずセッション開始を絶対に妨げない)。

設計:
  - **1 日 1 回だけ** PyPI (beacon-ai) の最新版を確認する (~/.beacon/auto-update.json に
    last_check を記録)。毎起動でネットに行かない。
  - 新版があれば install 方法を判定して更新を **detached (バックグラウンド)** で起動し、
    hook 自体は即 return する (= セッション開始を待たせない)。実際の反映は次回起動から。
  - install 方法別:
      brew          → brew upgrade beacon
      editable/git  → git status がクリーンなら git pull --ff-only (汚れてたら通知のみ)
      pipx          → pipx upgrade beacon-ai
      pip           → pip install -U beacon-ai
    続けて beacon skill install (新 CLI の Skill を反映)。
  - すべて fail-open / fail-silent: ネットワーク障害・不正リリース・失敗時は黙って
    旧版で続行し、セッション開始を壊さない。
  - opt-out: 環境変数 BEACON_AUTO_UPDATE=0 で完全に無効化。

foreground (hook 本体) は「cache gate + PyPI 確認」だけを短時間で行い、更新が必要な
ときのみ自分自身を ``--apply`` で detached 起動する。重い upgrade は background に逃がす。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

PACKAGE = "beacon-ai"          # PyPI 上の配布名 (内部 CLI 名は beacon)
CHECK_INTERVAL_SEC = 24 * 3600  # 1 日 1 回
PYPI_TIMEOUT_SEC = 4
# (b) 熟成待ち: 公開直後の版は壊れリリースを yank する猶予として自動更新の対象に
# しない。この秒数だけ経った版のみ自動更新する。
MATURITY_SEC = 24 * 3600


def _state_dir() -> Path:
    d = Path(os.path.expanduser("~")) / ".beacon"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _cache_path() -> Path:
    return _state_dir() / "auto-update.json"


def _log_path() -> Path:
    return _state_dir() / "auto-update.log"


def _read_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _current_version() -> Optional[str]:
    """Running beacon の __version__ (import せず軽量に取得)。"""
    try:
        from beacon_cli._version import __version__  # type: ignore
        return __version__
    except Exception:
        try:
            r = subprocess.run(
                ["beacon", "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8
            )
            if r.returncode == 0:
                # "beacon 0.56.0" → "0.56.0"
                return r.stdout.strip().split()[-1]
        except Exception:
            pass
    return None


def _pypi_latest_info():
    """PyPI の (最新版, 公開 epoch 秒) を返す。障害時は (None, None) (fail-silent)。

    公開時刻は releases[version] のファイル群の upload_time の最小値 (= 最初に
    上がった時刻) を使う。熟成待ち (b) の判定に使う。
    """
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{PACKAGE}/json",
            headers={"User-Agent": "beacon-auto-update"},
        )
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = data.get("info", {}).get("version")
        upload_epoch = None
        files = data.get("releases", {}).get(version, []) if version else []
        stamps = []
        for f in files:
            iso = f.get("upload_time_iso_8601") or f.get("upload_time")
            if not iso:
                continue
            try:
                import datetime
                s = iso.replace("Z", "+00:00")
                stamps.append(datetime.datetime.fromisoformat(s).timestamp())
            except Exception:
                pass
        if stamps:
            upload_epoch = min(stamps)
        return version, upload_epoch
    except Exception:
        return None, None


def _parse_ver(v: str):
    """'0.56.0' → (0,56,0)。数値でない部分は無視して比較用 tuple にする。"""
    parts = []
    for chunk in str(v).split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_ver(latest) > _parse_ver(current)
    except Exception:
        return False


def _beacon_root() -> Optional[Path]:
    """この beacon_cli パッケージのソースルート (editable なら repo root)。"""
    try:
        # <root>/beacon_cli/hooks/session_start.py → <root>
        return Path(__file__).resolve().parents[2]
    except Exception:
        return None


def _detect_method() -> str:
    """install 方法を判定: 'git' | 'brew' | 'pipx' | 'pip' | 'unknown'."""
    root = _beacon_root()
    # editable / 手動 clone: パッケージソースの上に .git がある
    if root and (root / ".git").exists():
        return "git"
    # pipx: PIPX_HOME 配下 or `pipx list` に載っている
    try:
        if root and "pipx" in str(root).lower():
            return "pipx"
    except Exception:
        pass
    if shutil.which("pipx"):
        try:
            r = subprocess.run(
                ["pipx", "list", "--json"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10
            )
            if r.returncode == 0 and PACKAGE in r.stdout:
                return "pipx"
        except Exception:
            pass
    # brew: brew --prefix beacon が通る
    if shutil.which("brew"):
        try:
            r = subprocess.run(
                ["brew", "--prefix", "beacon"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8
            )
            if r.returncode == 0 and r.stdout.strip():
                return "brew"
        except Exception:
            pass
    # 最後の砦: pip で入っているとみなす
    return "pip"


def _run(cmd, log) -> int:
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    try:
        r = subprocess.run(cmd, stdout=log, stderr=log, timeout=600)
        return r.returncode
    except Exception as e:
        log.write(f"  (failed: {e})\n")
        return 1


# CLI が Python レベルでクラッシュした痕跡 (= import / 構文 / dispatch が壊れた)。
_CRASH_MARKERS = ("Traceback (most recent call last)", "ModuleNotFoundError",
                  "ImportError", "SyntaxError", "AttributeError:")


def _cmd_crashed(args, timeout, env=None) -> Optional[bool]:
    """essential コマンドを非破壊で走らせ、Python クラッシュしたか判定する。

    Returns:
      False — クラッシュしなかった (= 起動 OK。警告や non-zero exit は許容)
      True  — クラッシュした (= traceback / import error 等を検出、bricked)
      None  — timeout / 実行不能 (= 判定不能、smoke では無視する)
    """
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    out = (r.stdout or "") + (r.stderr or "")
    return any(m in out for m in _CRASH_MARKERS)


def _smoke_check() -> bool:
    """更新後、必須 CLI コマンドが実際に動くかを検証する (= a: 自爆検知)。

    `--version` だけでは import は通るが dispatch が壊れているケースを見逃すため、
    復旧に不可欠な essential コマンドを **非破壊 (dry-run / read-only)** で叩き、
    Python クラッシュ (traceback / import error) が出たら bricked と判断する:
      - beacon --version            : そもそも CLI が起動するか (必須)
      - beacon update (dry-run)      : 復旧経路 (これが壊れると自力回復不能) ← 最重要
      - beacon doctor               : 自己診断
      - beacon bus budget show      : messaging 経路 (任意)
    警告や non-zero exit (= doctor が drift 検出等) は許容し、クラッシュのみ落とす。
    timeout は判定不能として無視 (= 遅い ≠ 壊れている)。
    """
    # (1) 起動できるか (ここだけ厳密に exit 0 + 出力を要求)。
    try:
        r = subprocess.run(
            ["beacon", "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=20, stdin=subprocess.DEVNULL,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
    except Exception:
        return False

    # (2) essential コマンドが Python クラッシュしないか。
    dry_env = dict(os.environ)
    dry_env["BEACON_UPDATE_CHECK"] = "1"   # update を dry-run 化 (変更しない)
    checks = [
        (["beacon", "update"], 25, dry_env),      # 復旧経路 (最重要)
        (["beacon", "doctor"], 30, None),         # 自己診断
        (["beacon", "bus", "budget", "show"], 20, None),  # messaging (任意)
    ]
    for args, timeout, env in checks:
        if _cmd_crashed(args, timeout, env) is True:
            return False
    return True


def _rollback(method: str, root, prev, log) -> None:
    """(a) 更新後 smoke 失敗時、直前状態へ戻す。全部 best-effort。"""
    log.write(f"  ROLLBACK (method={method}, prev={prev}) ...\n")
    try:
        if method == "git" and root and prev:
            _run(["git", "-C", str(root), "reset", "--hard", prev], log)
        elif method == "pipx" and prev:
            _run(["pipx", "install", "--force", f"{PACKAGE}=={prev}"], log)
        elif method == "pip" and prev:
            _run([sys.executable, "-m", "pip", "install", f"{PACKAGE}=={prev}"], log)
        elif method == "brew":
            # brew は旧版へのクリーンな downgrade が難しい。自動ロールバックせず警告。
            log.write("  (brew は自動ロールバック不可。`brew install beacon` で再導入 or "
                      "手動対応してください)\n")
    except Exception as e:
        log.write(f"  rollback failed: {e}\n")


def _apply_update(latest: str, prev: str = "") -> None:
    """install 方法別に更新 → smoke check → 失敗ならロールバック (background worker)。

    全部 fail-silent。(a) 壊れリリースで CLI が起動しなくなる自爆を、更新直後の
    `beacon --version` smoke + 直前状態への自動ロールバックで防ぐ。
    """
    method = _detect_method()
    root = _beacon_root()
    try:
        log = open(_log_path(), "a", encoding="utf-8")
    except Exception:
        return
    with log:
        log.write(f"\n=== auto-update {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"→ {latest} (method={method}, prev={prev}) ===\n")

        # ロールバック先を確定 (git は直前 commit、pip/pipx は直前 version)。
        rollback_ref = prev
        if method == "git":
            if root is None:
                log.write("  git root not resolved — skip\n")
                return
            try:
                dirty = subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain"],
                    capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
                )
                if dirty.returncode == 0 and dirty.stdout.strip():
                    log.write("  working tree dirty — skip auto pull "
                              "(手動で git pull してください)\n")
                    return
                head = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
                )
                if head.returncode == 0:
                    rollback_ref = head.stdout.strip()
            except Exception as e:
                log.write(f"  git prep failed ({e}) — skip\n")
                return
            _run(["git", "-C", str(root), "pull", "--ff-only"], log)
        elif method == "brew":
            _run(["brew", "upgrade", "beacon"], log)
        elif method == "pipx":
            _run(["pipx", "upgrade", PACKAGE], log)
        else:  # pip
            _run([sys.executable, "-m", "pip", "install", "-U", PACKAGE], log)

        # (a) smoke check: 更新後に CLI が起動するか。壊れていたらロールバック。
        if not _smoke_check():
            log.write("  SMOKE CHECK FAILED after update (CLI が起動しない)\n")
            _rollback(method, root, rollback_ref, log)
            if _smoke_check():
                log.write("  rollback で復旧しました\n")
            else:
                log.write("  ⚠ rollback 後も起動不可。手動対応が必要です\n")
            log.write("=== auto-update aborted (rolled back) ===\n")
            return

        # 新 CLI の Skill を反映 (更新後の beacon を呼ぶ)。
        if shutil.which("beacon"):
            _run(["beacon", "skill", "install"], log)
        log.write("=== auto-update done ===\n")


def _spawn_apply(latest: str, prev: str = "") -> None:
    """自分自身を ``--apply <latest> <prev>`` で detached 起動し、hook は即 return。"""
    try:
        kwargs = {}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NO_WINDOW
            kwargs["creationflags"] = 0x00000008 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "beacon_cli.hooks.session_start",
             "--apply", latest, prev or ""],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # background worker 経路: `--apply <latest> [<prev>]`
    if argv and argv[0] == "--apply":
        latest = argv[1] if len(argv) > 1 else ""
        prev = argv[2] if len(argv) > 2 else ""
        if latest:
            _apply_update(latest, prev)
        return 0

    # foreground (SessionStart hook 本体)
    try:
        # stdin を drain (Claude Code の pipe を詰まらせない)。
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass

    # opt-out
    if os.environ.get("BEACON_AUTO_UPDATE", "1") == "0":
        return 0

    # 1 日 1 回に絞る (cache gate)。
    cache = _read_cache()
    now = time.time()
    try:
        last = float(cache.get("last_check", 0))
    except Exception:
        last = 0
    if now - last < CHECK_INTERVAL_SEC:
        return 0
    cache["last_check"] = now

    current = _current_version()
    latest, upload_epoch = _pypi_latest_info()
    if latest:
        cache["latest_seen"] = latest
    _write_cache(cache)

    if not current or not latest or not _is_newer(latest, current):
        return 0

    # (b) 熟成待ち: 公開直後の版は yank 猶予として見送る (壊れリリースの一斉配布防止)。
    if upload_epoch is not None and (now - upload_epoch) < MATURITY_SEC:
        return 0

    # 新版あり → detached で更新を起動し (更新後セルフチェック + 失敗時ロールバック
    # 付き)、通知だけ 1 行出す (反映は次回起動から)。current も渡してロールバック先にする。
    _spawn_apply(latest, current)
    method = _detect_method()
    if method == "git":
        sys.stdout.write(
            f"📦 beacon {latest} が利用可能です（現在 {current}）。"
            f"自動で git pull を試みます（working tree がクリーンな場合）。\n"
        )
    else:
        sys.stdout.write(
            f"📦 beacon {latest} が利用可能です（現在 {current}）。"
            f"バックグラウンドで更新中です。次回起動から反映されます。\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
