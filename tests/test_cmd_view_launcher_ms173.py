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
