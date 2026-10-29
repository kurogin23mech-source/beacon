"""全貌マップを直接書き換える指示が Skill に無いこと (e-7213)。

全貌マップ (CORE doc ``application-map``) は成果ログ (deliverable-changelog) からの
**導出物**で、文書の冒頭にも「自動生成・手編集禁止・手で書き換えても次の記帳で上書き
される」と明記されている。それでも ``/beacon-map`` の手順は長く「本文を直して
``beacon doc update application-map`` で書き戻す」ままだった。指示どおり進めると
**次の記帳で消える編集をして「直した」と報告する**ことになる (2026-10-29 の reconcile で
実際に踏みかけた)。

ここは「消える編集の案内」が Skill に再び現れたら赤くなる歯止め。

**禁止文と実行指示を構造で区別する。** 直し方を説明するには、禁止する側でもコマンド名を
書かざるを得ない (「``beacon doc update application-map`` を打つな」)。素朴な全文の
substring 一致にすると、その警告文自身で赤くなるか、さもなければ緩めた結果として本物の
指示を見逃す。そこで **実行可能なブロック (```bash フェンス) の中だけ**を見る: 散文での
言及は許し、フェンス内に現れたら落とす。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Skill 本文の 3 コピー (正本 / 共有 / Codex plugin)。綴りが違うので find / grep で
# 漏れやすい — ここは明示的に 3 つ並べる。
SKILL_TREES = [
    REPO / "skills",
    REPO / "shared" / "skills",
    REPO / "plugins" / "beacon" / "skills",
]

BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)

# 地図を直接書く操作 (= 導出物を上書きする操作)。
FORBIDDEN_IN_BASH = [
    (re.compile(r"doc\s+update\s+application-map"),
     "beacon doc update application-map (導出物を直接上書き)"),
    (re.compile(r"doc\s+add\b[^\n]*--id\s+application-map"),
     "beacon doc add --id application-map (導出物を直接作成)"),
]


def _skill_files() -> list:
    out = []
    for tree in SKILL_TREES:
        if not tree.is_dir():
            continue
        out.extend(sorted(p for p in tree.rglob("*.md") if p.is_file()))
    if not out:
        raise AssertionError(
            "Skill 本文が 1 つも見つかりません (配置が変わったならこの試験も直してください)")
    return out


def _bash_blocks(text: str) -> list:
    return BASH_BLOCK_RE.findall(text)


def test_no_skill_instructs_writing_the_derived_map():
    """どの Skill も、地図への直接書き込みを実行ブロックで案内していないこと。"""
    offenders = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for block in _bash_blocks(text):
            for pattern, label in FORBIDDEN_IN_BASH:
                if pattern.search(block):
                    offenders.append(f"{path.relative_to(REPO)}: {label}")
    assert not offenders, (
        "全貌マップを直接書き換える指示が Skill の実行ブロックに在ります:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n地図は成果ログからの導出物なので、書き込みは "
        "beacon deliverable add / retire / supersede 経由にしてください "
        "(直接の書き換えは次の記帳で消えます)。")


MAP_SKILLS = [
    REPO / "skills" / "beacon-map.md",
    REPO / "shared" / "skills" / "beacon-map" / "SKILL.md",
    REPO / "plugins" / "beacon" / "skills" / "beacon-map" / "SKILL.md",
]


def _mode_sections(text: str) -> dict:
    """Skill 本文を reconcile モード (Step R) と生成モード (Step G) に割る。

    見出しが見つからない / 空で返るのを「一致」と読まれないよう、どちらも例外にする
    (無言の 0 件一致は偽の緑そのもの)。
    """
    r_head = text.find("## Step R:")
    g_head = text.find("## Step G:")
    if r_head < 0 or g_head < 0:
        raise AssertionError(
            "Step R / Step G の見出しが見つかりません。改名したならこの試験も直して"
            "ください (ガードが黙って無効になるのを防ぐため例外にしています)。")
    if not (r_head < g_head):
        raise AssertionError("Step R が Step G より後ろに在ります (節の割り方を確認)")
    sections = {"R": text[r_head:g_head], "G": text[g_head:]}
    for name, body in sections.items():
        if not _bash_blocks(body):
            raise AssertionError(f"Step {name} に実行ブロックが 1 つもありません")
    return sections


@pytest.mark.parametrize("path", MAP_SKILLS, ids=lambda p: str(p.name))
def test_map_skill_instructs_the_deliverable_route(path):
    """禁止しただけで終わらず、**代わりに何をするか**が実行ブロックに在ること。

    直接書き込みを消すだけなら上の試験は緑のまま通ってしまう (= 手順が「地図は直せない」
    で終わり、足す道が無い Skill になる)。足す道と消す道の両方を要求する。

    **モードごとに見る** (e-7213 の反転ハーネスの指摘): 文書全体で 1 回でも出ていれば
    よいとすると、片方のモード (例: reconcile) から足す道が消えても、もう片方
    (生成モード) に残っている分で緑のまま通ってしまう。手順はモードごとに完結して
    いなければ使えない。
    """
    assert path.exists(), f"{path} が無い (3 コピーの綴りを確認)"
    sections = _mode_sections(path.read_text(encoding="utf-8"))
    for mode, body in sections.items():
        blocks = "\n".join(_bash_blocks(body))
        assert "beacon deliverable add" in blocks, (
            f"Step {mode} に、書き漏れを足す道 (beacon deliverable add) が実行ブロックとして無い")
    r_blocks = "\n".join(_bash_blocks(sections["R"]))
    assert "beacon deliverable retire" in r_blocks, (
        "Step R に、幽霊を消す道 (beacon deliverable retire) が実行ブロックとして無い")


def _blockquotes(text: str) -> list:
    """連続する ``>`` 行を 1 つの引用ブロックとして取り出す。"""
    out, cur = [], []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            cur.append(line.lstrip().lstrip(">").strip())
        elif cur:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


@pytest.mark.parametrize("path", MAP_SKILLS, ids=lambda p: str(p.name))
def test_map_skill_states_the_derived_contract(path):
    """「地図は導出物だから直接書くな」という理由が、**各モードの警告として**在ること。

    コマンドだけ差し替えて理由を書かないと、次に触る AI が「なぜ遠回りするのか」を
    読み取れず、手近な直接書き込みに戻す。

    **文書のどこかに語が在るかでは見ない** (e-7213 の反転ハーネスの指摘): 語を数える
    だけだと、警告そのものを削っても別の箇所に同じ語が残っていて緑のまま通る。
    「禁止」と「なぜ (導出物なので上書きされる)」が **同じ引用ブロックに並んでいる**
    ことを、モードごとに要求する。
    """
    sections = _mode_sections(path.read_text(encoding="utf-8"))
    for mode, body in sections.items():
        warned = [q for q in _blockquotes(body)
                  if "導出物" in q and "上書き" in q
                  and ("直接" in q or "手編集" in q)]
        assert warned, (
            f"Step {mode} に『地図を直接書くな / 導出物なので上書きされる』という警告が "
            "引用ブロックとして在りません。禁止と理由は同じ場所に並べてください "
            "(理由が無いと、次に触る AI が手近な直接書き込みに戻します)。")
