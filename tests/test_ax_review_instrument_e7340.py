"""AX レビューの計器 (原典 + harness) が、黙って崩れないように固定する (ms-119)。

## なぜ要るか

AX レビューの原典 (``skills/ax-review/principles.md``) と harness
(``skills/ax-review/SKILL.md``) は、**この先のすべてのレビューが判定に使う契約**
である。にもかかわらず、原則を 1 つ丸ごと消しても、必須の出力キーを消しても、
リポジトリのどのテストも赤くならない状態だった (独立レビューが実測: 原典から P4 を
削除しても、harness から ``failure_class`` を削除しても 31 件緑のまま)。

姉妹 capability の ``test_maintainability_review.py::test_principles_cover_the_seven_lenses``
が既に同型の固定をしている。**同じリポの同じファミリに手本があるのに倣っていなかった**
という指摘なので、ここで揃える。

## 何を固定するか (そして何を固定しないか)

固定するのは **集合と名前** — 原則の見出し、閉じた語彙の値、必須の出力キー。
散文の言い回しは固定しない (書き換えのたびに赤くなるのは、守る価値のない硬さ)。

特に ``severity`` というキー名は **共有の契約** なので名指しで固定する:
``review_spine.aggregate_review_reports`` が全レビュー種別の finding から
``severity`` を読んで重さの列を作るため、AX だけ別名にすると **AX の指摘だけ重さが
空になる**。初版の実装がこれを踏み、独立レビューが実測した。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, "skills", "ax-review")


def _origin():
    with open(os.path.join(BASE, "principles.md"), encoding="utf-8") as fh:
        return fh.read()


def _harness():
    with open(os.path.join(BASE, "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


# --- 原典: 原則の集合 -------------------------------------------------------

PRINCIPLE_HEADINGS = (
    "### P1 操作の語彙を閉じる",
    "### P2 作れるものは読める",
    "### P3 危険な操作は、禁止ではなく経路ごと封じる",
    "### P4 同じ操作を 2 回呼んでも壊れない",
    "### P5 見て回れ、辿れる",
    "### P6 失敗は、種類つきの文字で返す",
    "### P7 文脈は役ごとに絞る",
    "### P8 記録は追記だけ",
)


def test_origin_declares_all_eight_principles():
    src = _origin()
    missing = [h for h in PRINCIPLE_HEADINGS if h not in src]
    assert not missing, (
        "原典から原則が消えています: %s。原則を増減するなら、この一覧も同時に直す "
        "(両方を直させることが、片方だけ消える事故を防ぐ唯一の仕掛け)" % missing)


def test_origin_declares_the_two_cross_cutting_lenses_with_their_tokens():
    """横断レンズは番号を持たないので、報告に使う綴りを原典が自分で名乗る。

    初版は原典に日本語の見出しだけ書き、報告用トークン (``naming`` /
    ``least-surprise``) は harness にしか無かった。原典だけ読んだ judge は
    「このレンズを何という文字列で報告するのか」を解決できない (独立レビュー指摘)。
    """
    src = _origin()
    for jp, token in (("命名の一貫性", "naming"), ("最小驚き", "least-surprise")):
        assert jp in src, jp
        assert "`%s`" % token in src, (
            "原典が横断レンズ %r の報告トークン %r を名乗っていません。"
            "原典だけ読んだ judge が綴りを導けません" % (jp, token))


def _first_column_of_table_under(heading):
    """``heading`` の節に最初に現れる表の、1 列目 (ヘッダ行を除く) を集合で返す。

    **部分一致で探さない理由**: 語彙の名前は原典の別の節 (finding の欄の説明など)
    にも現れるので、``name in src`` だと **定義の表から 1 行消しても緑のまま**に
    なる。初版はこの形で書いてしまい、反転で実測して捕まえた (このリポジトリが
    何度も踏んでいる「緩い一致は false-pass する」型)。だから定義の在りかを
    構造で特定し、そこから集合を取り出して突き合わせる。
    """
    src = _origin()
    i = src.index(heading)
    nxt = src.find("\n## ", i + 1)
    section = src[i:nxt if nxt != -1 else len(src)]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    out = set()
    for ln in rows[2:]:                      # 0=ヘッダ, 1=区切り
        cell = ln.split("|")[1].strip()
        cell = re.sub(r"^\*\*|\*\*$", "", cell).strip()
        if cell:
            out.add(cell)
    return out


def test_origin_declares_the_four_failure_classes():
    got = _first_column_of_table_under("## 4. 失敗の分類")
    assert got == {"届いていない", "指せない", "言えない", "確かめられない"}, (
        "失敗の分類が原典の定義表と食い違っています: %s。分類を増減するなら harness の "
        "failure_class の値域も同時に直す" % sorted(got))


def test_origin_declares_the_three_weights():
    got = _first_column_of_table_under("## 5. 重さの付け方")
    assert got == {"顧客に届く", "手が失われる・二重になる", "人の手間が増える"}, (
        "重さの語彙が原典の定義表と食い違っています: %s。増減するなら harness の "
        "severity の値域も同時に直す" % sorted(got))


# --- harness: 必須の出力キー -----------------------------------------------

REQUIRED_FINDING_KEYS = (
    "principle", "surface", "defect", "failure_class",
    "what_goes_wrong", "structural_fix", "severity", "detectability",
)


def test_harness_requires_every_finding_key():
    src = _harness()
    missing = [k for k in REQUIRED_FINDING_KEYS if '"%s"' % k not in src]
    assert not missing, (
        "harness の出力スキーマから必須キーが消えています: %s。欄を増減するなら、"
        "この一覧も同時に直す" % missing)


def test_harness_requires_the_per_principle_verdict_map():
    """指摘が無い原則も「問題なし」と明記させる欄が在ること。

    見ていないことと問題が無いことを読み手が区別できるようにするための欄で、
    これが消えると「黙って飛ばした」が「見て問題が無かった」と読まれる。
    """
    src = _harness()
    assert "principles_reviewed" in src
    for v in ("ok", "finding", "unchecked"):
        assert '"%s"' % v in src or "`%s`" % v in src, v
    assert "basis" in src, (
        "principles_reviewed の裏付け欄 (basis) が消えています。裸の値だけに戻すと、"
        "10 キー全部を無検証で ok と書いた出力と区別できません")


def test_harness_names_every_principle_the_origin_declares():
    """harness が P1〜P8 を名指しし、原典と員数が揃っていること。"""
    src = _harness()
    missing = [n for n in range(1, 9) if "P%d" % n not in src]
    assert not missing, "harness が名指ししていない原則: %s" % missing


# --- 共有の契約: 重さを載せるキーの名前 -------------------------------------

def test_the_weight_key_is_the_one_the_fold_reads():
    """AX の重さが、畳んだレポートに実際に載ることを機械で確かめる。

    ``aggregate_review_reports`` は finding の ``severity`` を読む。AX だけキー名を
    変えると **AX の指摘だけ重さが空になる** (初版で踏み、独立レビューが実測)。
    harness が宣言するキー名と、fold が読むキー名を突き合わせる。
    """
    folded = review_spine.aggregate_review_reports([
        {"review_type": "ax", "findings": [
            {"title": "t", "file": "f", "line": 1,
             "severity": "顧客に届く", "detectability": "silent"}]},
    ])
    got = folded["findings"][0]["severity"]
    assert got == "顧客に届く", (
        "fold が AX の重さを拾えていません (got=%r)。harness の重さのキー名と "
        "aggregate_review_reports が読むキー名が割れています" % got)
    assert '"severity"' in _harness(), (
        "harness が severity を宣言していません。fold はこのキーを読むので、"
        "別名にすると AX の指摘だけ重さが空になります")
