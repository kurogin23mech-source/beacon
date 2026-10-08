"""server/ の入口モジュールが app.py 抜きでも import できることを機械で見る (ms-166 e-6728 の退行止め)。

ms-166 e-6728 で ``import idempotency as _idem`` を ``server/app.py`` の 4 行目 —
``lib/`` を ``sys.path`` に載せる行より **上** — に挿してしまい、次の 2 つが同時に
壊れた:

  1. ``store_router`` → ``firestore_client`` を直接 import するテストが
     ``ModuleNotFoundError: No module named 'idempotency'`` で落ちた (CI で検出)。
  2. ``uvicorn app:app`` を ``PYTHONPATH`` 無しで起動する経路
     (``deploy/systemd/beacon-api.service``) で app.py 自身が import 不能になった。
     Dockerfile は ``PYTHONPATH=/app/lib:/app/server`` を立てるので、container では
     この壊れ方が出ない = 片方の経路だけ見ていると緑に見える。

なので「``lib/`` の module を import している」ことではなく、
**「入口から素の interpreter で import が通るか」** を見る。前者は書き方の検査 (表層) で、
``_libpath`` を import していても順序が逆なら緑になってしまう。後者は実際の壊れ方そのもの。

検査は子プロセスで行う: 親の ``sys.path`` には conftest 等が既に ``lib/`` を載せているので、
同一プロセスで import しても常に通ってしまい、テストが何も捕まえない。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(REPO_ROOT, "server")

# app.py が先に import された前提を持てない = 外から直接掴まれる入口。
#   app            : uvicorn / lambda の入口そのもの
#   lambda_handler : AWS Lambda の入口
#   store_router   : backend router。app.py は `import store_router as db` で使うが、
#                    テストと周辺ツールは app.py を通さず直接 import する
#   firestore_client / dynamodb_client / mysql_client : store_router が選んで re-export する実体
ENTRY_MODULES = [
    "app",
    "lambda_handler",
    "store_router",
    "firestore_client",
    "dynamodb_client",
    "mysql_client",
]


def _import_with_only_server_on_path(module: str) -> subprocess.CompletedProcess:
    """``server/`` だけを sys.path に載せた素の interpreter で module を import する。

    systemd 経路 (WorkingDirectory=/opt/beacon/server, PYTHONPATH 無し) の再現。
    ``-E`` で親の PYTHONPATH を無視し、親環境の寛容さが結果に漏れないようにする。
    """
    return subprocess.run(
        [sys.executable, "-E", "-c", f"import sys; sys.path.insert(0, {SERVER_DIR!r}); import {module}"],
        capture_output=True,
        text=True,
        cwd=SERVER_DIR,
    )


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_entry_module_imports_without_app_py_bootstrap(module: str) -> None:
    result = _import_with_only_server_on_path(module)

    if result.returncode != 0 and "ModuleNotFoundError" not in result.stderr:
        # 依存ライブラリ (fastapi / boto3 / google-cloud-firestore …) 未インストールや
        # 環境要因で落ちた場合は、この検査の対象外なので skip する。対象は
        # 「lib/ が sys.path に無くて落ちた」だけ。
        pytest.skip(f"{module}: 本検査の対象外の失敗 — {result.stderr.strip().splitlines()[-1:]}")

    missing = ""
    for line in result.stderr.splitlines():
        if "ModuleNotFoundError" in line:
            missing = line.strip()

    if missing:
        # 3rd-party が無いだけなら skip、lib/ の module なら本件の退行。
        name = missing.rsplit("'", 2)[-2] if "'" in missing else ""
        if name and not os.path.exists(os.path.join(REPO_ROOT, "lib", f"{name}.py")):
            pytest.skip(f"{module}: 依存ライブラリ {name} が未インストール")

    assert result.returncode == 0, (
        f"server/{module}.py は server/ だけを sys.path に載せた interpreter で import できる必要がある "
        f"(systemd 経路 = PYTHONPATH 無しの uvicorn app:app を再現)。\n"
        f"lib/ の module を import するなら、その行より **上** で `import _libpath` すること。\n"
        f"stderr:\n{result.stderr}"
    )


def test_libpath_is_idempotent_and_points_at_lib() -> None:
    """`_libpath` を二度 import しても sys.path に重複を作らない (= どこから import してもよい)。"""
    result = subprocess.run(
        [
            sys.executable,
            "-E",
            "-c",
            (
                f"import sys; sys.path.insert(0, {SERVER_DIR!r});"
                "import _libpath;"
                "_libpath.ensure(); _libpath.ensure();"
                "print(sys.path.count(_libpath.LIB_DIR), _libpath.LIB_DIR)"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=SERVER_DIR,
    )
    assert result.returncode == 0, result.stderr
    count, lib_dir = result.stdout.split(maxsplit=1)
    assert count == "1", f"sys.path に lib/ が {count} 回入っている (重複挿入)"
    assert os.path.isdir(lib_dir.strip()), f"_libpath.LIB_DIR が実在しない: {lib_dir!r}"
