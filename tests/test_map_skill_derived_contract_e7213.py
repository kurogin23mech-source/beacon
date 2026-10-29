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
指示を見逃す。そこで **実行可能な断片の中だけ**を見る: 引用ブロックに置かれた言及は許し、
フェンス付きブロック (言語タグを問わない) と引用ブロック外のコードスパンに現れたら落とす。
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

# 実行可能として扱うブロックは **フェンス言語を問わない**。```bash だけに縛っていたら、
# ```sh に変えるだけで同じ禁止コマンドが素通りした (PR#823 独立レビュー 2 体が実測)。
# 言語タグ無しの ``` も対象に含める。
FENCED_BLOCK_RE = re.compile(r"```[A-Za-z0-9_+.-]*\n(.*?)```", re.DOTALL)
# フェンスを使わず 1 行のコードスパンで案内する抜け道も見る (同レビュー)。
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# 地図を直接書く操作 (= 導出物を上書きする操作)。
# 引数の順を入れ替えた形も拾うため、動詞と doc-id の隣接ではなく **同じ行** で見る。
FORBIDDEN_IN_BASH = [
    (re.compile(r"doc\s+update\b[^\n]*\bapplication-map\b"),
     "beacon doc update application-map (導出物を直接上書き)"),
    (re.compile(r"doc\s+add\b[^\n]*\bapplication-map\b"),
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


def _normalize_command_text(s: str) -> str:
    """同じ操作の別の書き方を同じ形に潰す (行は保つ)。

    ``beacon doc update "application-map"`` / 行折り返し / python から
    ``subprocess.run(['beacon','doc','update','application-map'])`` で呼ぶ形は、
    シェル的には等価な操作なのに素の正規表現では別物に見える (PR#823 独立レビュー
    2 体が実測した抜け道)。引用符・カンマ・角括弧・行継続を落として同一視する。
    改行は **残す** — 無関係な 2 行が 1 行に繋がって誤検知するのを避けるため。
    """
    s = s.replace("\\\n", " ")                  # 行継続は 1 行に畳む
    s = re.sub(r"[\"'`,\[\]()]", " ", s)         # 引用符 / 区切り記号を落とす
    return re.sub(r"[^\S\n]+", " ", s)           # 改行以外の空白を 1 つに


def _bash_blocks(text: str) -> list:
    """実行可能として扱う断片 (フェンス付きブロック + インラインのコードスパン)。

    **引用ブロック (``>`` 行) は除く。** 禁止を説明するにはコマンド名を書かざるを
    得ないので、この Skill 群では「禁止と理由は引用ブロックに置く」という置き場の
    約束で区別する。引用ブロックの外にあるコードスパンは *案内* と見なして検査する
    (フェンスを使わず 1 行で書く抜け道を塞ぐ、PR#823 独立 AX レビュー)。散文で
    触れたいなら引用ブロックに置く、が守るべき作法。
    """
    out = [_normalize_command_text(b) for b in FENCED_BLOCK_RE.findall(text)]
    masked = FENCED_BLOCK_RE.sub("\n", text)      # フェンス内を二重に見ない
    prose = "\n".join(ln for ln in masked.splitlines()
                      if not ln.lstrip().startswith(">"))
    out += [_normalize_command_text(c) for c in INLINE_CODE_RE.findall(prose)]
    return out


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


@pytest.mark.parametrize("path", MAP_SKILLS, ids=lambda p: str(p.relative_to(REPO)))
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
            f"{path.relative_to(REPO)}: Step {mode} に、書き漏れを足す道 "
            "(beacon deliverable add) が実行ブロックとして無い")
    r_blocks = "\n".join(_bash_blocks(sections["R"]))
    assert "beacon deliverable retire" in r_blocks, (
        f"{path.relative_to(REPO)}: Step R に、幽霊を消す道 "
        "(beacon deliverable retire) が実行ブロックとして無い")


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


@pytest.mark.parametrize("path", MAP_SKILLS, ids=lambda p: str(p.relative_to(REPO)))
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
            f"{path.relative_to(REPO)}: Step {mode} に『地図を直接書くな / 導出物なので"
            "上書きされる』という警告が "
            "引用ブロックとして在りません。禁止と理由は同じ場所に並べてください "
            "(理由が無いと、次に触る AI が手近な直接書き込みに戻します)。")


# --- 機械照合が「節の二重化」を見ること (PR#823 独立 AX レビュー high) -------------
#
# 地図の節見出しは成果ログの --area / --category (自由入力) から起きるので、空白 1 つや
# 全角半角の違いで «同じ節» が 2 つ生える。楔の集合は変わらないため、書き漏れ / 幽霊の
# 照合はそれを 0 件のまま通していた — Skill が「綴りを完全一致させろ」と警告している
# まさにその破れを、Skill が唯一の確認手段として案内する照合が検出できない状態だった。

def _drift_module():
    import importlib.util
    path = REPO / "scripts" / "check-map-drift.py"
    spec = importlib.util.spec_from_file_location("check_map_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_same_spelling_heading_reemission_is_not_a_split():
    """同じ綴りの再掲は正常 (大節見出しは節が変わるたびに再掲される描画)。"""
    mod = _drift_module()
    text = "## B. 透明にする\n### B1. x\n## B. 透明にする\n### B3. y\n"
    assert mod.split_sections(text) == []


@pytest.mark.parametrize("twin", [
    "### A1. 状態を一望する\n- a\n### A1.状態を一望する\n- b\n",      # 半角スペースの有無
    "### A1. 状態を一望する\n### Ａ1. 状態を一望する\n",              # 全角/半角
    "## A. 見失わない\n## A.  見失わない\n",                          # 空白の数
])
def test_spelling_twin_headings_are_reported(twin):
    """綴り違いの双子は報告されること (= 二重に生えた節を検出する)。"""
    mod = _drift_module()
    found = mod.split_sections(twin)
    assert len(found) == 1, f"双子を検出していない: {found}"
    assert len(found[0]["spellings"]) == 2


def test_clean_requires_no_split_sections():
    """exit 0 の判定に節の二重化が入っていること (書き漏れ / 幽霊だけで緑にしない)。"""
    mod = _drift_module()
    base = {"counts": {"missing": 0, "phantom": 0, "split_sections": 0}}
    assert mod._clean(base) is True
    split = {"counts": {"missing": 0, "phantom": 0, "split_sections": 1}}
    assert mod._clean(split) is False
