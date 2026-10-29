"""`beacon skill install` がリポジトリの CLAUDE.md を古い案内に巻き戻さないこと (e-7208)。

CLAUDE.md は AI が毎セッション読む常時参照の案内で、そこから操作の存在を知る。その
beacon 節は ``lib/commands.py`` の雛形 (``CLAUDE_MD_BEACON_SECTION``) から生成され、
``beacon skill install`` / ``beacon init`` が走るたびに丸ごと差し替わる。つまり **同じ表が
雛形とリポジトリの CLAUDE.md に二重にある**。

実際に踏んだ形: メモの片付け (``beacon note reap``) の案内を CLAUDE.md に直接足して
雛形を直し忘れ、``beacon skill install`` を走らせたらその行が消えて古い案内に戻った
(2026-10-28 のセッション中に 2 回)。巻き戻しは静かに起きるので、CLAUDE.md を見て
いなければ気付けない。

**書き込み経路そのものを走らせて判定する。** 初版は純関数 ``render_claude_md_section``
だけを呼んで比べており、「書き込み側がその純関数を通っているか」を一度も見ていなかった:
独立レビューが書き込み側から純関数の呼び出しを外して別の内容を書くように変えたところ、
試験は 5 件すべて緑のままだった (PR#822、high)。守るつもりの症状が守られていない緑は、
ガードが無いより悪い。いまは一時ディレクトリで実際に ``_append_claude_md()`` を走らせ、
書かれた中身で判定する。
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import commands  # noqa: E402
from cmd_note import DEFAULT_LIST_SINCE_DAYS, DEFAULT_LIST_LIMIT  # noqa: E402

CLAUDE_MD = REPO / "CLAUDE.md"
PLACEHOLDER_RE = re.compile(r"@@[A-Z0-9_]+@@")


def _claude_md_after_install(tmp_path, monkeypatch, content):
    """一時 cwd に ``content`` を置き、**実際の書き込み経路** を走らせて結果を返す。

    ``_append_claude_md`` は ``cmd_skill_install`` と ``cmd_common_setup`` (init /
    setup) の双方が呼ぶ、CLAUDE.md を書く唯一の口。ここを通すことで「install が
    巻き戻さない」を実挙動で言える (純関数だけ呼ぶ試験では言えなかった)。
    """
    target = tmp_path / "CLAUDE.md"
    target.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    commands._append_claude_md()
    return target.read_text(encoding="utf-8")


# --- 書き込み経路 (実挙動) -----------------------------------------------------

def test_install_path_does_not_rewrite_this_repos_claude_md(tmp_path, monkeypatch):
    """このリポジトリの CLAUDE.md に install の書き込みを当てても、何も変わらないこと。

    変わるなら `beacon skill install` が案内を巻き戻す状態。直し方は 2 通りで、
    **どちらが正しいかは中身を見て決める**: 雛形が古いなら雛形を直す、CLAUDE.md を
    手で足したのが先なら同じ内容を雛形へ写す。
    """
    content = CLAUDE_MD.read_text(encoding="utf-8")
    written = _claude_md_after_install(tmp_path, monkeypatch, content)
    if written == content:
        return
    cur = content.splitlines()
    new = written.splitlines()
    lost = [ln for ln in cur if ln not in new and ln.strip()]
    gained = [ln for ln in new if ln not in cur and ln.strip()]
    raise AssertionError(
        "beacon skill install が CLAUDE.md を書き換えます (= 案内が巻き戻る)。\n"
        "  CLAUDE.md から消える行:\n    "
        + "\n    ".join(lost[:10] or ["(なし)"])
        + "\n  雛形から入る行:\n    "
        + "\n    ".join(gained[:10] or ["(なし)"])
        + "\n  雛形は lib/commands.py の CLAUDE_MD_BEACON_SECTION。"
        "片方だけ直すとここが赤くなります。")


def test_install_path_goes_through_the_pure_function(tmp_path, monkeypatch):
    """書き込み経路の結果が、純関数の戻り値と byte 一致すること。

    「試験と書き込みが同じ規則を通る」を **主張ではなく構造** にする。書き込み側が
    純関数を迂回したり別の変換を挟んだ瞬間にここが赤くなる (独立レビューが実測した
    穴そのもの)。入力はあえて修正前の形 (reap の行が無い CLAUDE.md) にして、
    「当てたら変わる」場面で一致を見る。
    """
    stale = CLAUDE_MD.read_text(encoding="utf-8").replace(
        "| `beacon note reap --yes` | Clear notes of ENDED sessions only "
        "(live sessions untouched) / 終了したセッションのメモだけ片付ける |\n", "")
    assert stale != CLAUDE_MD.read_text(encoding="utf-8"), (
        "入力を修正前の形にできませんでした (reap の行の文面が変わった?)")
    written = _claude_md_after_install(tmp_path, monkeypatch, stale)
    assert written == commands.render_claude_md_section(stale), (
        "書き込み経路の結果が純関数 render_claude_md_section の戻り値と違います。"
        "書き込み側が純関数を通らなくなっていないか確認してください "
        "(通らなくなると、この試験が守っている症状が守られなくなります)。")
    assert written != stale, "修正前の形に当てたのに何も変わっていません (当て方が死んでいる)"


def test_both_install_entry_points_write_claude_md():
    """CLAUDE.md を書く口が install の両入口から呼ばれていること。

    入口が書き込み関数を呼ばなくなれば、上の 2 件は緑のまま install は何もしなく
    なる (= 案内が更新されない別の穴)。入口側の配線も見る。
    """
    for fn in (commands.cmd_skill_install, commands.cmd_common_setup):
        src = inspect.getsource(fn)
        assert "_append_claude_md()" in src, (
            f"{fn.__name__} が _append_claude_md() を呼んでいません "
            "(install しても CLAUDE.md が更新されない)")


# --- 雛形の中身 ---------------------------------------------------------------

def _quick_reference_rows(section: str) -> dict:
    """雛形の CLI Quick Reference 表を {コマンド: 説明} として構造的に読む。

    表が見つからない / 空のまま「一致」と読まれるのを防ぐため、どちらも例外にする
    (無言の 0 件一致は偽の緑そのもの)。
    """
    head = re.search(r"### CLI Quick Reference\n", section)
    if head is None:
        raise AssertionError(
            "雛形から CLI Quick Reference 表の見出しが見つかりません。改名したなら"
            "この試験も直してください (黙って無効になるのを防ぐため例外にしています)。")
    rows = {}
    for line in section[head.end():].splitlines():
        if not line.startswith("|"):
            if rows:
                break          # 表の終わり
            continue           # 表の手前 (見出し行 / 区切り行まで)
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2 or set(cells[0]) <= set("-: "):
            continue           # 区切り行
        cmd = cells[0].strip("`").strip()
        if cmd.lower() == "command":
            continue           # 見出し行
        rows[cmd] = cells[1]
    if not rows:
        raise AssertionError("CLI Quick Reference 表から 1 行も読めませんでした")
    return rows


def _row(rows: dict, command: str) -> str:
    """表から 1 行を引く。無ければ診断文付きで落とす (素の KeyError を出さない)。"""
    if command not in rows:
        raise AssertionError(
            f"案内表から {command!r} の行が読めません。表が分割された / 列数が変わった"
            f"可能性があります。読めた行: {sorted(rows)[:8]}")
    return rows[command]


def _note_verb(command: str) -> str:
    """``beacon note <x>`` 系のコマンド表記から操作名だけ取り出す。

    表記揺れ (help は ``beacon note <text>``、案内表は ``beacon note "text"``) を
    吸収するため、3 つめの語から括弧 / 引用符を落として比べる。
    """
    parts = command.split()
    if len(parts) < 3 or parts[:2] != ["beacon", "note"]:
        return ""
    return parts[2].strip('<>"\'`[]')


# 常時参照に載せない判断をした操作 (= 載せ忘れではない)。判定の元はコード側の
# help 一覧なので、ここは「あえて外した」ものだけを理由付きで持つ。
NOTE_COMMANDS_INTENTIONALLY_OMITTED = {
    # 1 回限りの掃除用。日常の操作ではないので常時参照には出さない。
    "purge-probes",
}


def _note_verbs_from_help_registry() -> set:
    verbs = {_note_verb(row["command"]) for row in commands._help_registry()}
    verbs.discard("")
    if not verbs:
        raise AssertionError(
            "help 一覧から beacon note 系の操作を 1 つも読めませんでした "
            "(_help_registry の形が変わった?)")
    return verbs


def test_template_lists_every_standing_note_command():
    """help 一覧にあるメモ操作が、あえて外したもの以外すべて案内表に在ること。

    初版はここが手書きの固定リストだった。コードに新しいメモ操作を足しても
    CLAUDE.md は更新を強制されず、試験は緑のままで「案内が古いまま誰も気付かない」
    という e-7208 と同型の穴が別経路 (足し忘れ) で残っていた (独立レビュー、PR#822)。
    判定の元を人の記憶からコード側の help 一覧に移してある。
    """
    rows = _quick_reference_rows(commands.CLAUDE_MD_BEACON_SECTION)
    listed = {_note_verb(cmd) for cmd in rows} - {""}
    expected = _note_verbs_from_help_registry() - NOTE_COMMANDS_INTENTIONALLY_OMITTED
    missing = sorted(expected - listed)
    assert not missing, (
        f"雛形の案内表に載っていないメモ操作があります: {missing}。"
        "CLAUDE.md は AI が毎セッション読む唯一の常時参照なので、載っていない操作は"
        "存在しないのと同じになります。あえて載せない判断なら "
        "NOTE_COMMANDS_INTENTIONALLY_OMITTED に理由付きで足してください。")


def test_omission_list_only_names_commands_that_exist():
    """あえて外した一覧が、消えた操作を指したまま残っていないこと。"""
    stale = NOTE_COMMANDS_INTENTIONALLY_OMITTED - _note_verbs_from_help_registry()
    assert not stale, (
        f"あえて外した一覧に、help 一覧に無い操作が残っています: {sorted(stale)}")


def test_note_list_window_numbers_come_from_the_constants():
    """一覧の窓の数字は手打ちでなく cmd_note の定数由来であること。

    定数を変えても手打ちコピーが古い数字を言い続けた前例があるので (help の同じ行が
    先に同じ理由で interpolate に直されている)、定数を変えたらこの行も追従するはず。
    """
    rows = _quick_reference_rows(commands.CLAUDE_MD_BEACON_SECTION)
    desc = _row(rows, "beacon note list")
    assert f"{DEFAULT_LIST_SINCE_DAYS} 日" in desc, (
        f"一覧の窓の日数が定数 ({DEFAULT_LIST_SINCE_DAYS}) と一致しません: {desc!r}")
    assert f"{DEFAULT_LIST_LIMIT} 件" in desc, (
        f"一覧の窓の件数が定数 ({DEFAULT_LIST_LIMIT}) と一致しません: {desc!r}")


def test_no_unreplaced_placeholder_tokens():
    """埋め込みトークンが本文に残っていないこと (置換の取りこぼし検知)。"""
    left = PLACEHOLDER_RE.findall(commands.CLAUDE_MD_BEACON_SECTION)
    assert not left, (
        f"雛形に埋め残しのトークンがあります: {sorted(set(left))}。"
        "lib/commands.py の置換 (.replace) と綴りが合っているか確認してください。")


# --- 当て方そのもの -----------------------------------------------------------

def test_rendering_is_idempotent():
    """2 回当てても結果が変わらないこと (= 当て方そのものが安定している)。"""
    content = CLAUDE_MD.read_text(encoding="utf-8")
    once = commands.render_claude_md_section(content)
    assert commands.render_claude_md_section(once) == once


def test_section_is_added_when_absent():
    """節が無い本文には足す (他プロジェクトへの初回導入の経路)。"""
    out = commands.render_claude_md_section("# My project\n\nnotes here\n")
    assert out.startswith("# My project")
    assert "## Beacon Project Management" in out
