#!/usr/bin/env python3
"""宛先確認の規則 ↔ 手順書 の整合ガード (ms-160 e-6349).

依存は標準ライブラリだけ。`scripts/ci-strict-drift-guards.sh` は pytest の入って
いない軽量ジョブ (lint-docs) からも呼ばれるので、ここに pytest を持ち込まない
(最初の版はそれで CI を落とした)。挙動レベルの契約は
`tests/test_bus_consent_check_contract_e6349.py` が担い、このスクリプト自身の
正しさは `tests/test_dm_consent_skill_alignment_e6349.py` が検証する。

守る不変条件:

1. 判定理由の定数すべてに 1 行説明が在る (逆向き = 定数の無い説明も掃除させる)。
   説明は draft に出る唯一の「なぜ」なので、足し忘れると読み手が識別子だけを見る。
2. 手順書が規則を散文で再実装せず、`beacon bus consent-check` に問い合わせている。
3. 手順書に project 軸の判定文言が戻っていない。
   **これが事故の本体**: 「same-project なら宛先確認は不要」と書かれていたが、規則は
   project を見ず「宛先が別の人間か」で判定する。同一プロジェクトの協働者宛に手順
   どおり送ると 403 で拒否された (2026-09-09)。手順書に従うほど失敗する型だった。
4. 手順書が説明している JSON field が、実際に verb の出力に組み立てられている。
5. 3 コピー (skills / shared / plugins) の Step 3.1 が一致している。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

SKILL_COPIES = (
    ROOT / "skills" / "beacon-dm-send.md",
    ROOT / "shared" / "skills" / "beacon-dm-send" / "SKILL.md",
    ROOT / "plugins" / "beacon" / "skills" / "beacon-dm-send" / "SKILL.md",
)
VERB_SOURCE = ROOT / "lib" / "cmd_bus.py"

# 手順書が読者に説明している JSON field。ここを変えるなら手順書も変える。
PROMISED_FIELDS = (
    "recipient_confirmation_required",
    "reason",
    "explanation",
    "identity_uncertain",
    "channel_recognized",
    "carve_outs_not_checked",
    "caveats",
)

# 「同一プロジェクトなら不要」という **判定としての断定**。project という語自体は
# 別の文脈 (cross-project の宛先確認、--project フラグ) で正しく使われるので、
# 語の存在ではなく断定の形を突く。
FORBIDDEN_PROJECT_AXIS = (
    "same-project (同一プロジェクト) | 付けない",
    "| same-project | 付けない",
    "同一プロジェクトなら宛先確認は不要",
    "同じプロジェクトなら宛先確認は不要",
)

AXIS_STATEMENTS = (
    "プロジェクトが同じかどうかではない",
    "プロジェクトが同じかは関係しない",
    "プロジェクトが同じかどうかは判定に関係ありません",
)


def _skill_section(text: str) -> str:
    """Step 3.1 の本文。見出しが動いたら全文にフォールバックする。"""
    try:
        return text[text.index("### Step 3.1:"):text.index("### Step 3.2:")]
    except ValueError:
        return text


def find_problems(root: Path = ROOT) -> list:
    problems = []

    import dm_consent

    constants = {v for k, v in vars(dm_consent).items()
                 if k.startswith("CONSENT_SKIP_") or k.startswith("CONSENT_REQUIRED_")}
    explained = set(dm_consent.CONSENT_REASON_EXPLANATIONS)
    for missing in sorted(constants - explained):
        problems.append(
            f"判定理由 '{missing}' の 1 行説明が lib/dm_consent.py に在りません "
            f"(CONSENT_REASON_EXPLANATIONS に足してください)。説明は draft に出る"
            f"唯一の『なぜ』なので、無いと読み手は識別子だけを見ることになります。")
    for stale in sorted(explained - constants):
        problems.append(
            f"説明 '{stale}' に対応する定数がありません (定数を消したなら説明も消す)。")

    verb_src = (root / "lib" / "cmd_bus.py").read_text()
    try:
        body = verb_src[verb_src.index("def cmd_bus_consent_check"):]
        body = body[:body.index("\ndef ", 1)]
    except ValueError:
        problems.append("lib/cmd_bus.py に cmd_bus_consent_check が見つかりません。")
        body = ""

    sections = []
    for path in SKILL_COPIES:
        if not path.exists():
            problems.append(f"手順書が見つかりません: {path.relative_to(root)}")
            continue
        text = path.read_text()
        rel = path.relative_to(root)
        section = _skill_section(text)
        sections.append((rel, section))

        if "beacon bus consent-check" not in text:
            problems.append(
                f"{rel}: 判定を CLI (`beacon bus consent-check`) に問い合わせていません。"
                f"散文で規則を書き写すと、規則が動いたとき静かに食い違います。")
        for phrase in FORBIDDEN_PROJECT_AXIS:
            if phrase in text:
                problems.append(
                    f"{rel}: project 軸の判定文言が戻っています ({phrase!r})。"
                    f"規則は project を見ません — 判定軸は『宛先が別の人間か』です。")
        if not any(a in text for a in AXIS_STATEMENTS):
            problems.append(
                f"{rel}: 判定軸が明言されていません "
                f"(『プロジェクトが同じかどうかではない』旨を 1 行書いてください)。")
        for field in PROMISED_FIELDS:
            if field in text and body and f'"{field}"' not in body:
                problems.append(
                    f"{rel}: 手順書は JSON field '{field}' を説明していますが、"
                    f"cmd_bus_consent_check の出力に組み立てられていません。")

    if len(sections) > 1:
        first_rel, first = sections[0]
        for rel, section in sections[1:]:
            if section != first:
                problems.append(
                    f"手順書 3 コピーの Step 3.1 が一致しません ({first_rel} ↔ {rel})。"
                    f"片方だけ直すと、配布された側に古い判定が残ります。")
    return problems


def main() -> int:
    problems = find_problems()
    if not problems:
        print("[dm-consent-alignment] OK: 宛先確認の規則と手順書が一致しています "
              "(判定理由の説明・CLI 問い合わせ・判定軸・JSON field・3 コピー)。")
        return 0
    print("[dm-consent-alignment] 宛先確認の規則と手順書が食い違っています:\n",
          file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print("\n出典: lib/dm_consent.classify_send_consent (規則) / "
          "skills/beacon-dm-send.md Step 3.1 (手順書) / ms-160 e-6349",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
