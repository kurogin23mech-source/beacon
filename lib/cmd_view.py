"""`beacon view` — 運用室 (Go 版 beacon-view) に委譲する入口 (ms-170)。

環境変数 (dispatch 層が渡す BEACON_VIEW_PORT / HOST / EXPOSE / NO_OPEN のほか):
  BEACON_VIEW_SKIP_HANDSHAKE=1 … 委譲前の版握手 (e-6482) を意図して飛ばす。
    通常は不要 — 版が食い違う beacon-view へ黙って委譲しないための握手なので、
    飛ばすのは「食い違いを承知で古い盤を見たい」ときだけ。


**Go 一本化 (e-6518)**: 盤 / 運用室 (セッション一覧・状態・端末ジャンプ) の表示は
Go 版ビューワー ``beacon-view`` (viewer/*.go) が唯一の実装。この Python モジュールは
ユーザーの入口 ``beacon view`` として beacon-view を **探して委譲する** だけで、盤の
HTML を自前で描いたり HTTP で配ったりはしない (かつての Python 素朴盤 serve/PAGE は
e-6518 で撤去)。beacon-view は e-6476 で配布 (pipx wheel / brew) に per-platform 同梱
されるので、正規経路の利用者は常に持つ。見つからない / 起動できないときは、黙って別物
を出さず入手・更新手順を案内して終了する。

この層に残る責務は 2 つだけ:
  * **委譲**: beacon-view を PATH / dev ビルド候補から解決し (resolve_viewer_binary)、
    フラグを Go の argv に写して (viewer_argv) ``os.execv`` で引き継ぐ。
  * **--json (headless)**: 画面を立てずに盤の中身だけを出す経路。変換層
    (``view_model.board_view_from_store``) が返す形をそのまま JSON で出す
    (``build_view``)。beacon-view 無しでも盤データは取れるようにここだけ Python に残す。

盤の形を決めるのは変換層 (``view_model``) であって、ここではない。ローカル / クラウド
の分岐もここには無い (``board_view_from_store()`` が取得元を吸収済み、doc
``4aHU7n92YXQFEGuhuEbj`` の規則)。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys

import store as store_mod
import view_model

# 既定の待ち受け口。使用中なら空きを探す (別プロジェクトの盤と併走できるように)。
DEFAULT_PORT = 7377
# 待ち受けは常に自分の機械の中だけ。手元起動でネットワークに開かない。
LOOPBACK = "127.0.0.1"


def _collect_sessions(store) -> list:
    """作業セッション一覧を best-effort で集める。

    変換層は読み書きをしない約束なので、外から集めるのはこちら側の仕事。
    取れなくても盤は成立するため、失敗は握りつぶして空一覧を返す (セッションが
    見えないことより、盤が出ないことのほうが困る)。
    """
    if not store.is_cloud():
        # ローカルには他セッションの名簿が無い。ms-171 (= Beacon 外のセッションも
        # 同じ面に載せる) で扱う領域なので、ここでは空のままにする。
        return []
    try:
        # `beacon bus directory` と同じ経路。名簿はサーバが持つので、cloud の
        # ときだけ引ける。取れなくても盤は出す (握りつぶしは意図的)。
        from commands import _get_api_client  # noqa: PLC0415
        client, config = _get_api_client()
        return client.list_sessions(
            config.get("project_id", ""), live_only=True) or []
    except Exception:
        return []


def build_view(store=None, *, sessions=None) -> dict:
    """いまの盤を 1 面組み立てて返す (画面と JSON の共通入口)。

    ``sessions`` を渡すとそれを使い、省略すると名簿を取りに行く。差し替えられる
    ようにしてあるのは、名簿の取得だけが唯一の外向き通信であり、ここを固定できないと
    「盤を組み立てる」ことの試験がネットワークに依存してしまうため。
    """
    store = store or store_mod.get_store()
    documents = []
    try:
        documents = store.list_documents() or []
    except Exception:
        # ドキュメントが取れなくても盤は成立する。空一覧で通す。
        documents = []
    if sessions is None:
        sessions = _collect_sessions(store)
    return view_model.board_view_from_store(
        store, documents=documents, sessions=sessions)


# --- 運用室への委譲 (ms-173 e-6430、Go 一本化 e-6518) -------------------------
# 並列セッションの運用室 (セッション一覧 / scope フィルタ / activity / 端末ジャンプ)
# は Go 版ビューワー ``beacon-view`` (viewer/*.go) が担う。ユーザーの入口
# ``beacon view`` はそれを **探して委譲** するだけ。
#
# **Python 素朴盤フォールバックは撤去した (e-6518 = Go 一本化の締め)。** e-6476 で
# beacon-view は配布 (pipx wheel / brew) に per-platform 同梱されたので、正規経路の
# 利用者は常に beacon-view を持つ。見つからないのは「配布版が古い」か「dev clone で
# 未ビルド」で、回復はアップグレード or build.sh。見つからないときは **黙って別物を
# 出さず** (親 design 指示)、下の案内を出して終了する。盤の中身だけなら
# ``beacon view --json`` が引き続き取れる (画面を立てない headless 経路)。

# beacon-view が見つからないときの終端案内 (e-6518 で素朴盤フォールバックを撤去)。
# 回復は配布版のアップグレードが第一 (e-6476 で同梱済み)、dev は build.sh。
# Windows 既定文字コード (cp932) で出せるよう記号は使わない。
#
# **not-found と exec-failed を surface で区別する** (AX + 保守性 独立レビュー consensus):
# 「beacon-view が無い」と「在るが起動できなかった (アーキ違い/壊れ)」は回復手段が
# 違う。同じ文言を両方に出すと、壊れたバイナリが PATH に居座ったまま「入手せよ」の
# 案内に従っても状況が変わらない誤診ループに入る。理由ごとに別文言を出す。
FALLBACK_NOTICE = (
    # 冒頭を Error: に統一 (SystemExit 全経路で揃える、PR #752 AX2)。
    "Error: 運用室 (セッション一覧・状態・端末ジャンプ) は Go 版ビューワー beacon-view で"
    "見られますが、この端末には beacon-view が見つかりません。\n"
    "  beacon-view は beacon の配布 (pipx / brew) に同梱されています。最新版に更新すると"
    "入ります:\n"
    "    pipx upgrade beacon-ai   (pip なら pip install --upgrade beacon-ai)\n"
    "    brew upgrade beacon\n"
    "  dev clone では viewer/build.sh でビルドして beacon-view を PATH に置いてください。\n"
    "  盤の中身だけなら beacon view --json で取得できます (画面は出ません)。"
)


def exec_failed_notice(binary: str, error: object) -> str:
    """beacon-view は見つかったが起動できなかったときの案内 (not-found とは別文言)。

    「無い」ではなく「在るが動かない」を正しく伝え、回復行動を正しい対象 (壊れた
    バイナリの削除 / 差し替え) に向ける。cp932 で出せるよう記号は使わない。
    """
    return (
        # 冒頭を Error: に統一 (SystemExit 全経路で揃える、PR #752 AX2)。
        f"Error: beacon-view ({binary}) を起動できませんでした: {error}\n"
        "  このファイルが壊れているか、この端末とは別のプラットフォーム向けの"
        "可能性があります。\n"
        "  削除するか、この端末に合う beacon-view に差し替えてください "
        "(配布版を入れ直すなら pipx upgrade beacon-ai / brew upgrade beacon)。\n"
        "  盤の中身だけなら beacon view --json で取得できます (画面は出ません)。"
    )


def _go_os_arch(system: str, machine: str) -> tuple[str, str]:
    """Python の platform 表記を Go の GOOS / GOARCH に写す。

    配布バイナリ (build.sh が ``beacon-view-<goos>-<goarch>[.exe]`` で吐く) を手元で
    探すための対応表。知らない値は素通しする (探索が空振りするだけで害はない)。
    """
    goos = {"darwin": "darwin", "windows": "windows", "linux": "linux"}.get(
        system.lower(), system.lower())
    goarch = {
        "x86_64": "amd64", "amd64": "amd64",
        "arm64": "arm64", "aarch64": "arm64",
    }.get(machine.lower(), machine.lower())
    return goos, goarch


def _bundled_viewer_candidates(install_root: str, system: str,
                               machine: str) -> list:
    """PATH に無いときに当たる、dev / 手元ビルドの beacon-view 候補 (探索順)。

    **配布物での正式な届け方は PATH** (e-6476 / ms-170、2026-09-15 に PATH 方式で確定):
    pip の per-platform wheel は beacon-view を wheel の script として ``bin`` /
    ``Scripts`` に +x 付きで置き、brew は ``bin/beacon-view`` を置く。どちらも PATH に
    載るので、``resolve_viewer_binary`` は ``shutil.which("beacon-view")`` で先に拾う
    (= 正式配布はこの関数の候補には来ない)。

    したがってここは **PATH に無い環境 = dev clone / 手元ビルド 専用** の候補だけを
    返す: build.sh の出力先 (viewer/dist/、per-platform 名) と、素直に手で置かれうる
    場所。install 済み wheel / brew を「正式同梱先」として当てる候補は持たない
    (それは PATH 経由で解決されるため、ここに重複させると偽の canonical になる)。
    """
    goos, goarch = _go_os_arch(system, machine)
    ext = ".exe" if goos == "windows" else ""
    name = "beacon-view" + ext
    dist_name = f"beacon-view-{goos}-{goarch}{ext}"
    return [
        # dev clone の手元ビルド (build.sh の per-platform 名)
        os.path.join(install_root, "viewer", "dist", dist_name),
        # 素直な置き場所 (手動配置 / 後方互換)
        os.path.join(install_root, "bin", name),
        os.path.join(install_root, "viewer", name),
    ]


def resolve_viewer_binary(*, which=None, install_root=None, system=None,
                          machine=None, is_exec=None):
    """委譲できる beacon-view の実行ファイルを 1 つ返す (無ければ None)。

    探索順は PATH が先。配布 (brew / pipx) は PATH に置けるので、そこに在れば
    手元ビルドの詮索より優先する。副作用ゼロの純関数にしてあるのは、探索規則そのものを
    ネットワーク / 実ファイルに依存せず試験できるようにするため (依存は全て差し替え可能)。
    """
    which = which or shutil.which
    install_root = install_root if install_root is not None else _install_root()
    system = system if system is not None else platform.system()
    machine = machine if machine is not None else platform.machine()
    is_exec = is_exec or (
        lambda p: os.path.isfile(p) and os.access(p, os.X_OK))

    on_path = which("beacon-view")
    if on_path:
        return on_path
    for cand in _bundled_viewer_candidates(install_root, system, machine):
        if is_exec(cand):
            return cand
    return None


def _install_root() -> str:
    """この beacon 一式が置かれた根 (lib/ の 1 つ上)。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def handshake_failed_notice(binary: str, detail: str) -> str:
    """版握手 (e-6482) に失敗したときの案内。silent に古い盤を出す代わりに、
    何が合っていないかと回復手順を明示する。cp932 で出せるよう記号は使わない。"""
    return (
        f"Error: beacon-view ({binary}) との版握手に失敗しました: {detail}\n"
        "  古い beacon-view に委譲すると、新しい欄 (状態・担当・context% 等) が"
        "黙って欠けた運用室が出ます。\n"
        "  配布版を更新してください: pipx upgrade beacon-ai / brew upgrade beacon\n"
        "  dev clone では viewer/build.sh で作り直して PATH の beacon-view を"
        "差し替えてください。\n"
        "  盤の中身だけなら beacon view --json で取得できます (画面は出ません)。\n"
        "  この握手を意図して飛ばすには BEACON_VIEW_SKIP_HANDSHAKE=1 を指定します。"
    )


