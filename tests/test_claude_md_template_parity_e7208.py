"""`beacon skill install` がリポジトリの CLAUDE.md を古い案内に巻き戻さないこと (e-7208)。

CLAUDE.md は AI が毎セッション読む常時参照の案内で、そこから操作の存在を知る。その
beacon 節は ``lib/commands.py`` の雛形 (``CLAUDE_MD_BEACON_SECTION``) から生成され、
``beacon skill install`` / ``beacon init`` が走るたびに丸ごと差し替わる。つまり **同じ表が
雛形とリポジトリの CLAUDE.md に二重にある**。

実際に踏んだ形: メモの片付け (``beacon note reap``) の案内を CLAUDE.md に直接足して
雛形を直し忘れ、``beacon skill install`` を走らせたらその行が消えて古い案内に戻った
(2026-10-28 のセッション中に 2 回)。巻き戻しは静かに起きるので、CLAUDE.md を見て
いなければ気付けない。

ここがその食い違いを捕まえる歯止め。判定は書き込み関数と **同じ純関数**
(``render_claude_md_section``) を通すので、確かめた規則と実際に走る規則がずれない。
"""
from __future__ import annotations

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


def test_install_does_not_rewrite_this_repos_claude_md():
    """このリポジトリの CLAUDE.md に雛形を当てても、何も変わらないこと。

    変わるなら `beacon skill install` が案内を巻き戻す状態。直し方は 2 通りで、
    **どちらが正しいかは中身を見て決める**: 雛形が古いなら雛形を直す、CLAUDE.md を
    手で足したのが先なら同じ内容を雛形へ写す。
    """
    content = CLAUDE_MD.read_text(encoding="utf-8")
    rendered = commands.render_claude_md_section(content)
    if rendered == content:
        return
    # 差分の在処を行単位で示す (全文を貼ると読めないため)。
    cur = content.splitlines()
    new = rendered.splitlines()
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


# 表に必ず載っていてほしいメモ操作 (= ms-178 で揃えた 1 組)。1 つでも欠けると、その
# 操作は AI から存在しないのと同じになる。`note purge-probes` は 1 回限りの掃除用で
# 常時参照には載せない、という判断であえて除いてある。
REQUIRED_NOTE_COMMANDS = [
    'beacon note "text"',
    "beacon note list",
    "beacon note clear --yes [--include-other-sessions]",
    "beacon note reap --yes",
    "beacon note restore",
]


def test_template_lists_every_note_command_we_promise():
    rows = _quick_reference_rows(commands.CLAUDE_MD_BEACON_SECTION)
    missing = [c for c in REQUIRED_NOTE_COMMANDS if c not in rows]
    assert not missing, (
        f"雛形の案内表からメモ操作が欠けています: {missing}。"
        "CLAUDE.md は AI が毎セッション読む唯一の常時参照なので、載っていない操作は"
        "存在しないのと同じになります。")


def test_note_list_window_numbers_come_from_the_constants():
    """一覧の窓の数字は手打ちでなく cmd_note の定数由来であること。

    定数を変えても手打ちコピーが古い数字を言い続けた前例があるので (help の同じ行が
    先に同じ理由で interpolate に直されている)、定数を変えたらこの行も追従するはず。
    """
    rows = _quick_reference_rows(commands.CLAUDE_MD_BEACON_SECTION)
    desc = rows["beacon note list"]
    assert f"{DEFAULT_LIST_SINCE_DAYS} 日" in desc, (
        f"一覧の窓の日数が定数 ({DEFAULT_LIST_SINCE_DAYS}) と一致しません: {desc!r}")
    assert f"{DEFAULT_LIST_LIMIT} 件" in desc, (
        f"一覧の窓の件数が定数 ({DEFAULT_LIST_LIMIT}) と一致しません: {desc!r}")


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
