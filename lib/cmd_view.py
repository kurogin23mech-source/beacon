"""`beacon view` — 手元で盤を立ち上げて、自分のブラウザで見る (ms-170 e-6344)。

親 SPEC ``KIGmqQTIanqUypbtmrTs`` の設計方針 3 を実装する: **手元起動とサーバ設置は
同じものの起動モードの違い**。既定は手元起動で、127.0.0.1 に小さな受け口を立てて
利用者自身のブラウザに出す。``--host`` を明示するとサーバ設置になり、**表示層も
変換層もそのまま**でデータの取得元だけが設定 (``.beacon/cloud.json``) で決まる。

サーバ設置と安全について
------------------------
盤にはそのプロジェクトの全体像が載るが、この受け口は **認証を持たない**。したがって
ループバック以外に開くことは「その口に届く全員に盤を見せる」ことと同じである。事故を
黙って起こさないため、``--host`` でループバック以外を指定するときは ``--expose`` の
明示を必須にしてある (指定が無ければ起動を断る)。社外に出す場合は、認証を持つ前段
(reverse proxy 等) の後ろに置くこと。

この層が持たない責務
--------------------
盤の形を決めるのは変換層 (``view_model``) であって、ここではない。本モジュールは
``board_view_from_store()`` が返したものを、そのまま JSON として配り、同じものを
描く画面を返すだけである。**ここでデータを整形し直したくなったら、それは表示層の
都合を変換層に持ち込もうとしている合図**なので、スキーマ側を直すか画面側で吸収する
かを先に判断すること (doc ``4aHU7n92YXQFEGuhuEbj`` の規則)。

ローカル / クラウドの分岐もここには無い。``board_view_from_store()`` が取得元を
吸収済みなので、この受け口はどちらで動いているかを知らない。
"""

from __future__ import annotations

import http.server
import json
import os
import platform
import shutil
import socket
import threading
import webbrowser

import store as store_mod
import view_model

# 既定の待ち受け口。使用中なら空きを探す (別プロジェクトの盤と併走できるように)。
DEFAULT_PORT = 7377
# 待ち受けは常に自分の機械の中だけ。手元起動でネットワークに開かない。
LOOPBACK = "127.0.0.1"


def _is_loopback(host: str) -> bool:
    """その待ち受け先が自分の機械の中だけかどうか。"""
    return host in ("127.0.0.1", "::1", "localhost")


def _pick_port(preferred: int, host: str = LOOPBACK) -> int:
    """使える口を返す。希望の口が塞がっていれば、空いているものを借りる。"""
    for candidate in (preferred, 0):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, candidate))
            return probe.getsockname()[1]
        except OSError:
            continue
        finally:
            probe.close()
    raise OSError("待ち受けできる口が見つかりませんでした")


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


def _handler_class(store, sessions=None):
    """受け口 1 つ分の応答規則。``store`` を掴んだまま毎回読み直す。"""

    class _BoardHandler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — http.server の決まり
            if self.path.rstrip("/") in ("", "/index.html"):
                self._send(200, PAGE.encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            if self.path.rstrip("/") == "/api/board":
                try:
                    view = build_view(store, sessions=sessions)
                except Exception as e:  # 盤が取れない理由を画面に出す
                    body = json.dumps(
                        {"error": str(e)}, ensure_ascii=False).encode("utf-8")
                    self._send(500, body, "application/json; charset=utf-8")
                    return
                body = json.dumps(view, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):  # noqa: N802 — http.server の決まり
            pass  # 手元起動なので通信記録は出さない

    return _BoardHandler


def serve(port: int = DEFAULT_PORT, *, open_browser: bool = True,
          store=None, forever: bool = True, sessions=None,
          host: str = LOOPBACK, expose: bool = False):
    """盤を立ち上げる。``forever=False`` なら立てるだけで返る (試験用)。

    ``host`` の既定は自分の機械の中だけ。それ以外に開くとき (= サーバ設置) は
    ``expose=True`` の明示が要る。認証が無いので、黙って外に開かせない。
    """
    if not _is_loopback(host) and not expose:
        raise ValueError(
            f"{host} に開くと、その口に届く全員が盤を読めます "
            "(このビューワーは認証を持ちません)。"
            "意図した設置なら --expose を付けてください。"
            "社外に出す場合は認証を持つ前段の後ろに置いてください。")
    # 逆向きの誤り (ms-170 #742 AX review): --expose は「外に開く」明示なのに
    # loopback で単独指定すると何も起きず黙って受理され、「公開したつもりで非公開」
    # の逆誤りになる。--host が loopback 以外を要求するのと対称に、こちらも拒否する。
    if _is_loopback(host) and expose:
        raise ValueError(
            "--expose は自分の機械の外に開くときの明示です。"
            f"--host に loopback 以外の宛先 (例 0.0.0.0) を指定してください "
            f"(いまの --host は {host})。ローカルだけで見るなら --expose は不要です。")
    store = store or store_mod.get_store()
    chosen = _pick_port(port, host)
    server = http.server.HTTPServer(
        (host, chosen), _handler_class(store, sessions))
    # 表示用の住所。外に開いた場合は実際の到達先が違いうるので、その旨は下で伝える。
    url = f"http://{host}:{chosen}/"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"盤を開きました: {url}", flush=True)
    print(f"  取得元: {'クラウド' if store.is_cloud() else 'ローカル'}", flush=True)
    if not _is_loopback(host):
        print("  [警告] この盤は自分の機械の外にも開いています。認証は無いので、"
              "この口に届く全員が読めます。", flush=True)
    print("  終了: Ctrl+C", flush=True)

    # 外に開いた設置ではブラウザを立ち上げても意味がない (画面のある機械ではない)。
    if open_browser and _is_loopback(host):
        try:
            webbrowser.open(url)
        except Exception:
            # ブラウザが開けなくても、上に出した住所を手で開けば見られる。
            pass

    if not forever:
        return server, url

    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n盤を閉じました。")
    finally:
        server.shutdown()
        server.server_close()
    return server, url