def viewer_handshake(binary: str, expected_version: str, *, run=None):
    """委譲前の版握手 (ms-173 e-6482)。``(ok, detail)`` を返す。

    ``beacon-view --version`` を叩き、beacon 本体の版と照合する:

      * ``beacon-view <expected>`` → ok (配布同梱の正常ペア)
      * ``beacon-view dev``        → ok (手元ビルド。版を刻まない dev を塞ぐと
        開発が回らないので通す — 握手の狙いは「気づかず古い」の排除であり、
        意図して作った手元ビルドは利用者が自分で把握している)
      * 版が食い違う                → NG (古い / 別系列の beacon-view)
      * --version 非対応 (非 0 終了) → NG (e-6482 以前の古い beacon-view)
      * 起動できない (OSError 等)   → NG (壊れ / アーキ違い — exec 前に検出)

    ``run`` は試験用の差し替え口 (subprocess.run 互換)。純粋な判定部を試験できる
    ようにするための注入で、既定は実 subprocess。
    """
    import subprocess
    runner = run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=10))
    try:
        proc = runner([binary, "--version"])
    except Exception as e:  # OSError / timeout — 起動そのものができない
        return False, f"--version を実行できませんでした ({e})"
    if proc.returncode != 0:
        return False, ("--version に応答しません (e-6482 の版握手より古い "
                       "beacon-view です)")
    out = (proc.stdout or "").strip()
    parts = out.split()
    got = parts[1] if len(parts) == 2 and parts[0] == "beacon-view" else ""
    if not got:
        return False, f"--version の応答を解釈できません: '{out}'"
    if got == "dev":
        return True, "dev"
    if got != expected_version:
        return False, (f"版が食い違っています (beacon-view {got} / "
                       f"beacon {expected_version})")
    return True, got


