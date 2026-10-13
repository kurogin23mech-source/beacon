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

# ms-166 e-6890: 対象は「手書きの入口 6 本」から **server 配下の全 *.py** に広げた。
#
# 手書きの一覧は、一覧に載っていないモジュールを黙って検査対象外にする。実際それが
# 起きていた: e-6728 の退行で 4 本を直したあと、それ以前から同じ状態だった 10 本が
# 一覧に無いまま残り、誰も測っていなかった (2026-10-08 に外から数えて初めて分かった)。
# 「app.py が先に import された」という前提はどのモジュールにも書かれていないので、
# 1 本でも別経路から掴まれた瞬間に静かに落ちる。だから対象は列挙せず走査する。
#
# 対象外にするものは、なぜ対象外かを 1 行添えてここに明記する (黙って外さない)。
#
# ただし「1 行添える」は書き手の自己申告で、機械は理由の妥当性を測れない。独立レビューが
# 実測した通り (2026-10-13)、このキーに 1 行足すだけで任意のモジュールが走査から落ち、
# 件数が 28 → 27 に減ったまま全部緑で通る。それは e-6890 が直したはずの失敗の型
# (手書きの一覧が、載っていないものを黙って検査対象外にする) を、新しい場所で
# 作り直したことになる。
#
# なので除外の集合そのものを下の ``EXCLUDED_SNAPSHOT`` で留める。除外を 1 つ増やすと
# 必ず赤くなり、増やす人は「スナップショットも直す」という形で意図を表明させられる。
# 自動では通らない = 人間が一度見る、が狙い (禁止ではなく可視化)。
EXCLUDED_MODULES = {
    # _libpath 自身は lib/ を載せる側で、lib/ の module を import しない。
    # これを対象に含めると「自分が自分を bootstrap できるか」という無意味な検査になる。
    "_libpath": "lib/ を sys.path に載せる当事者。lib/ の module を import しない",
}

# 上の除外が増減したら赤になるための留め。増やすときは一緒に直す (= 意図の表明)。
EXCLUDED_SNAPSHOT = frozenset({"_libpath"})


def _discover_server_modules() -> list:
    """server/ 直下の全 *.py を module 名で返す (除外分を引いたもの)。"""
    names = sorted(
        f[:-3] for f in os.listdir(SERVER_DIR)
        if f.endswith(".py") and not f.startswith("__")
    )
    return [n for n in names if n not in EXCLUDED_MODULES]


ENTRY_MODULES = _discover_server_modules()


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


def test_exclusions_cannot_grow_silently() -> None:
    """走査から外す判断を、黙って通さない。

    ``EXCLUDED_MODULES`` に 1 行足せば任意のモジュールが検査から落ちる。理由を
    1 行添える規約はあるが、理由の妥当性は機械には測れないし、diff 上の 1 行追加は
    レビューでも見落としやすい。独立レビューが実際にそれをやって全部緑のまま
    28 → 27 件に減ることを実測した (2026-10-13)。

    除外の集合を留めて、増減したら必ず赤にする。増やす人はスナップショットも
    直すことになるので、「外した」が意図の表明として残る。
    """
    current = frozenset(EXCLUDED_MODULES)
    added = current - EXCLUDED_SNAPSHOT
    removed = EXCLUDED_SNAPSHOT - current
    assert not added, (
        "走査対象から新たに外されたモジュールがある: {0}。"
        "意図的なら EXCLUDED_SNAPSHOT も更新し、なぜ外すのかを "
        "EXCLUDED_MODULES の値に書くこと".format(sorted(added)))
    assert not removed, (
        "除外が消えている: {0}。EXCLUDED_SNAPSHOT を更新すること".format(sorted(removed)))
    for module, reason in EXCLUDED_MODULES.items():
        assert reason and reason.strip(), (
            "{0} の除外理由が空。なぜ外すのかを 1 行書くこと".format(module))


def test_the_scan_actually_found_modules() -> None:
    """走査が 0 件で「全部通った」ことにならないようにする。

    ``_discover_server_modules`` が空を返すと parametrize が 0 ケースになり、
    テストは緑のまま何も測らない。境界で赤に倒れる側に寄せる。
    """
    assert len(ENTRY_MODULES) >= 20, (
        "server 配下の走査が {0} 件しか返していない。ディレクトリ解決が壊れている疑い"
        .format(len(ENTRY_MODULES)))
    # かつて手書きで挙げられていた入口は、走査でも必ず拾えていること。
    for entry in ("app", "lambda_handler", "store_router", "firestore_client",
                  "dynamodb_client", "mysql_client"):
        assert entry in ENTRY_MODULES, entry


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


def test_lib_path_single_owner_guard_passes() -> None:
    """lib/ への道を知る箇所が server/_libpath.py だけであることを機械で確かめる。

    CI では scripts/ci-strict-drift-guards.sh が同じスクリプトを呼ぶ (lint-docs は
    pytest を持たないので、ガード本体は pytest 非依存の素のスクリプトに置いてある)。
    ここはその結果を手元の pytest からも見えるようにする窓。

    経緯: _libpath.py の docstring が自分を「唯一の持ち主」と名乗った時点では、
    routers_projects.py の検索ハンドラの中に 3 つ目のコピーが残っていた
    (2026-10-08 の独立レビュー 2 体が揃って指摘)。名乗りは守らないので機械で数える。
    """
    script = os.path.join(REPO_ROOT, "scripts", "check-lib-path-single-owner.py")
    result = subprocess.run(
        [sys.executable, script, "--strict"], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, (
        "lib/ を sys.path に載せる箇所が server/_libpath.py の外にあります。\n"
        + result.stdout
        + result.stderr
    )
