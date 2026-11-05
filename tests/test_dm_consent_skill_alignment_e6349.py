"""宛先確認の規則が、実装と手順書で食い違わないことの試験 (ms-160 e-6349).

起きた事故: `/beacon-dm-send` は「same-project なら `--recipient-confirmed` を
付けない」と書いていた。しかしサーバ側の規則 `dm_consent.classify_send_consent`
は **プロジェクトを一切見ない** — 「宛先が別の人間か」で判定する。同一プロジェクトに
居る協働者宛に手順どおり送ると 403 (`cross_user_missing_confirmation`) で弾かれた
(2026-09-09)。**手順書に従うほど失敗する**型で、AI も人間も原因に辿り着けなかった。

構造的な直し: 手順書は規則を書き写さず `beacon bus consent-check` に問い合わせる。
これらはその不変条件を固定する:

  * 規則が実際に project を見ていないこと (= 手順書が project 軸で書かれていたのは
    読み違えであって、実装がそうだったのではない);
  * 同一プロジェクトの別ユーザー宛で確認が要る、という報告された事故そのもの;
  * 判定理由の定数すべてに 1 行説明が在ること (= 新しい理由が説明なしで入ると赤);
  * 手順書が判定を散文で再実装せず、CLI に問い合わせる形になっていること
    (= project 軸の文言が戻ってきたら赤);
  * 3 コピー (skills / shared / plugins) が揃っていること。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dm_consent  # noqa: E402

SKILL_COPIES = (
    ROOT / "skills" / "beacon-dm-send.md",
    ROOT / "shared" / "skills" / "beacon-dm-send" / "SKILL.md",
    ROOT / "plugins" / "beacon" / "skills" / "beacon-dm-send" / "SKILL.md",
)


def _load_guard():
    """`scripts/check-dm-consent-alignment.py` を読み込む。

    CI の lint-docs ジョブには pytest が無いので、実際に走るのはあの素のスクリプト。
    ここで同じものを読んで検証し、**CI が走らせる実体と試験対象を一致させる**
    (最初の版は ci-strict-drift-guards.sh に `python3 -m pytest` を直に書いて
    CI を落とした)。
    """
    spec = importlib.util.spec_from_file_location(
        "_dm_consent_alignment_e6349",
        ROOT / "scripts" / "check-dm-consent-alignment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_the_standalone_guard_is_green_on_this_tree():
    """CI が実際に走らせるスクリプトが、今の木で通ること。"""
    assert _load_guard().find_problems() == []


def test_the_standalone_guard_needs_no_pytest():
    """あのスクリプトが pytest を import しないこと (lint-docs ジョブに無いため)。"""
    src = (ROOT / "scripts" / "check-dm-consent-alignment.py").read_text()
    assert "import pytest" not in src
    assert "pytest" not in src.split('"""', 2)[2] or "python3 -m pytest" not in src


def test_ci_guards_script_does_not_invoke_pytest():
    """ci-strict-drift-guards.sh が pytest を起動しないこと。

    あの script は pytest の入っていない lint-docs ジョブからも呼ばれる。ここで
    起動行を足すと、手元では緑のまま CI だけが落ちる (実際に一度落とした)。
    """
    lines = (ROOT / "scripts" / "ci-strict-drift-guards.sh").read_text().splitlines()
    offenders = [l for l in lines
                 if "pytest" in l and not l.lstrip().startswith("#")]
    assert not offenders, f"pytest を起動している行: {offenders}"


def _consent(**kw):
    base = dict(sender_user_id="", recipient_user_id="", channel="dm",
                is_reply=False, operation_envelope=False)
    base.update(kw)
    return dm_consent.classify_send_consent(**base)


# --- 実装側の不変条件 ---------------------------------------------------------

def test_the_rule_does_not_look_at_the_project():
    """規則の入力に project が無い。手順書が project 軸だったのは読み違い。"""
    import inspect
    params = set(inspect.signature(dm_consent.classify_send_consent).parameters)
    assert not {p for p in params if "project" in p.lower()}, params


def test_same_project_different_user_still_needs_confirmation():
    """報告された事故そのもの: 同一プロジェクトでも相手が別の人間なら確認が要る。

    project は判定に入らないので、`同一プロジェクト` は別ユーザー性を打ち消さない。
    """
    required, reason = _consent(sender_user_id="me@example.com",
                                recipient_user_id="colleague@example.com")
    assert required is True
    assert reason == dm_consent.CONSENT_REQUIRED_CROSS_USER


