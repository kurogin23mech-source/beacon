#!/usr/bin/env python3
"""server/ vs lib/ module name collision detector (ms-64 e-1618).

なぜこれが要るか:
  server/app.py の上部で

      sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

  によって lib/ ディレクトリが import path 先頭に挿入される。
  そのため `import store` のような書き方は **lib/ 配下が先に解決される**。
  つまり server/ に同名の *.py を作っても、lib/ 側に同名ファイルがあれば
  shadow されて壊れる。

  2026-06-13 にこれが本番障害を起こした (= 17 分間 CLI 機能停止、姉妹
  project trailnode-cd652b も巻き込み):
    PR #140 (e-1544 Phase 0) で server/store.py を新設 → 既存 lib/store.py が
    shadow → app.py の `import store as db` が lib/store.py を解決して
    AttributeError: module 'store' has no attribute 'get_or_create_user'。

  このスクリプトは pre-commit と CI で機械的に collision を検知し、
  「lib/ に同名がある名前を server/ で使わない」を構造的に enforce する。

検査ルール:
  - server/*.py の各ファイル名 (= module 名候補) を集める
  - lib/*.py に同名ファイルがあれば collision として fail
  - server/static/、server/__pycache__/、サブディレクトリ等は除外 (= 直下のみ)

example collision:
  server/store.py + lib/store.py    → fail (= 本件)
  server/core.py  + lib/core.py     → fail (= もし起きたら)

intentional shadow (= 「lib/ 側を意図的に参照する」場合):
  collision を発生させてはいけない。lib/ 側を import したいなら、それを
  指す名前 (= `import core` for lib/core.py) と server/ 側の名前を別にする。

実行:
  python3 scripts/check-server-lib-collision.py            # warn mode (= pre-commit)
  python3 scripts/check-server-lib-collision.py --strict   # exit 1 if collision (= CI gate)
"""
from __future__ import annotations

import argparse
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(REPO_ROOT, "server")
LIB_DIR = os.path.join(REPO_ROOT, "lib")


def _list_module_files(directory: str) -> set[str]:
    """Return the set of `*.py` basenames in `directory` (= 直下のみ、
    サブディレクトリ・キャッシュ・特殊ファイルは除外)。"""
    if not os.path.isdir(directory):
        return set()
    result = set()
    for entry in os.listdir(directory):
        if not entry.endswith(".py"):
            continue
        full = os.path.join(directory, entry)
        if not os.path.isfile(full):
            continue
        # __init__.py や __main__.py は package 構造の一部なので除外
        # (= server/ も lib/ も flat module 集まりとして使われている前提)
        if entry.startswith("_") and entry.endswith("_.py"):
            continue
        result.add(entry)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="衝突を検知したら exit 1 (= CI gate 用)。default は warn のみ。",
    )
    args = parser.parse_args()

    server_modules = _list_module_files(SERVER_DIR)
    lib_modules = _list_module_files(LIB_DIR)
    collisions = sorted(server_modules & lib_modules)

    if not collisions:
        print("[server-lib-collision] OK: no collisions between server/*.py and lib/*.py")
        return 0

    print(
        "[server-lib-collision] FAIL: server/*.py と lib/*.py で名前が衝突しています。",
        file=sys.stderr,
    )
    print(
        "  app.py 上部の sys.path.insert で lib/ が先に解決されるため、",
        file=sys.stderr,
    )
    print(
        "  server/ 側の同名ファイルは shadow されて壊れます (2026-06-13 本番障害参照)。",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for name in collisions:
        print(f"  - {name}  (server/{name} と lib/{name} が衝突)", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  解決策: server/ 側のファイル名を別の名前に rename してください",
        file=sys.stderr,
    )
    print("    例: server/store.py → server/store_router.py", file=sys.stderr)

    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
