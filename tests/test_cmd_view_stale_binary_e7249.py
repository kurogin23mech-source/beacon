"""建て直し忘れの検出の単体テスト (ms-170 e-7249)。

**なぜこの試験が要るのか**: 2026-10-30 に運用室から端末へ飛ぶ不具合を直して取り込んだ
のに、11-01 に同じ症状が観測された。原因はコードではなく、動いていた実行ファイルが
10/28 に建てたもので修正が入っていなかったこと。版はどちらも 0.65.0 だったので版握手
(e-6482) は通り、しかも実行ファイルは git で追跡されないローカル成果物なので取り込んでも
更新されない。直したはずの不具合が直らないまま報告され、受入条件を満たしたと誤って
記録された。

この試験が守るのは次の 4 つ:

1. 時刻で見る判定が 3 値 (stale / fresh / unknown) で、**比べられなかったことを
   「古くない」に畳まない**こと
2. **版握手だけでは同版の古さを捕まえられない**こと (= この検査が必要な理由そのもの。
   ここが緑のまま検査を外すと、同じ事故に戻れてしまう)
3. 古ければ委譲を止め、案内が **建て直しとプロセスの入れ替えの両方** を示すこと
   (建て直しだけ案内すると「直したのに直らない」をもう一周する)
4. 意図的な素通し口を使っても、**古いという事実は stderr に残る**こと

判定は差し替え可能なので、実ファイル / 実行 OS / 実プロセスに依存しない。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_view  # noqa: E402


# --- 対象ファイルの選び方 ---------------------------------------------------

def test_go_sources_and_build_inputs_are_watched():
    """実行ファイルの中身を決めるものは見る (Go ソース / 画面 / 依存の固定)。"""
    for name in ("main.go", "jump.go", "page.html", "go.mod", "go.sum"):
        assert cmd_view._is_viewer_source(name), name


def test_test_sources_are_not_watched():
    """``*_test.go`` は見ない。

    試験ファイルは実行ファイルに入らない。入れると試験を直すたびに建て直しを
    強いることになり、それは嘘の古さになる。緩すぎるガードと同じく、厳しすぎる
    ガードも「どうせ誤報だ」と読まれて効かなくなる。
    """
    for name in ("jump_test.go", "send_test.go", "cloud_test.go"):
        assert not cmd_view._is_viewer_source(name), name


def test_unrelated_files_are_not_watched():
    """ソースでないものは見ない (建て直しても変わらないため)。"""
    for name in ("README.md", "build.sh", "dist", "beacon-view"):
        assert not cmd_view._is_viewer_source(name), name


# --- 最も新しいソースの取り出し ---------------------------------------------

def _dir_with(tmp_path, files):
    """``{名前: mtime}`` で viewer ディレクトリを組む。"""
    d = tmp_path / "viewer"
    d.mkdir(exist_ok=True)
    for name, mtime in files.items():
        f = d / name
        f.write_text("x", encoding="utf-8")
        os.utime(f, (mtime, mtime))
    return str(d)


def test_newest_source_picks_the_latest(tmp_path):
    """最も新しいソースの時刻と名前を返す。"""
    d = _dir_with(tmp_path, {"main.go": 1000, "jump.go": 3000,
                             "page.html": 2000})
    assert cmd_view.newest_viewer_source(d) == (3000, "jump.go")


def test_newest_source_ignores_test_files(tmp_path):
    """試験ファイルが一番新しくても選ばない (建て直しは不要なため)。"""
    d = _dir_with(tmp_path, {"main.go": 1000, "jump_test.go": 9999})
    assert cmd_view.newest_viewer_source(d) == (1000, "main.go")


def test_newest_source_is_none_without_sources(tmp_path):
    """ソースが無ければ None (= 判定できない)。配布版がこの状態になる。"""
    d = _dir_with(tmp_path, {"README.md": 1000})
    assert cmd_view.newest_viewer_source(d) is None


def test_newest_source_is_none_without_directory(tmp_path):
    """ディレクトリ自体が無くても落ちない。"""
    assert cmd_view.newest_viewer_source(str(tmp_path / "nope")) is None


def test_newest_source_reaches_into_subdirectories(tmp_path):
    """**入れ子のソースも見る** (e-7249 保守性 review medium)。

    build.sh は viewer/ で ``go build ./...`` する。``./...`` は再帰的なので、
    サブディレクトリのソースも実行ファイルの中身を決める。浅く見るだけだと、
    そこだけ新しくても「新しくない」と答えてしまう —— 黙って古いまま動くのを
    止める検査が、自分で黙って古さを見落とす形になる。
    """
    d = _dir_with(tmp_path, {"main.go": 1000})
    sub = tmp_path / "viewer" / "internal"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "extra.go"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (5000, 5000))
    newest = cmd_view.newest_viewer_source(d)
    assert newest is not None
    assert newest[0] == 5000
    assert "extra.go" in newest[1]          # どこのファイルか辿れる
    assert "internal" in newest[1]          # 相対パスで返る


def test_nested_source_newer_than_binary_is_stale(tmp_path):
    """入れ子のソースが新しければ stale と答える (浅い走査だと fresh に化ける)。"""
    d = _dir_with(tmp_path, {"main.go": 1000})
    sub = tmp_path / "viewer" / "internal"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "extra.go"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (5000, 5000))
    verdict, detail = cmd_view.binary_freshness(_binary(tmp_path, 2000),
                                                viewer_dir=d)
    assert verdict == "stale"
    assert "extra.go" in detail


def test_testdata_go_files_are_not_sources(tmp_path):
    """``testdata/`` の ``.go`` は古さの根拠にしない (**除外が効く唯一の場所**)。

    Go の道具は ``testdata/`` を無視するので、そこの ``.go`` は実行ファイルに
    入らない。ファイル名の絞り込みは拡張子しか見ないため、ここで降りないと
    「ビルドに入らないのに新しい」を建て直しの理由にしてしまう (= 嘘の古さ)。

    最初はこの主張を ``dist/`` で書いていたが、反転ハーネスが偽の緑として
    捕まえた —— ``dist/`` に並ぶのは実行ファイルで、それはファイル名の絞り込みが
    既に弾くので、除外を外してもテストが緑のままだった。守っている機構と、
    テストが名指しする機構がずれていた (e-7249、自作の反転ハーネスが検出)。
    """
    d = _dir_with(tmp_path, {"main.go": 1000})
    td = tmp_path / "viewer" / "testdata"
    td.mkdir(parents=True, exist_ok=True)
    f = td / "fixture.go"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (9999, 9999))
    assert cmd_view.newest_viewer_source(d) == (1000, "main.go")
    assert cmd_view.binary_freshness(_binary(tmp_path, 2000),
                                     viewer_dir=d)[0] == "fresh"


def test_built_binary_is_not_a_source_by_name(tmp_path):
    """``dist/`` の実行ファイルは **ファイル名の絞り込み** が弾く。

    走査から ``dist`` を外しているのは「歩かないため」(11MB の実行ファイルを
    毎回 stat しない) で、古さの判定には影響しない。ここをディレクトリ除外の
    功績として書くと、効いていない機構を守っているつもりになる。
    """
    d = _dir_with(tmp_path, {"main.go": 1000})
    stray = tmp_path / "viewer" / "beacon-view-darwin-arm64"
    stray.write_text("x", encoding="utf-8")
    os.utime(stray, (9999, 9999))
    # 除外ディレクトリの外 (viewer/ 直下) に置いても拾わない = 名前で弾いている
    assert cmd_view.newest_viewer_source(d) == (1000, "main.go")


# --- 3 値の判定 --------------------------------------------------------------

def _binary(tmp_path, mtime):
    b = tmp_path / "beacon-view"
    b.write_text("x", encoding="utf-8")
    os.utime(b, (mtime, mtime))
    return str(b)


def test_source_newer_than_binary_is_stale(tmp_path):
    """ソースのほうが新しければ stale。これが 10/28 の実行ファイルが居た状態。"""
    d = _dir_with(tmp_path, {"jump.go": 5000})
    verdict, detail = cmd_view.binary_freshness(_binary(tmp_path, 1000),
                                                viewer_dir=d)
    assert verdict == "stale"
    assert "jump.go" in detail          # どのファイルが新しいかを名指しする


def test_binary_newer_than_source_is_fresh(tmp_path):
    """建て直した直後は fresh。"""
    d = _dir_with(tmp_path, {"jump.go": 1000})
    assert cmd_view.binary_freshness(_binary(tmp_path, 5000),
                                     viewer_dir=d)[0] == "fresh"


def test_equal_mtime_is_fresh(tmp_path):
    """同時刻は fresh。ソースより **新しい** ときだけ古くないとは言わない代わりに、
    同時刻を stale にすると建て直し直後に誤報しうるため境界はこちらに寄せる。"""
    d = _dir_with(tmp_path, {"jump.go": 1000})
    assert cmd_view.binary_freshness(_binary(tmp_path, 1000),
                                     viewer_dir=d)[0] == "fresh"


def test_missing_sources_is_unknown_not_fresh(tmp_path):
    """ソースが無い環境 (配布版) は unknown。**fresh に畳まない**。

    比べられなかったことを「古くない」と同じ値にすると、判定できない環境が黙って
    緑になる。3 値に分けてあるのはそのため。
    """
    d = _dir_with(tmp_path, {"README.md": 1000})
    verdict, detail = cmd_view.binary_freshness(_binary(tmp_path, 1000),
                                                viewer_dir=d)
    assert verdict == "unknown"
    assert "配布版" in detail


def test_unreadable_binary_is_unknown(tmp_path):
    """実行ファイルの時刻が読めなければ unknown (stale と断定しない)。"""
    d = _dir_with(tmp_path, {"jump.go": 5000})
    verdict, _ = cmd_view.binary_freshness(str(tmp_path / "nope"),
                                           viewer_dir=d)
    assert verdict == "unknown"


# --- 版握手では捕まえられない、という事実の固定 -----------------------------

def test_version_handshake_passes_on_same_version_staleness():
    """**版握手だけでは今回の事故を捕まえられない**ことを固定する。

    これがこの検査の存在理由そのもの。版の札はリリースでしか動かないので、同じ版の
    中で入った修正 (= 日々の開発のほとんど) では握手が通る。ここが緑のまま時刻の
    検査を外すと、同じ事故に戻れてしまう。

    再現しているのは実際の値: 10/28 に建てた実行ファイルも、10/30 に修正が入った
    beacon 本体も、どちらも 0.65.0 だった。
    """
    ok, detail = cmd_view.viewer_handshake(
        "/x/beacon-view", "0.65.0",
        run=lambda argv: type("P", (), {
            "returncode": 0, "stdout": "beacon-view 0.65.0", "stderr": ""})())
    assert ok is True                   # 握手は通る
    assert detail == "0.65.0"
    # 同じ状況を時刻で見ると古いと分かる (= 捕まえる層が違う)
    verdict, _ = cmd_view.binary_freshness(
        "/x/beacon-view",
        viewer_dir="/x/viewer",
        newest_source=lambda d: (5000.0, "jump.go"),
        getmtime=lambda p: 1000.0)
    assert verdict == "stale"


# --- 案内文 ------------------------------------------------------------------

def test_notice_names_both_rebuild_and_process_replacement():
    """案内は建て直しと **動いている盤の終了** の両方を示す。

    建て直しだけを案内すると、すでに動いている盤がそのままなので「直したのに
    直らない」をもう一周する。実際にそれが起きた (古いプロセスが 16 本生きていた)。
    """
    text = cmd_view.stale_binary_notice("/x/beacon-view", "detail")
    assert "build.sh" in text                       # 建て直す手段
    assert "終了" in text                            # 動いている盤を止めること
    # 素通し口は **この検査だけの口** を指す (版握手の口を指してはいけない)。
    assert "BEACON_VIEW_SKIP_STALENESS_CHECK" in text
    assert "BEACON_VIEW_SKIP_HANDSHAKE=1" not in text
    assert "e-7249" in text                         # 経緯への辿り口


def test_notice_puts_stopping_before_rebuilding():
    """**止める手順が建て直しより前に来る** (2026-11-01 実測)。

    動いている盤を止めずに建て直すと、``go build`` が実行中のプロセスが掴んでいる
    ファイルへ上書きするため、出来た実行ファイルが起動できなくなる (macOS が
    SIGKILL する)。そのとき表に出る症状は ``--version`` の無応答 = 版握手の失敗
    なので、案内は「配布版を更新せよ」と言い出し、dev clone の利用者を誤診の輪に
    入れる。順序が逆の案内は、従うと壊れる案内になる。
    """
    text = cmd_view.stale_binary_notice("/x/beacon-view", "detail")
    assert text.index("終了") < text.index("build.sh"), (
        "止める手順が建て直しより後にある (従うと実行ファイルが壊れる)")


def test_notice_is_encodable_on_windows_legacy_codepage():
    """Windows 既定文字コード (cp932) で出せる (記号で落ちない)。"""
    try:
        cmd_view.stale_binary_notice("C:/x/beacon-view.exe",
                                     "detail").encode("cp932")
    except UnicodeEncodeError as e:
        raise AssertionError(f"cp932 で出せない文字が案内にある: {e}")


# --- cmd_view() の配線 ------------------------------------------------------

class _ExecCalled(Exception):
    def __init__(self, argv):
        self.argv = argv


def _wire(monkeypatch, *, freshness):
    """cmd_view() の外部依存を差し替え、呼び出し記録を返す。"""
    calls = {"execv": None, "printed": [], "streams": []}
    for k in ("BEACON_JSON", "BEACON_VIEW_PORT", "BEACON_VIEW_NO_OPEN",
              "BEACON_VIEW_HOST", "BEACON_VIEW_EXPOSE",
              "BEACON_VIEW_SKIP_HANDSHAKE", "BEACON_PROJECT_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cmd_view, "resolve_viewer_binary",
                        lambda **kw: "/x/beacon-view")
    monkeypatch.setattr(cmd_view, "_project_root", lambda: ".")
    monkeypatch.setattr(cmd_view, "viewer_handshake",
                        lambda binary, expected, **kw: (True, expected))
    monkeypatch.setattr(cmd_view, "binary_freshness",
                        lambda binary, **kw: freshness)

    def _fake_execv(path, argv):
        calls["execv"] = (path, argv)
        raise _ExecCalled(argv)

    monkeypatch.setattr(cmd_view.os, "execv", _fake_execv)
    import builtins
    real_print = builtins.print

    def _record_print(*a, **k):
        """print を記録する。**宛先 (file kwarg) ごと残す** (e-7249 AX review high)。

        以前は ``*a`` だけを記録して ``**k`` を捨てていた。そのため
        ``file=sys.stderr`` を ``file=sys.stdout`` に変えても全テストが緑のまま
        通り、「診断は stderr に出る」という主張を 1 件も守れていなかった
        (独立レビューが変異と実 CLI 実行の両方で実証)。stdout は委譲先 Go 盤の
        出力用に空けておく約束なので、混ざると stdout を機械で読む側が誤動作する。
        """
        if not a:
            return real_print(*a, **k)
        text = " ".join(str(x) for x in a)
        target = k.get("file")
        if target is sys.stderr:
            stream = "stderr"
        elif target is None or target is sys.stdout:
            stream = "stdout"
        else:
            stream = getattr(target, "name", repr(target))
        calls["printed"].append(text)
        calls["streams"].append((stream, text))

    monkeypatch.setattr(builtins, "print", _record_print)
    return calls


def test_stale_binary_refuses_delegation(monkeypatch):
    """古ければ委譲を止める (黙って古いまま起動しない — 受入条件 1)。"""
    calls = _wire(monkeypatch, freshness=("stale", "jump.go が新しい"))
    with pytest.raises(SystemExit) as ei:
        cmd_view.cmd_view()
    assert calls["execv"] is None                   # 委譲していない
    assert "手元のソースより古い" in str(ei.value)
    assert "jump.go が新しい" in str(ei.value)      # 根拠を文言に運ぶ


def test_fresh_binary_delegates(monkeypatch):
    """新しければ従来どおり委譲する (誤って止めない)。"""
    calls = _wire(monkeypatch, freshness=("fresh", ""))
    with pytest.raises(_ExecCalled):
        cmd_view.cmd_view()
    assert calls["execv"] is not None


def test_unknown_freshness_delegates(monkeypatch):
    """判定できない (配布版) なら止めない。

    比べる相手が無いことを理由に配布版の利用者を止めてはいけない。unknown を
    stale 扱いにすると、正規経路の全員が起動できなくなる。
    """
    calls = _wire(monkeypatch, freshness=("unknown", "配布版"))
    with pytest.raises(_ExecCalled):
        cmd_view.cmd_view()
    assert calls["execv"] is not None


def test_skip_env_still_reports_staleness(monkeypatch):
    """素通し口を使っても古い事実は stderr に残る (受入条件 2)。

    飛ばしたいのは「止めること」であって「知ること」ではない。起動した事実が
    どこにも残らないと、受入条件を誤って満たしたと記録する穴がまた開く。
    """
    calls = _wire(monkeypatch, freshness=("stale", "jump.go が新しい"))
    monkeypatch.setenv("BEACON_VIEW_SKIP_STALENESS_CHECK", "1")
    with pytest.raises(_ExecCalled):
        cmd_view.cmd_view()
    assert calls["execv"] is not None                        # 起動はする
    assert any("古いです" in line for line in calls["printed"])  # 名乗る


def test_staleness_warning_goes_to_stderr_not_stdout(monkeypatch):
    """古い事実は **stderr** に出る。stdout には出さない。

    stdout は委譲先 (Go 盤) の出力用に空けてある約束なので、診断が混ざると
    stdout を機械で読む側が「委譲に成功した」と誤認する。宛先を見ないまま
    「stderr に残る」と言う試験は、宛先が入れ替わっても緑のままで、守れていない
    (独立レビューが変異と実 CLI 実行の両方で実証、e-7249 AX review high)。
    """
    calls = _wire(monkeypatch, freshness=("stale", "jump.go が新しい"))
    monkeypatch.setenv("BEACON_VIEW_SKIP_STALENESS_CHECK", "1")
    with pytest.raises(_ExecCalled):
        cmd_view.cmd_view()
    on_stderr = [t for s, t in calls["streams"] if s == "stderr"]
    on_stdout = [t for s, t in calls["streams"] if s == "stdout"]
    assert any("古いです" in t for t in on_stderr), (
        f"古い事実が stderr に出ていない: {calls['streams']}")
    assert not any("古いです" in t for t in on_stdout), (
        f"診断が stdout に漏れている: {on_stdout}")


def test_version_handshake_skip_does_not_downgrade_staleness(monkeypatch):
    """**版握手の素通し口は、建て直し忘れの検出には効かない** (e-7249 AX review)。

    2 つは別の問いなので別の口にしてある。1 本に束ねると、版違いを承知で
    `BEACON_VIEW_SKIP_HANDSHAKE` を立てた人が、古い実行ファイルで起動する
    保護まで無自覚に外すことになる (名前は「握手」単数なので予告もできない)。
    """
    calls = _wire(monkeypatch, freshness=("stale", "jump.go が新しい"))
    monkeypatch.setenv("BEACON_VIEW_SKIP_HANDSHAKE", "1")
    with pytest.raises(SystemExit) as ei:
        cmd_view.cmd_view()
    assert calls["execv"] is None                      # 止まる
    assert "手元のソースより古い" in str(ei.value)


def test_skip_env_does_not_silence_freshness_check(monkeypatch):
    """素通し口は判定そのものを飛ばさない (呼ばれることを固定する)。

    「skip なら判定も呼ばない」に実装が寄ると、上の試験は警告が出ないことに
    気づけるが、判定を呼ばない実装はそれ以前に静かになる。呼び出し自体を押さえる。
    """
    seen = []
    calls = _wire(monkeypatch, freshness=("fresh", ""))
    monkeypatch.setattr(cmd_view, "binary_freshness",
                        lambda binary, **kw: (seen.append(binary),
                                              ("fresh", ""))[1])
    monkeypatch.setenv("BEACON_VIEW_SKIP_STALENESS_CHECK", "1")
    with pytest.raises(_ExecCalled):
        cmd_view.cmd_view()
    assert seen == ["/x/beacon-view"]
    assert calls["execv"] is not None


# --- ガードが落ちるところまで見る (偽の緑を自分で作らない) -------------------

def test_inversion_harness_is_wired():
    """機構を 1 つずつ外して、名指しのテストが赤くなることを確かめる。

    このガードは「置いたのに守られていなかった」事故 (= 版握手は緑なのに古い盤が
    動き続けた) への対処なので、ガード自身も「壊したら赤くなる」まで測る。

    **SKIP にしない**。ここで skip すると緑に見え、ハーネスが証明するはずのもの
    そのものを隠す。ハーネスは作業コピーに変異を当てるので、リポジトリのファイルを
    書き換えず、隣で走る pytest とも干渉しない。
    """
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "scripts",
                          "invert-stale-viewer-guard-e7249.py")
    assert os.path.isfile(script), f"反転ハーネスがありません: {script}"
    proc = subprocess.run([sys.executable, script], cwd=root,
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (
        "機構を外してもどのテストも赤くならない箇所があります:\n"
        f"{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}")