def nonstandard_project_file_notice():
    """非標準 ``BEACON_PROJECT_FILE`` での Go 委譲を止める案内 (無害なら None)。

    Go 版 beacon-view は ``--path <root>`` から ``<root>/.beacon/project.json``
    (と project.db) を自分で読む — ``BEACON_PROJECT_FILE`` は解釈しない。標準
    配置 (``.../.beacon/project.json``) 以外を指している状態で委譲すると、Go 版は
    別の (あるいは無い) プロジェクトを黙って開き、利用者は気づけない (e-6482)。
    その場合は委譲せず、この案内で headless 経路 (--json は
    ``BEACON_PROJECT_FILE`` を尊重する Python 側で返す) を示す。
    """
    pf = os.environ.get("BEACON_PROJECT_FILE", "")
    if not pf:
        return None
    if (os.path.basename(pf) == "project.json"
            and os.path.basename(os.path.dirname(pf)) == ".beacon"):
        return None
    return (
        f"Error: BEACON_PROJECT_FILE ({pf}) が標準配置"
        " (…/.beacon/project.json) ではありません。\n"
        "  運用室 (Go 版 beacon-view) は場所 (--path) から .beacon/ を自分で読む"
        "ため、この指定は届かず、別のプロジェクトを黙って開く恐れがあります。\n"
        "  盤の中身だけなら beacon view --json が BEACON_PROJECT_FILE を尊重して"
        "返します (画面は出ません)。\n"
        "  画面で見るには、対象プロジェクトのフォルダに cd して beacon view を"
        "実行してください。"
    )


