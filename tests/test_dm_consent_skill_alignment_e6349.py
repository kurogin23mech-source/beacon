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


def _consent(**kw):
    base = dict(sender_user_id="", recipient_user_id="", channel="dm",
                is_reply=False, operation_envelope=False, shared_trek=False)
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
    ({"shared_trek": True}, dm_consent.CONSENT_SKIP_SHARED_TREK),
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
