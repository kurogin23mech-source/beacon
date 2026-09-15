"""`beacon view` の運用室委譲ランチャーの単体テスト (ms-173 e-6430)。

守っているもの:
- ユーザーの入口 ``beacon view`` が、運用室を持つ Go 版 ``beacon-view`` を **探して**、
  在れば委譲用の argv を組み立てられること
- 探索順が PATH 優先で、無ければ同梱 / 手元ビルドの候補を当たり、どれも無ければ
  None を返すこと (= 素朴盤へフォールバックする合図)
- フォールバック時の案内が「Go 同梱 (e-6476) までの暫定」と読めること
  (= 恒久保持と誤読させない、親方針)
- 案内文が Windows 既定文字コード (cp932) で出せること

探索は差し替え可能な純関数なので、実ファイル / PATH / 実行 OS に依存しない。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_view  # noqa: E402


# --- 探索: PATH 優先 --------------------------------------------------------

def test_path_binary_wins_over_bundled():
    """PATH に在れば、同梱 / 手元ビルドの詮索より優先する。

    配布 (brew / pipx) は PATH に置けるので、そこに在るものを正典として扱う。
    """
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: "/usr/local/bin/beacon-view",
        install_root="/opt/beacon", system="Darwin", machine="arm64",
        is_exec=lambda p: True)  # bundled も在ることにしても PATH が勝つ
    assert found == "/usr/local/bin/beacon-view"


def test_which_is_queried_for_beacon_view():
    """探すのは ``beacon-view`` (ハイフン付き、Go 版) であって自分自身ではない。"""
    asked = []

    def _which(name):
        asked.append(name)
        return None

    cmd_view.resolve_viewer_binary(
        which=_which, install_root="/opt/beacon", system="Linux",
        machine="x86_64", is_exec=lambda p: False)
    assert asked == ["beacon-view"]


# --- 探索: 同梱 / 手元ビルドのフォールバック --------------------------------

def test_bundled_dist_binary_is_found_when_not_on_path():
    """PATH に無ければ build.sh の配布物 (per-platform 名) を当たる。"""
    dist = os.path.join(
        "/opt/beacon", "viewer", "dist", "beacon-view-darwin-arm64")
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: None, install_root="/opt/beacon",
        system="Darwin", machine="arm64",
        is_exec=lambda p: p == dist)
    assert found == dist


def test_wheel_bundled_viewer_is_the_canonical_location():
    """per-platform wheel の同梱先 _bundled_viewer/beacon-view が見つかる (e-6476)。

    wheel は platform 固有なので suffix 無しの固定名 beacon-view[.exe] を
    install_root/_bundled_viewer/ に入れる。install 後は install_root が
    site-packages/beacon_cli/ を指す。
    """
    bundled = os.path.join("/sp/beacon_cli", "_bundled_viewer", "beacon-view")
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: None, install_root="/sp/beacon_cli",
        system="Darwin", machine="arm64",
        is_exec=lambda p: p == bundled)
    assert found == bundled


def test_wheel_bundled_viewer_preferred_over_dev_candidates():
    """_bundled_viewer (正式同梱) が dev の viewer/dist より先に当たる。"""
    cand = cmd_view._bundled_viewer_candidates("/root", "Darwin", "arm64")
    bundled = os.path.join("/root", "_bundled_viewer", "beacon-view")
    dist = os.path.join("/root", "viewer", "dist", "beacon-view-darwin-arm64")
    assert cand.index(bundled) < cand.index(dist)


def test_wheel_bundled_viewer_windows_has_exe():
    """Windows の同梱先も .exe 固定名。"""
    cand = cmd_view._bundled_viewer_candidates("C:\\pkg", "Windows", "AMD64")
    assert os.path.join("C:\\pkg", "_bundled_viewer", "beacon-view.exe") in cand


def test_windows_bundled_name_has_exe_suffix():
    """Windows では配布物名に .exe が付く (GOOS/GOARCH 写像)。"""
    exe = os.path.join(
        "C:\\beacon", "viewer", "dist", "beacon-view-windows-amd64.exe")
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: None, install_root="C:\\beacon",
        system="Windows", machine="AMD64",
        is_exec=lambda p: p == exe)
    assert found == exe


def test_returns_none_when_nothing_found():
    """PATH にも候補にも無ければ None (= 素朴盤へフォールバックする合図)。"""
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: None, install_root="/opt/beacon",
        system="Linux", machine="aarch64", is_exec=lambda p: False)
    assert found is None


def test_non_executable_candidate_is_skipped():
    """在るだけでは足りない。実行できないファイルは候補にしない。"""
    found = cmd_view.resolve_viewer_binary(
        which=lambda name: None, install_root="/opt/beacon",
        system="Darwin", machine="arm64",
        is_exec=lambda p: False)  # ファイルはあるが実行不可、の想定
    assert found is None


# --- GOOS / GOARCH 写像 -----------------------------------------------------

def test_os_arch_mapping_covers_common_hosts():
    assert cmd_view._go_os_arch("Darwin", "arm64") == ("darwin", "arm64")
    assert cmd_view._go_os_arch("Darwin", "x86_64") == ("darwin", "amd64")
    assert cmd_view._go_os_arch("Linux", "aarch64") == ("linux", "arm64")
    assert cmd_view._go_os_arch("Windows", "AMD64") == ("windows", "amd64")


def test_unknown_os_arch_passes_through_lowercased():
    """知らない値でも落ちない (探索が空振りするだけ)。"""
    assert cmd_view._go_os_arch("Plan9", "sparc") == ("plan9", "sparc")


# --- 委譲 argv の組み立て ---------------------------------------------------

def test_viewer_argv_maps_flags():
    argv = cmd_view.viewer_argv(
        "/usr/local/bin/beacon-view", project_root=".", port=7377,
        host="127.0.0.1", expose=False, no_open=False)
    assert argv == ["/usr/local/bin/beacon-view", "--path", ".",
                    "--port", "7377", "--host", "127.0.0.1"]


def test_viewer_argv_carries_expose_and_no_open():
    argv = cmd_view.viewer_argv(
        "/bin/beacon-view", project_root="/proj", port=8000,
        host="0.0.0.0", expose=True, no_open=True)
    assert "--expose" in argv
    assert "--no-open" in argv
    # 値付きフラグの写像が崩れていないこと
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8000"


# --- project_root の解決 ----------------------------------------------------

def test_project_root_defaults_to_cwd(monkeypatch):
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    assert cmd_view._project_root() == "."


def test_project_root_follows_beacon_project_file(monkeypatch):
    monkeypatch.setenv(
        "BEACON_PROJECT_FILE", "/home/me/proj/.beacon/project.json")
    assert cmd_view._project_root() == "/home/me/proj"


# --- フォールバック案内 -----------------------------------------------------

def test_fallback_notice_marks_itself_as_interim():
    """案内は『暫定』であることと入手方法を含む (恒久保持と誤読させない)。"""
    notice = cmd_view.FALLBACK_NOTICE
    assert "暫定" in notice
    assert "e-6476" in notice          # Go 同梱タスクへの導線
    assert "beacon-view" in notice     # 何を入手すればよいか


def test_fallback_notice_is_encodable_on_windows_legacy_codepage():
    """案内は cp932 (Windows 日本語環境の既定) で出せること。

    記号を混ぜると日本語 Windows で出力時に例外が出て、案内そのものが消える。
    """
    try:
        cmd_view.FALLBACK_NOTICE.encode("cp932")
    except UnicodeEncodeError as e:
        raise AssertionError(f"cp932 で出せない文字が案内にある: {e}")


# --- exec 失敗案内 (not-found と区別する / 独立レビュー consensus) ------------

def test_exec_failed_notice_distinguishes_from_not_found():
    """『在るが起動できなかった』は『無い』と別文言で伝える。

    同じ「見つからない」を両方に出すと、壊れたバイナリが居座ったまま『入手せよ』の
    案内に従っても状況が変わらない誤診ループに入る (AX misleading + 保守性 §5 consensus)。
    """
    notice = cmd_view.exec_failed_notice("/usr/local/bin/beacon-view",
                                         OSError("Exec format error"))
    # 起動できなかった対象 (path) と理由 (error) が surface に出ている
    assert "/usr/local/bin/beacon-view" in notice
    assert "Exec format error" in notice
    # not-found 案内の「見つからない」とは異なる主張であること
    assert "見つからない" not in notice
    assert "起動できませんでした" in notice


def test_exec_failed_notice_is_encodable_on_windows_legacy_codepage():
    try:
        cmd_view.exec_failed_notice("C:\\beacon-view.exe",
                                    OSError("boom")).encode("cp932")
    except UnicodeEncodeError as e:
        raise AssertionError(f"cp932 で出せない文字が案内にある: {e}")


# --- cmd_view() の配線 (純関数の seam を繋ぐ経路 / 保守性 finding #5) --------
#
# 純関数群は上でテスト済み。ここは「その seam を繋ぐ配線」= 分岐順序と
# フォールバック連鎖を押さえる。os.execv / resolve_viewer_binary / serve /
# build_view を差し替えて、cmd_view() を実際に 1 度通す。


class _ExecCalled(Exception):
    """os.execv が『戻らない』のを試験内で模す (実 execv はプロセスを置換する)。"""

    def __init__(self, argv):
        self.argv = argv


def _wire(monkeypatch, *, binary, execv_error=None):
    """cmd_view() の外部依存を差し替え、呼び出し記録を返す。"""
    calls = {"execv": None, "serve": False, "printed": []}

    monkeypatch.setattr(cmd_view, "resolve_viewer_binary",
                        lambda **kw: binary)

    def _fake_execv(path, argv):
        calls["execv"] = (path, argv)
        if execv_error is not None:
            raise execv_error
        raise _ExecCalled(argv)  # 成功時は戻らない、を模す

    monkeypatch.setattr(cmd_view.os, "execv", _fake_execv)
    monkeypatch.setattr(cmd_view, "serve",
                        lambda *a, **k: calls.__setitem__("serve", True))
    monkeypatch.setattr(cmd_view, "build_view", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cmd_view, "_project_root", lambda: ".")

    import builtins
    real_print = builtins.print
    monkeypatch.setattr(
        builtins, "print",
        lambda *a, **k: calls["printed"].append(" ".join(str(x) for x in a))
        if a else real_print(*a, **k))
    return calls


def _clear_view_env(monkeypatch):
    for k in ("BEACON_JSON", "BEACON_VIEW_PORT", "BEACON_VIEW_NO_OPEN",
              "BEACON_VIEW_HOST", "BEACON_VIEW_EXPOSE"):
        monkeypatch.delenv(k, raising=False)


def test_json_mode_does_not_delegate(monkeypatch):
    """--json は盤のみで運用室を含まないため Go 版に委譲しない (順序不変条件)。"""
    _clear_view_env(monkeypatch)
    monkeypatch.setenv("BEACON_JSON", "1")
    calls = _wire(monkeypatch, binary="/x/beacon-view")
    cmd_view.cmd_view()
    assert calls["execv"] is None       # 委譲していない
    assert calls["serve"] is False      # サーバも立てていない (JSON 出して return)


def test_binary_found_delegates_via_execv(monkeypatch):
    """beacon-view が在れば viewer_argv の結果で execv に委譲する。"""
    _clear_view_env(monkeypatch)
    calls = _wire(monkeypatch, binary="/x/beacon-view")
    try:
        cmd_view.cmd_view()
    except _ExecCalled as e:
        assert e.argv[0] == "/x/beacon-view"
        assert "--path" in e.argv
    else:
        raise AssertionError("execv に委譲していない")
    assert calls["serve"] is False      # 委譲成功時は素朴盤を立てない
    # 委譲を名乗る 1 行が出ている (silent narrowing 防止)
    assert any("委譲します" in line for line in calls["printed"])


def test_exec_failure_falls_back_to_simple_board(monkeypatch):
    """execv が OSError なら exec 失敗案内を出して素朴盤に落ちる。"""
    _clear_view_env(monkeypatch)
    calls = _wire(monkeypatch, binary="/x/beacon-view",
                  execv_error=OSError("Exec format error"))
    cmd_view.cmd_view()
    assert calls["execv"] is not None   # 委譲は試みた
    assert calls["serve"] is True       # 素朴盤にフォールバックした
    assert any("起動できませんでした" in line for line in calls["printed"])


def test_binary_absent_falls_back_with_notice(monkeypatch):
    """beacon-view が無ければ not-found 案内を出して素朴盤を立てる。"""
    _clear_view_env(monkeypatch)
    calls = _wire(monkeypatch, binary=None)
    cmd_view.cmd_view()
    assert calls["execv"] is None
    assert calls["serve"] is True
    assert any("見つからない" in line for line in calls["printed"])


# --- プロセス境界の写しの drift ガード (保守性 finding #4 / #6) --------------
#
# 配布バイナリの命名と Go 版フラグ名は Python 側に写しとして存在する。写しは
# 1 箇所に固めてあるが、真実源 (build.sh / main.go) が変わったとき Python 側の
# 写しが silent に取り残されると委譲が黙って効かなくなる。repo 内で閉じる軽量な
# 突き合わせで drift を検出可能にする (Go 一本化 = ms-170 終着でこの写し自体が消える)。

_VIEWER_DIR = os.path.join(os.path.dirname(__file__), "..", "viewer")


def test_bundled_dist_name_matches_build_sh():
    """resolver が探す dist 名が build.sh の出力規約と一致していること。"""
    with open(os.path.join(_VIEWER_DIR, "build.sh"), encoding="utf-8") as f:
        build = f.read()
    # build.sh は NAME="beacon-view" を "$NAME-$os-$arch$ext" で dist に吐く。
    assert 'NAME="beacon-view"' in build, \
        "build.sh の出力名が変わった。_bundled_viewer_candidates の dist 名も直すこと"
    assert "$NAME-$os-$arch$ext" in build, \
        "build.sh の dist 命名規約が変わった。resolver 側の写しを同期すること"
    # resolver 側の写しがその規約通りに組まれていること
    cand = cmd_view._bundled_viewer_candidates("/root", "Darwin", "arm64")
    assert any(c.endswith(os.path.join(
        "viewer", "dist", "beacon-view-darwin-arm64")) for c in cand)


def test_viewer_argv_flags_exist_in_go_main():
    """viewer_argv が組む全フラグが Go 版 main.go に定義されていること。

    Go 側でフラグを改名すると exec は成功して Go 側のフラグエラーが直接出る
    (Python fallback は発動しない)。repo 内でフラグ集合を突き合わせて改名を捕まえる。
    """
    with open(os.path.join(_VIEWER_DIR, "main.go"), encoding="utf-8") as f:
        main_go = f.read()
    # main.go の「実際のフラグ定義」だけを集合として抽出する。
    # 旧実装は `'flag.' in main_go and '"<name>"' in main_go` という緩い包含判定で、
    # (1) 'flag.' はファイルに 1 つでもあれば全フラグで真、(2) '"<name>"' は help 文・
    # JSON タグ・URL パス等どこかにあれば真、となり「Go 側でフラグを改名しても旧名の
    # 文字列がどこかに残っていれば素通り」する false-pass だった (親レビュー #747 保守性
    # medium)。flag.String/Int/Bool(...) の第1引数だけを拾い、定義集合との包含で突き合わせる。
    defined = set(re.findall(r'flag\.\w+\(\s*"([\w-]+)"', main_go))
    assert defined, "main.go から flag 定義を 1 つも抽出できていない (抽出正規表現が古い?)"
    argv = cmd_view.viewer_argv("beacon-view", project_root=".", port=7377,
                                host="127.0.0.1", expose=True, no_open=True)
    flags = {a[2:] for a in argv if a.startswith("--")}
    assert flags, "viewer_argv がフラグを 1 つも組んでいない"
    missing = flags - defined
    assert not missing, (
        f"viewer_argv が渡す {sorted('--' + f for f in missing)} が Go 版 main.go の "
        f"flag 定義に無い (改名 drift)。main.go 定義済み: {sorted(defined)}")