# --- 運用室への委譲 (ms-173 e-6430) -----------------------------------------
# 並列セッションの運用室 (セッション一覧 / scope フィルタ / activity / 端末ジャンプ)
# は Go 版ビューワー ``beacon-view`` (viewer/*.go) にしかない。ここ (ユーザーの入口
# ``beacon view``) はそれを **探して、在れば委譲** する。無ければ従来の素朴な盤に
# フォールバックする。
#
# **これは暫定の縮退経路である。** ms-170 の終着は Go 一本化で、この Python 盤は
# 最終的に撤去される (恒久保持ではない)。ゼロ依存の下限は「Go バイナリを beacon
# 配布 (pipx wheel / brew) に per-platform で同梱する」ことで担保する予定で、その
# 同梱作業は別タスク e-6476 (ms-170) が持つ。同梱が入るまでの間、beacon-view が手元
# に無い環境では運用室に到達できないため、簡易盤を出しつつ入手方法を 1 行案内する。

# フォールバック時に出す 1 行案内。Go 同梱 (e-6476) が入るまでの暫定である旨を明記し、
# 「素朴盤が最終形」と読めないようにする。Windows 既定文字コード (cp932) で出せるよう
# 記号は使わない (serve の起動出力と同じ制約)。
FALLBACK_NOTICE = (
    "運用室 (セッション一覧・状態・端末ジャンプ) は Go 版ビューワー beacon-view で"
    "見られます。\n"
    "  この端末には beacon-view が見つからないので、簡易版の盤を表示します。\n"
    "  (これは Go 版ビューワーが beacon 配布に同梱される [e-6476] までの暫定です。)\n"
    "  入手: viewer/build.sh でビルドするか、配布物の beacon-view を PATH に置いて"
    "ください。"
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
    """同梱 / 手元ビルドの beacon-view を探す候補パス (探索順)。

    ここは best-effort の推測であって正典ではない。**同梱の正式な置き場所は e-6476
    (ms-170) が決める**。それが入るまでは、build.sh の出力先 (dist/) と、素朴に
    置かれうる場所を当たる。
    """
    goos, goarch = _go_os_arch(system, machine)
    ext = ".exe" if goos == "windows" else ""
    name = "beacon-view" + ext
    dist_name = f"beacon-view-{goos}-{goarch}{ext}"
    return [
        # build.sh の配布物 (per-platform 名)
        os.path.join(install_root, "viewer", "dist", dist_name),
        # beacon 同梱を想定した素直な置き場所 (e-6476 が確定させる)
        os.path.join(install_root, "bin", name),
        # 手元ビルド (viewer/ 直下、.gitignore 済)
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
    # 運用室 (ms-173 e-6430): Go 版 beacon-view が在れば画面をそちらに委譲する。
    # 委譲できれば os.execv で Go 版が丸ごと引き継ぐ (戻らない)。見つからない、または
    # 実行できなかったときだけ、下の素朴盤にフォールバックする。
    binary = resolve_viewer_binary()
    if binary:
        argv = viewer_argv(binary, project_root=_project_root(), port=port,
                           host=host, expose=expose, no_open=no_open)
        try:
            os.execv(binary, argv)  # 成功すれば戻らない (Go 版が住所も自分で出す)
        except OSError:
            # アーキ違い / 壊れたファイル等で起動できなかった。素朴盤に落ちる。
            pass
    # ここに到達 = 運用室へ委譲できなかった。暫定の素朴盤を出しつつ入手方法を案内する
    # (Go 同梱 e-6476 が入れば、この経路は最終的に不要になる)。
    print(FALLBACK_NOTICE, flush=True)
    try:
        serve(port, open_browser=not no_open, host=host, expose=expose)
    except ValueError as e:
        raise SystemExit(f"Error: {e}")


# --- 画面 -------------------------------------------------------------------
# 取得元による分岐をここに書かないこと。この画面は /api/board が返す形だけを知る。
PAGE = """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<title>Beacon 盤</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 24px;
         line-height: 1.6; }
  header { border-bottom: 1px solid #8884; padding-bottom: 12px;
           margin-bottom: 20px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .objective { opacity: .75; font-size: 14px; }
  .meta { font-size: 12px; opacity: .6; margin-top: 6px; }
  .counts { display: flex; gap: 20px; margin: 16px 0 24px; }
  .counts div { font-size: 13px; }
  .counts b { font-size: 22px; display: block; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px;
           border-bottom: 1px solid #8882; vertical-align: top; }
  th { font-weight: 600; opacity: .7; font-size: 12px; }
  .bar { background: #8883; height: 6px; border-radius: 3px; width: 100px; }
  .bar span { display: block; height: 100%; border-radius: 3px;
              background: currentColor; opacity: .6; }
  .done { opacity: .45; }
  h2 { font-size: 14px; margin: 32px 0 8px; opacity: .7; }
  .empty { opacity: .5; font-size: 13px; }
  .err { color: #c33; }
</style>
<div id="app">読み込み中…</div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function targetRow(t) {
  const w = t.work_items || {};
  const pct = w.total ? Math.round(w.done / w.total * 100) : 0;
  return `<tr class="${t.is_done ? "done" : ""}">
    <td>${esc(t.id)}</td>
    <td>${esc(t.label)}</td>
    <td>${esc(t.status)}</td>
    <td>${w.done}/${w.total}
      <div class="bar"><span style="width:${pct}%"></span></div></td>
  </tr>`;
}

function sessionRow(s) {
  return `<tr>
    <td>${esc(s.who)}</td><td>${esc(s.agent)}</td>
    <td>${esc(s.target || "—")}</td>
    <td>${s.live ? "稼働中" : "—"}</td>
    <td>${esc(s.cwd)}</td>
  </tr>`;
}

fetch("/api/board").then(r => r.json()).then(b => {
  if (b.error) {
    document.getElementById("app").innerHTML =
      `<p class="err">盤を読めませんでした: ${esc(b.error)}</p>`;
    return;
  }
  const p = b.project || {}, g = b.progress || {};
  const src = b.source || {};
  document.getElementById("app").innerHTML = `
    <header>
      <h1>${esc(p.name)}</h1>
      <div class="objective">${esc(p.objective)}</div>
      <div class="meta">取得元: ${esc(src.kind)}${
        src.project_id ? " / " + esc(src.project_id) : ""} ・ 職種: ${
        esc(p.profession)} ・ 形式 v${esc(b.schema_version)}</div>
    </header>
    <div class="counts">
      <div><b>${g.total}</b>対象</div>
      <div><b>${g.done}</b>完了</div>
      <div><b>${g.open}</b>進行中</div>
    </div>
    <table>
      <tr><th>ID</th><th>内容</th><th>状態</th><th>消化</th></tr>
      ${(b.targets || []).map(targetRow).join("")}
    </table>
    <h2>作業セッション</h2>
    ${(b.sessions || []).length ? `<table>
      <tr><th>誰</th><th>種別</th><th>対象</th><th>状態</th><th>作業場所</th></tr>
      ${b.sessions.map(sessionRow).join("")}</table>`
      : `<p class="empty">見えているセッションはありません。</p>`}
    <h2>ドキュメント (${(b.documents || []).length})</h2>
    ${(b.documents || []).length ? `<table>
      <tr><th>種別</th><th>題</th><th>対象</th><th>更新</th></tr>
      ${b.documents.map(d => `<tr><td>${esc(d.scope)}</td>
        <td>${esc(d.title)}</td><td>${esc(d.target || "—")}</td>
        <td>${esc((d.updated_at || "").slice(0, 10))}</td></tr>`).join("")}
      </table>` : `<p class="empty">ドキュメントはありません。</p>`}
  `;
}).catch(e => {
  document.getElementById("app").innerHTML =
    `<p class="err">盤を読めませんでした: ${esc(e)}</p>`;
});
</script>
</html>
"""
