"""反転ハーネスの共通部品 — 「ガードを壊したら本当に赤くなるか」を測る足場。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。反転ハーネスは
対象ファイルに 1 箇所ずつ壊し方を当てて pytest を走らせ、意図したテストが
赤くなることを確かめる道具だが、**ハーネス自身が偽の結果を作る穴**が 2 つある。
この module はその 2 つを閉じた形を 1 箇所に置く (各ハーネスに写すと、片方だけ
直されて drift する — 実際 e-7065 のハーネスは穴を持ったまま、e-7077 の
ハーネスだけが塞がれた状態で並んでいた)。

**穴 1: バイトコードキャッシュが変異版を持ち越す (2026-10-19 実測)**
macOS 同梱の Python は ``~/Library/Caches/com.apple.python/<ソースの絶対パス>``
に ``.pyc`` を書く。ハーネスは同じパスのファイルを変異させては戻すので、
**変異版の ``.pyc`` が残り、ハーネス実行後の通常のテストがソースと食い違う結果を
返す**。実測でハーネス後に単体テストが 2 件、偽の赤になった。向きが逆なら
**偽の緑**にもなる。``python3 -B`` は書き込みを止めるだけで読み込みは止めない
ので対策にならない。後から消す形も subprocess の書き込みと競合して取りこぼす。
→ (a) subprocess のキャッシュ先を実行ごとに隔離し、(b) 変異/復元のたびに
キャッシュ実体を退避 + 更新時刻を進めて鍵を変える。

**穴 2: ハーネスが環境を汚したまま終わる**
変異と復元を繰り返すので、復元漏れやキャッシュ残留が起きうる。黙って汚して
終わると、次に走る人が「ソースは正しいのにテストが落ちる (or 通る)」状態を
引き継ぐ。→ 最後にベースラインを再実行し、緑に戻らなければ大声で落ちる。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

# 実行ごとに固有のキャッシュ置き場 (穴 1-a)。プロセス内で 1 回だけ作る。
_RUN_CACHE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="invert-pycache-"))


def isolated_env() -> dict:
    """バイトコードキャッシュを隔離した環境変数 (穴 1-a)。"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(_RUN_CACHE_DIR)
    return env


def invalidate_bytecode_cache(path: pathlib.Path, repo_root: pathlib.Path) -> None:
    """``path`` のバイトコードキャッシュを無効化する (穴 1-b)。

    2 重に無効化する: (1) Apple のキャッシュ実体を ``.trash/`` へ退避する
    (このプロジェクトは ``rm`` 禁止なので消さずに移す)、(2) ソースの更新時刻を
    先に進めてキャッシュ鍵を確実に変える。
    """
    cache_root = pathlib.Path.home() / "Library" / "Caches" / "com.apple.python"
    if cache_root.is_dir():
        # キャッシュは絶対パスをそのまま掘った場所に置かれる
        mirrored = cache_root / str(path.resolve()).lstrip("/")
        for cached in mirrored.parent.glob(mirrored.stem + ".*.pyc"):
            trash = repo_root / ".trash" / "pycache-invert-harness"
            trash.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(cached), str(trash / cached.name))
            except OSError:
                pass
    try:
        future = time.time() + 2
        os.utime(path, (future, future))
    except OSError:
        pass


def restore(originals: dict, repo_root: pathlib.Path) -> None:
    """全対象ファイルを元に戻し、バイトコードキャッシュも無効化する。"""
    for p, text in originals.items():
        p.write_text(text, encoding="utf-8")
        invalidate_bytecode_cache(p, repo_root)


def run_pytest(targets, repo_root: pathlib.Path):
    """pytest を隔離環境で走らせ ``(赤くなったか, 出力の末尾)`` を返す。"""
    if isinstance(targets, str):
        targets = [targets]
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *targets, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=repo_root, capture_output=True, text=True, env=isolated_env())
    tail = (r.stdout + r.stderr).strip().split("\n")[-1]
    return (r.returncode != 0, tail, r)


def check_baseline(targets, repo_root: pathlib.Path) -> bool:
    """壊す前に全件緑であることを確かめる。False なら中止すべき。"""
    went_red, tail, r = run_pytest(targets, repo_root)
    if went_red:
        print("BASELINE RED — 壊す前からガードが落ちている。先にそれを直すこと。")
        print((r.stdout + r.stderr)[-2000:])
        return False
    print("baseline: " + tail + "\n")
    return True


def check_not_polluted(targets, repo_root: pathlib.Path) -> bool:
    """ハーネス実行後にベースラインが緑へ戻ることを実測する (穴 2)。"""
    went_red, tail, r = run_pytest(targets, repo_root)
    if went_red:
        print()
        print("NG: ハーネス実行後にベースラインが緑に戻りません。復元漏れか、")
        print("    バイトコードキャッシュの残留です。環境を汚したまま終わるのは")
        print("    偽の安全そのものなので失敗として扱います。")
        print((r.stdout + r.stderr)[-1500:])
        return False
    print("after: " + tail + "  (= 環境を汚していない)")
    return True
