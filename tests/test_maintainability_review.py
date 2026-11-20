"""ms-119 — maintainability review: AX's sibling (AI-experience of *changing* code).

AX measures "AI using the tool"; this measures "AI changing the code". It's a
SIBLING category (different operator), added purely as data via the e-4009
registry — no code change to review_spine / commands beyond generalising the
PR-open firing. These tests pin: (1) it registers from its descriptor, (2) its
原典 assembles, (3) PR-open fires BOTH ax + maintainability (data-driven from
fires_on), and clears both.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402
import commands  # noqa: E402
import commands_shared  # noqa: E402  (ms-127 e-4856: trigger helpers promoted here)
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- registration (data-driven, no code change) ---

def test_maintainability_registered_as_judge_run():
    reg = review_spine.judge_run_review_types()
    assert "maintainability" in reg
    assert reg["maintainability"]["origin"]["kind"] == "repo-file"
    assert reg["maintainability"]["fires_on"] == "pr-open"


def test_maintainability_files_exist():
    base = os.path.join(REPO, "skills", "maintainability-review")
    for f in ("principles.md", "review-type.json", "SKILL.md"):
        assert os.path.isfile(os.path.join(base, f)), f
    desc = json.load(open(os.path.join(base, "review-type.json"), encoding="utf-8"))
    assert desc["judge_run"] is True
    assert desc["id"] == "maintainability"


def test_judge_model_tiering_in_descriptors():
    """ms-119 cost tiering: independence (context-zero subagent) is universal, but
    the default judge MODEL is tiered by review type via the descriptor. Frequent
    PR-bound reviews (ax / maintainability) default to sonnet (capable but lighter),
    the rarer judgement-heavy philosophy to fable. Not haiku — cheap must not mean a
    judge too weak to be an instrument."""
    reg = review_spine.load_review_types()
    assert reg["ax"]["default_judge_model"] == "sonnet"
    assert reg["maintainability"]["default_judge_model"] == "sonnet"
    assert reg["philosophy"]["default_judge_model"] == "fable"


def test_skill_schema_has_introduced_vs_touched_axis():
    """First-dogfood skill_gap: findings must distinguish introduced (this diff's
    debt) from touched (pre-existing debt the diff followed)."""
    s = open(os.path.join(REPO, "skills", "maintainability-review", "SKILL.md"),
             encoding="utf-8").read()
    assert '"origin": "introduced | touched"' in s


#: 「直さずに記録して終わる」列の各要素が持つ欄 (ms-119 e-7554)。
#: この instrument は findings 側のキーを守る試験を持たない (元からの穴で、
#: e-7554 の変更とは別の話なのでここでは触らない)。足した分だけを守る。
REQUIRED_RECORD_ONLY_KEYS = (
    "record_only", "observed", "why_no_change_risk",
)


def test_skill_schema_has_the_record_only_column():
    """次の変更を誤らせない気づきを、直す列から外して記録する欄が在ること。

    この列が消えると、表記の揺れや命名の好みが findings に混ざって戻り、
    受け取る側が全部を修正依頼として読む状態になる (2026-11-19 にユーザーが
    「明らかに過剰な問題検出をしている」と指摘した状態)。散文で足しただけでは
    消えても誰も気づかないので、ここで員数を守る。

    **この緑が主張しないこと**: 検査は「その語がファイルのどこかに在るか」だけを
    見る (既存の ``REQUIRED_FINDING_KEYS`` 系と同じ形)。だから **一部だけ消した
    場合は緑のまま通る** —— JSON の例を残して規約の行だけ消す、等。丸ごと消えたら
    赤くなることは反転で実測したが、部分的な欠落は守っていない。
    """
    s = open(os.path.join(REPO, "skills", "maintainability-review", "SKILL.md"),
             encoding="utf-8").read()
    missing = [k for k in REQUIRED_RECORD_ONLY_KEYS
               if '"%s"' % k not in s and "`%s`" % k not in s]
    assert not missing, (
        "SKILL.md から記録のみの列の必須キーが消えています: %s。"
        "欄を増減するなら、この一覧も同時に直す" % missing)


def test_skill_defers_the_split_rule_to_the_origin():
    """どちらの列に入れるかの規則を、原典 §4 への参照で持っていること。

    規則を SKILL.md と原典の両方に全文で置くと、既定を変える次の AI が両方を
    揃えて直す必要があるのに、それを強制する仕組みが無い (独立保守性レビュー
    2026-11-20 medium)。参照の形を保つ。あわせて参照先が宙に浮いていないこと
    (原典 §4 に下限の規則が実在すること) も見る。
    """
    base = os.path.join(REPO, "skills", "maintainability-review")
    skill = open(os.path.join(base, "SKILL.md"), encoding="utf-8").read()
    origin = open(os.path.join(base, "principles.md"), encoding="utf-8").read()
    assert "原典 §4 の下限規則" in skill, (
        "SKILL.md が振り分け規則を原典 §4 への参照で持っていません。"
        "全文を書き戻すと、規則が 2 箇所に分かれて片方だけ古くなる")
    section = origin.split("## 4.")[-1].split("## 5.")[0]
    assert "報告の下限" in section, (
        "参照先 (原典 §4) に下限の規則が在りません。参照が宙に浮くと規則が黙って"
        "消えます")


def test_maintainability_bundle_assembles():
    b = review_spine.assemble_review_context(
        "maintainability", origin_id="skills/maintainability-review/principles.md",
        origin_content="原則1 ...", diff_text="+x", mode="diff", target_ref="PR #1",
    )
    assert b["review_type"] == "maintainability"


def test_principles_cover_the_seven_lenses():
    p = open(os.path.join(REPO, "skills", "maintainability-review", "principles.md"),
             encoding="utf-8").read()
    # the 7 maintainability lenses must all be present (the 原典 is the benchmark)
    for lens in ("局所性", "単一の真実源", "適切なサイズ", "疎結合",
                 "grep", "一貫性", "フィードバック"):
        assert lens in p, lens
    # and the AI-specific north star
    assert "コンテキスト" in p


# --- PR-open firing is data-driven over fires_on=="pr-open" ---

def test_pr_open_fires_both_ax_and_maintainability(tmp_path, monkeypatch):
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    commands._fire_pr_open_review_triggers("321", "feat: x",
                                           "https://github.com/o/r/pull/321")
    files = set(os.listdir(tmp_path))
    assert "ax-review-due-321.json" in files
    assert "maintainability-review-due-321.json" in files
    m = json.loads((tmp_path / "maintainability-review-due-321.json").read_text())
    assert m["kind"] == "maintainability-review-due"
    assert "--type maintainability --pr 321" in m["message"]


def test_pr_open_clear_removes_both(tmp_path, monkeypatch):
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    commands._fire_pr_open_review_triggers("55", "t", "https://github.com/o/r/pull/55")
    assert len(list(tmp_path.glob("*-review-due-55.json"))) == 2
    cmd_pr._clear_pr_open_review_triggers("55")
    assert list(tmp_path.glob("*-review-due-55.json")) == []


def test_ax_shim_still_works(tmp_path, monkeypatch):
    """The AX-specific shim (pinned by the e-4003 tests) delegates to the generic
    writer and still produces an ax-review-due trigger."""
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    commands._fire_ax_review_due_trigger("7", "t", "https://github.com/o/r/pull/7")
    t = json.loads((tmp_path / "ax-review-due-7.json").read_text())
    assert t["kind"] == "ax-review-due"
    assert "--type ax --pr 7" in t["message"]
