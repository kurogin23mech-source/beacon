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

**穴 3: 「赤くなった」を「ガードが効いた」と読み違える**
変異が構文を壊したり対象を収集できなかったりしても pytest は赤くなる。赤の
**理由** まで見ないと、壊し方が下手なだけのケースが「守られている」に化ける。
最初は出力の自由文を ``"error in" in 出力`` の部分一致で探していたが、テストの
docstring に「a syntax error in the ledger」と書いてあるだけで INVALID と誤判定
した (2026-10-27 実測)。緩い一致はこのリポジトリが繰り返し踏んだ形なので、
pytest の **終了コード** と **要約行** という機械が出す構造だけを見る。
→ ``classify_pytest_result`` に 1 箇所で持つ。各ハーネスに写すと、1 本だけ直って
残りが旧判定のまま並ぶ (PR #818 の独立レビューが、新しい 1 本だけ直り姉妹 3 本に
旧判定が残っている状態を実際に指摘した)。

**穴 4: ハーネス自身が「黙って危ないことをする」入口になる**
ハーネスは共有ファイル (``bin/beacon`` は 90 本以上のテストが subprocess で叩く)
を in-place で書き換える。(a) ``--help`` や打ち間違えたフラグを渡しても黙って
本物の変異実行が走り exit 0 で終わる、(b) 2 本同時に走ると互いの変異を読んで
でたらめな結果を出し、復元が相手の書き込みを巻き戻す (lost update)。どちらも
「お願い」(docstring の『pytest から呼ぶな』) でしか守られていなかった。
→ ``guarded_main`` が、引数を先に裁いて (未知は非 0 で拒否、``--help`` は
ファイルに触らず使い方を出す)、排他ロックを取ってから本体を呼ぶ。ロックが
取れなければ何も書かずに断る。ロックで防げるのはハーネス同士の衝突まで。素の
``pytest`` との同時実行は防げないので、ハーネスは pytest に配線しない (CI は
``-n 4`` 並列で、隣の worker が変異中のファイルを読む)。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import re
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


# --- 穴 3: 赤の理由を構造で見る --------------------------------------------

#: pytest の終了コード。1 = テストが落ちた (= 本来狙っていた赤)、
#: 2 interrupted (収集時 error もここ) / 3 internal error / 4 usage error /
#: 5 no tests collected。0 は全部緑 (= MISS)。
PYTEST_TESTS_FAILED = 1
PYTEST_INVALID_RCS = (2, 3, 4, 5)
#: 要約行 (`3 failed, 1 error in 0.4s` のような機械が出す 1 行) の中で
#: error が **数えられている** 形だけを見る。本文の自由文は見ない。
_SUMMARY_ERROR_COUNT = re.compile(r"\b\d+ errors?\b")


def classify_pytest_result(r, tail: str):
    """``(ガードが捕まえたか, 表示用の末尾)`` を pytest の構造だけから決める。

    ``r`` は ``run_pytest`` が返す CompletedProcess、``tail`` はその要約行。
    収集/起動の失敗 (終了コード 2〜5) と、終了コード 1 でも要約行が error を
    数えている場合は **INVALID** (= 捕まえたとは数えない)。出力の自由文を
    substring で探さない理由は module docstring の穴 3。
    """
    rc = r.returncode
    if rc in PYTEST_INVALID_RCS:
        return (False, f"INVALID (pytest rc={rc} — 収集/起動の失敗): {tail}")
    if rc == PYTEST_TESTS_FAILED and _SUMMARY_ERROR_COUNT.search(tail):
        return (False, f"INVALID (要約行が error を数えている): {tail}")
    return (rc != 0, tail)


def run_and_classify(targets, repo_root: pathlib.Path):
    """``run_pytest`` + ``classify_pytest_result`` を 1 回で。各ハーネスの
    ``run_one`` はこれを呼ぶ (判定を手元に写さない)。"""
    _went_red, tail, r = run_pytest(targets, repo_root)
    return classify_pytest_result(r, tail)


# --- 穴 4: 入口で裁く (引数 + 排他ロック) -----------------------------------

class LockHeld(RuntimeError):
    """別のハーネスが同じ repo のファイルを変異中。"""


def parse_cli(argv, usage: str) -> None:
    """引数を先に裁く。契約は **引数ゼロ**。

    ``-h`` / ``--help`` は使い方 (= 呼び出し側 module の docstring) を出して
    exit 0、それ以外の引数が 1 つでもあれば非 0 で拒否する。黙って捨てて既定で
    走ると「フラグが効いた」と「捨てられた」が出力から区別できない。
    """
    args = list(argv[1:])
    if not args:
        return
    if args[0] in ("-h", "--help"):
        print((usage or "").strip())
        raise SystemExit(0)
    print(f"error: このハーネスは引数を取りません (受け取った: {' '.join(args)})。",
          file=sys.stderr)
    print("       ファイルには触っていません。使い方は --help。", file=sys.stderr)
    raise SystemExit(2)


def lock_path_for(repo_root: pathlib.Path) -> pathlib.Path:
    """repo ごとに 1 つのロックファイル (一時ディレクトリ、repo 内は汚さない)。"""
    key = hashlib.sha1(str(pathlib.Path(repo_root).resolve()).encode()).hexdigest()[:12]
    return pathlib.Path(tempfile.gettempdir()) / f"invert-harness-{key}.lock"


@contextlib.contextmanager
def exclusive_lock(repo_root: pathlib.Path):
    """repo 単位の排他ロック。取れなければ ``LockHeld`` (何も書かない)。

    flock (POSIX) / msvcrt.locking (Windows) はプロセス終了で必ず解放される
    ので、クラッシュしてもロックが腐らない (pid ファイル方式だと腐る)。
    """
    path = lock_path_for(repo_root)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            if os.name == "nt":  # pragma: no cover - Windows
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.read(fd, 64).decode("utf-8", "replace").strip() or "?"
            raise LockHeld(
                f"another invert harness is running (pid {holder}) — "
                f"refusing to mutate shared files under {repo_root} "
                f"(lock: {path})")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield path
    finally:
        os.close(fd)  # ロックは close で解放される


def guarded_main(run, argv, usage: str, repo_root: pathlib.Path) -> int:
    """ハーネスの唯一の入口。引数を裁き、ロックを取ってから ``run()`` を呼ぶ。

    各 ``invert-*.py`` は ``sys.exit(ih.guarded_main(main, sys.argv, __doc__,
    ROOT))`` で終わる。``main`` 自体は変えない (本体と入口の分離)。
    """
    parse_cli(argv, usage)
    try:
        with exclusive_lock(repo_root):
            return int(run())
    except LockHeld as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