def _project_root() -> str:
    """--path で Go 版に渡す、.beacon を含むフォルダ。

    取得元の分岐はしない (Go 版が .beacon/cloud.json を見てローカル / クラウドを
    自分で判定する)。cloud プロジェクトでも、Go 版はローカル起動のまま名簿だけ
    クラウドから取る設計なので、運用室は機能する。
    """
    project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    return os.path.dirname(beacon_dir) or "."


def viewer_argv(binary: str, *, project_root: str, port: int, host: str,
                expose: bool, no_open: bool) -> list:
    """beacon-view を起動する argv を組み立てる (フラグ写像を 1 箇所に固める)。"""
    argv = [binary, "--path", project_root, "--port", str(port),
            "--host", host]
    if expose:
        argv.append("--expose")
    if no_open:
        argv.append("--no-open")
    return argv


def cmd_view() -> None:
    """CLI 入口。環境変数は他の commands.py の verb と同じ渡し方に揃える。"""
    # --port の入口検証 (ms-170 #742 AX review): 非数値だと素の traceback で
    # 落ちて何を直せばよいか分からない。数値と範囲を verb 入口で確かめ、回復の
    # 手掛かりを文言に埋める (bash / dispatch のどちらの frontend も env 経由で
    # ここを通るので、ここ 1 箇所で両 frontend を閉じる)。
    raw_port = os.environ.get("BEACON_VIEW_PORT") or ""
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        raise SystemExit(
            f"Error: --port は数値で指定してください (1-65535)。受け取った値: '{raw_port}'")
    if not (1 <= port <= 65535):
        raise SystemExit(
            f"Error: --port は 1-65535 の範囲で指定してください。受け取った値: {port}")
    no_open = os.environ.get("BEACON_VIEW_NO_OPEN") == "1"
    host = os.environ.get("BEACON_VIEW_HOST") or LOOPBACK
    expose = os.environ.get("BEACON_VIEW_EXPOSE") == "1"
    if os.environ.get("BEACON_JSON") == "1":
        # 画面を立てずに、いまの盤をそのまま出す (別の道具に渡したいとき用)。
        # --json は盤だけで運用室 (セッション一覧) を含まないため、Go 版に委譲せず
        # ここで返す (Python 版と Go 版の盤は parity テストで一致を担保済み)。
        print(json.dumps(build_view(), ensure_ascii=False))
        return
    # 運用室 (ms-173 e-6430 / Go 一本化 e-6518): Go 版 beacon-view に委譲する。
    # Python 素朴盤フォールバックは撤去済。見つからない / 起動できないときは、黙って
    # 別物を出さず案内を出して終了する (親 design 指示)。
    #
    # 委譲前に 2 つの握手 (ms-173 e-6482 — silent に壊れない):
    #  1. 非標準 BEACON_PROJECT_FILE — Go 版はこの env を解釈しないため、標準配置
    #     以外を指したまま委譲すると別プロジェクトを黙って開く。委譲せず案内。
    #  2. 版握手 — 古い beacon-view は新しい欄が黙って欠けた運用室を出す。
    #     --version で照合し、合わなければ委譲せず更新案内。
    notice = nonstandard_project_file_notice()
    if notice:
        raise SystemExit(notice)
    binary = resolve_viewer_binary()
    if not binary:
        # beacon-view が無い。素朴盤に落とさず、入手 / 更新手順を出して終了する。
        raise SystemExit(FALLBACK_NOTICE)
    if os.environ.get("BEACON_VIEW_SKIP_HANDSHAKE") != "1":
        # __version__ の真実源は commands (lib/cmd_project._beacon_version と同じ
        # 出所)。遅延 import なのは commands が重く、--json 経路では不要なため。
        from commands import __version__ as _beacon_version
        ok, detail = viewer_handshake(binary, _beacon_version)
        if not ok:
            raise SystemExit(handshake_failed_notice(binary, detail))
        if detail == "dev":
            # dev ビルドは通すが、その事実は名乗る (silent narrowing を防ぐ)。
            print("beacon-view は dev ビルドです (版握手は素通し)",
                  file=sys.stderr, flush=True)
    argv = viewer_argv(binary, project_root=_project_root(), port=port,
                       host=host, expose=expose, no_open=no_open)
    # どの実体に委譲したかを名乗る (silent narrowing を防ぐ)。execv は成功すれば
    # 戻らないので、事前に 1 行出しておく。
    # 診断ログは stderr へ (PR #752 AX1)。stdout は Go 盤の出力用に空けておく —
    # execv 失敗時 (下の SystemExit は stderr) に stdout だけ読む側が「委譲成功」と
    # 誤認しないため。委譲が成功すれば execv でこのプロセスは Go 版に置き換わる。
    print(f"運用室 beacon-view に委譲します: {binary}", file=sys.stderr, flush=True)
    try:
        os.execv(binary, argv)  # 成功すれば戻らない (Go 版が住所も自分で出す)
    except OSError as e:
        # 見つかったが起動できなかった (アーキ違い / 壊れ)。not-found とは別文言を
        # 出して終了する (回復行動を壊れたバイナリ側へ向ける)。
        raise SystemExit(exec_failed_notice(binary, e))