def test_same_user_is_the_only_identity_based_skip():
    required, reason = _consent(sender_user_id="me@example.com",
                                recipient_user_id="me@example.com")
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_SAME_USER


@pytest.mark.parametrize("kw,expected_reason", [
    ({"is_reply": True}, dm_consent.CONSENT_SKIP_REPLY),
    ({"channel": "claim-signal"}, dm_consent.CONSENT_SKIP_NON_DM),
    ({"operation_envelope": True}, dm_consent.CONSENT_SKIP_OPERATION),
])
def test_carve_outs_hold_for_a_cross_user_pair(kw, expected_reason):
    """各 carve-out は「別ユーザー同士」でも確認を免除する (= 既存経路の非退行)。"""
    required, reason = _consent(sender_user_id="me@example.com",
                                recipient_user_id="colleague@example.com", **kw)
    assert required is False
    assert reason == expected_reason


def test_every_reason_constant_has_a_one_line_explanation():
    """理由を増やしたら説明も足す。説明なしの理由が入ったらここで赤くなる。

    説明は「なぜ確認が要る / 要らないか」を読み手に示す唯一の面 (draft に載る)。
    足し忘れると draft が識別子だけを見せ、読み手が判断できない。
    """
    constants = {v for k, v in vars(dm_consent).items()
                 if k.startswith("CONSENT_SKIP_") or k.startswith("CONSENT_REQUIRED_")}
    missing = constants - set(dm_consent.CONSENT_REASON_EXPLANATIONS)
    assert not missing, f"説明が未登録の理由: {sorted(missing)}"
    # 逆向き (説明だけ在って定数が消えた) も掃除させる。
    stale = set(dm_consent.CONSENT_REASON_EXPLANATIONS) - constants
    assert not stale, f"対応する定数が無い説明: {sorted(stale)}"


def test_retired_reasons_live_in_their_own_map_not_the_live_one():
    """引退した判定理由は生きている規則の辞書に混ぜない (ms-185 e-7068)。

    判定理由は監査記録に永続化されるので、規則を消しても**過去の記録を読む**ため
    に説明は要る。一方で生きている辞書に残すと、上の双方向チェックが「定数が無い
    説明」として掃除を要求し、消すと過去が読めなくなる — どちらに倒しても間違う。
    だから 2 つの辞書に分けてある。ここでは混ざっていないことを固定する。
    """
    live = set(dm_consent.CONSENT_REASON_EXPLANATIONS)
    retired = set(dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS)
    assert not (live & retired), f"両方に在る判定理由: {sorted(live & retired)}"
    # 引退側は定数を持たない (= 誰もセットしない判定を残さない)。
    constants = {v for k, v in vars(dm_consent).items()
                 if k.startswith("CONSENT_SKIP_") or k.startswith("CONSENT_REQUIRED_")}
    assert not (retired & constants), "引退させた理由が定数として生きている"
    # それでも読める。
    for reason in retired:
        assert "未登録" not in dm_consent.explain_consent_reason(reason)


def test_unknown_reason_explains_itself_instead_of_raising():
    """表示経路なので、未知の理由で例外にして送信を壊さない。"""
    out = dm_consent.explain_consent_reason("some_new_reason")
    assert "some_new_reason" in out
    assert "dm_consent" in out


# --- 手順書側の不変条件 -------------------------------------------------------

@pytest.mark.parametrize("path", SKILL_COPIES, ids=lambda p: p.parts[-2])
def test_skill_asks_the_cli_instead_of_restating_the_rule(path):
    text = path.read_text()
    assert "beacon bus consent-check" in text, "判定を CLI に問い合わせていない"
    assert "recipient_confirmation_required" in text, "返り field を説明していない"


@pytest.mark.parametrize("path", SKILL_COPIES, ids=lambda p: p.parts[-2])
def test_skill_no_longer_keys_the_decision_on_the_project(path):
    """project 軸の判定文言が戻ってきたら赤くする (= これが事故の本体だった)。

    「same-project だから付けない」という形の断定を禁じる。project という語自体は
    別の文脈 (cross-project の宛先確認や `--project` フラグ) で正しく使われるので、
    語の存在ではなく **判定としての断定** を突く。
    """
    text = path.read_text()
    forbidden = [
        "same-project (同一プロジェクト) | 付けない",
        "| same-project | 付けない",
        "同一プロジェクトなら宛先確認は不要",
        "同じプロジェクトなら宛先確認は不要",
    ]
    hit = [f for f in forbidden if f in text]
    assert not hit, f"project 軸の判定が残っている: {hit}"


