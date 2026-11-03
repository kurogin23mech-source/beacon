"""盤の既定と文面を突き合わせるガード自身の試験 (ms-173 e-7266)。

姉妹の `check-single-skill-parser.py` には同形の試験が在るのに、
`check-idle-default-claims.py` には無かった。独立保守性レビューが指摘した通り、
test-the-test が無いガードは **免除集合の境界を機械で確かめられない** ので、
文面を直す人が「主張が無い」のか「言い回しが網から外れた」のかを区別できない。

ここで固定するのは 3 つ:
  1. 拾うべき文 (現在形で既定を述べる各綴り) が拾われること
  2. 免除すべき文 (経緯 / 操作説明 / 旗の値の例示) が拾われないこと
  3. 真実源 (Go 定数) の読み取りが 3 状態 (0 / 値 / 判定不能) を返すこと

**独立レビューが実測で素通りさせた綴りは、ここに実例として載せてある。** 網を
広げる/狭めるときは、まずこの表に例を足してから触る (そうしないと、次に同じ
綴りが素通りしても誰も気づかない)。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "check-idle-default-claims.py"


def _load():
    spec = importlib.util.spec_from_file_location("idle_claims_guard", GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_claims_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load()


# --- 1. 拾うべき文 -----------------------------------------------------------
#
# 先頭 3 件は **独立 AX レビューが実測で素通りさせた綴り** (e-7266)。決め打ちの
# 文を並べる形から「畳む動作の語 + 既定/時間の語」に変えたのはこれが理由。
@pytest.mark.parametrize("sentence", [
    "既定では、しばらく操作が無いと盤は停止します",
    "既定では 30 分後に盤を終了します",
    "By default the board shuts down after 30 minutes idle",
    "手元で開いた盤は、誰も見に来なくなって 30 分で自ら畳みます",
    "a board opened locally closes after 30m with nobody watching",
    "盤は 2h で自動的に畳みます",
])
def test_present_tense_claims_are_caught(sentence):
    assert G._claims_it_closes(sentence), (
        f"「既定で畳む」と読める文が拾われていない: {sentence!r} — "
        "網を広げたら、この表にも例を足すこと")


# --- 2. 免除すべき文 ---------------------------------------------------------
@pytest.mark.parametrize("sentence,why", [
    ("盤を終了するには Ctrl+C を押してください", "操作説明 (既定の主張ではない)"),
    ("畳ませたいときは --idle-timeout 30m のように時間を明示してください",
     "指定の案内 (既定は述べていない)"),
    ('"--idle-timeout <30m|2h|0>"', "旗の値の例示 — 散文ではない"),
    ("BEACON_VIEW_IDLE_TIMEOUT=<時間> … 畳むまでの時間 (例 2h)",
     "旗の値の例示 (例 2h) は既定の主張ではない"),
])
def test_non_claims_are_not_caught(sentence, why):
    assert not G._claims_it_closes(sentence), f"誤検知: {sentence!r} — {why}"


# --- 2b. 経緯の記述は、文単位の免除で落ちること -----------------------------
#
# `_claims_it_closes` は文の中身だけを見るので、経緯の文も「拾う」と答える。
# 免除は `find_claims` 側の HISTORY_MARKERS で行う。**どちらの層が免除している
# かを取り違えないよう、層ごとに試験する。**
@pytest.mark.parametrize("sentence", [
    "以前は手元で開いた盤を 30 分で畳んでいました",
    "かつては手元で開いた盤だけを 30 分で畳んでいた",
])
def test_history_is_exempted_by_the_marker_layer(sentence):
    assert any(h in sentence for h in G.HISTORY_MARKERS), (
        f"経緯の文が history として免除されない: {sentence!r}")


# --- 3. 真実源の 3 状態 ------------------------------------------------------
def test_truth_source_has_three_states(tmp_path, monkeypatch):
    """0 / 読める値 / 判定不能 を区別すること。

    判定不能を「食い違いなし」に畳むと、等価なリファクタ (`time.Duration(0)`) で
    「既定を変えたなら文面も直して」という嘘の診断が出る (独立レビューが実測)。
    """
    for expr, want_never in [
        ("0", True),
        ("30 * time.Minute", False),
        ("2 * time.Hour", False),
        ("time.Duration(0)", None),   # 読めない
        ("someConst", None),          # 読めない
    ]:
        fake = tmp_path / "idle.go"
        fake.write_text(f"package main\n\nconst defaultIdleTimeout = {expr}\n",
                        encoding="utf-8")
        monkeypatch.setattr(G, "GO_CONST", fake)
        never, got_expr = G.default_idle()
        assert never is want_never, (
            f"{expr!r} の判定が {never} (期待 {want_never})")
        assert got_expr == expr


def test_expected_tokens_render_both_spellings():
    assert set(G.expected_tokens("30 * time.Minute")) >= {"30m", "30 分"}
    assert set(G.expected_tokens("2 * time.Hour")) >= {"2h", "2 時間"}
    assert G.expected_tokens("0") == []


# --- 4. 走査範囲に 3 箇所目が入っていること ---------------------------------
def test_scan_sites_include_the_third_file():
    """`lib/cmd_view.py` が走査対象であること (e-7266)。

    e-7264 で既定を反転したとき、ここにも新しい既定を書いたのに検査の対象に
    入れ忘れていた。独立レビューが「自分が書いた場所が走査外」と実測した。
    """
    paths = {str(path) for _, path, _ in G.CLAIM_SITES}
    assert any(p.endswith("lib/cmd_view.py") for p in paths), (
        "lib/cmd_view.py が CLAIM_SITES に無い")
    assert any(p.endswith("viewer/README.md") for p in paths)
    assert any(p.endswith("lib/commands.py") for p in paths)


# --- 5. 現ツリーが緑であること (回帰) ---------------------------------------
def test_current_tree_passes():
    report_never, expr = G.default_idle()
    claims = G.find_claims()
    assert report_never is True, f"既定が 0 でなくなった: {expr}"
    assert claims == [], f"文面に「畳む」の主張が残っている: {claims}"