@pytest.mark.parametrize("path", SKILL_COPIES, ids=lambda p: p.parts[-2])
def test_skill_states_the_axis_explicitly(path):
    """「判定軸は別ユーザーか」を明言しているか (読み手が誤読しないため)。"""
    text = path.read_text()
    assert "別のユーザー" in text or "別ユーザー" in text
    assert "プロジェクトが同じかどうかではない" in text or \
           "プロジェクトが同じかは関係しない" in text or \
           "プロジェクトが同じかどうかは判定に関係ありません" in text


def test_the_three_skill_copies_agree_on_this_section():
    """3 コピーの Step 3.1 が同一であること (片方だけ直す drift を止める)。"""
    sections = []
    for p in SKILL_COPIES:
        t = p.read_text()
        sections.append(t[t.index("### Step 3.1:"):t.index("### Step 3.2:")])
    assert sections[0] == sections[1] == sections[2]


# ---------------------------------------------------------------------------
# ms-185 e-7078 / 独立 AX レビュー — 引退した免除の軸が「まだ在る」と読める
# 出力を残さない。
#
# **起きたこと**: `beacon bus consent-check` は、答えが「宛先確認が必要」のとき
# 「この軸は判定していないので、真なら実際には不要かもしれない」という但し書きを
# 出す。その一覧が **JSON 側と人間向け文面で別々に手書きされていた**。Trek 参加を
# 理由にした免除が e-7068 で判定ごと消えたあと、JSON 側は Trek を外したのに
# **人間向けの文面だけが「Trek 参加中なら実際には不要」と言い続けた** (= 同じ
# 関数の中で 2 つの出力が食い違う)。文脈を持たない読み手がこれを信じると、
# 要るはずの人間確認を省きうる — 認可の境界に関わる。
#
# **なぜこの測り方か**: 「Trek という語が無いこと」を文字列で測ると、語を言い換え
# られた瞬間に素通りする (= 偽の緑)。代わりに **引退した判定理由の辞書そのもの**
# (dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS) と突き合わせる。新しく引退した
# 軸が出ても、辞書に入った時点でこの試験が効く。
# ---------------------------------------------------------------------------

def _carve_outs():
    import cmd_bus
    return cmd_bus._CONSENT_CARVE_OUTS_NOT_CHECKED


def test_retired_consent_axes_are_not_offered_as_carve_outs():
    """引退した判定理由が「見ていない軸」の一覧に居ない。

    居ると、答えが「必要」のときに「その軸が真なら不要」と案内してしまうが、
    その軸を真にする機構はもう無い (= 満たせない免除を案内する)。
    """
    retired = set(dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS)
    assert retired, "前提: 引退した判定理由の辞書が空ではない"
    offered = {key for key, _label in _carve_outs()}
    leaked = offered & retired
    assert not leaked, (
        f"引退した判定理由が免除の案内に残っている: {sorted(leaked)}。"
        "その軸を真にする機構はもう無いので、案内は満たせない免除になる")


def test_the_two_outputs_of_consent_check_share_one_source():
    """JSON と人間向け文面が同じ出所から導かれている。

    手書きの一覧を 2 箇所に持つと、軸が引退したとき片方だけ古くなる
    (実際に起きた: JSON は Trek を外したのに文面だけ残った)。
    """
    import inspect

    import cmd_bus
    src = inspect.getsource(cmd_bus.cmd_bus_consent_check)
    # どちらの出力も定数を経由していること。文字列直書きが復活したら赤くなる。
    assert "_CONSENT_CARVE_OUTS_NOT_CHECKED" in src, (
        "免除の一覧が定数を経由していない — 手書きに戻った疑い")
    assert src.count("_CONSENT_CARVE_OUTS_NOT_CHECKED") >= 2, (
        "JSON と人間向け文面の片方しか定数を見ていない "
        "(= もう片方が手書きのまま古くなる構造が残っている)")
    # 引退した軸の語が、案内の文面に直書きで戻ってきていないこと。
    assert "Trek 参加中" not in src, (
        "引退した免除 (Trek 参加) の案内が文面に直書きで戻っている")
