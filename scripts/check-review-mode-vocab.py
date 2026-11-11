#!/usr/bin/env python3
"""レビュー mode の語彙が層をまたいでズレないことの照合 (ms-119 e-4204)。

**何が起きていた**: mode の語彙は kernel (`lib/review_spine.py` の
`ARTIFACT_MODES`) が持つが、各 judge instrument (`skills/*/SKILL.md`) が
**自分でも語彙を並べていた**。そして実際にズレていた —— philosophy / 保守性の
instrument は mode を「diff | full」の 2 つと書いており、`full` は **kernel に
存在しない名前**、kernel が持つ `whole-target` / `decision-audit` は
instrument のどこにも無かった。

実害は silent: target の完了節目では kernel が `whole-target` を **自動で**
設定するので、その bundle を受けた judge には依るべき指針が無く、出力スキーマは
存在しない値 (`full`) を書かせていた。judge は何らかの判定を返すので、
「指針が無いまま判定した」ことが誰にも見えない。

**この検査が見るもの**: instrument が mode として名指す語が、kernel の
`ARTIFACT_MODES` の **部分集合** であること。instrument が kernel より多くを
名乗ったら赤。

**この検査が見ないもの** (名乗りを実装に合わせる):
- instrument が kernel の mode を **全部** 説明しているかは見ない。対象外の mode を
  「来たら止める」と書くのも正当な扱いなので、網羅は要求しない。
- 説明の中身 (その mode で正しい問いを立てているか) は見ない。これは散文の質で、
  機械では測れない。
- 語が散文のどこに出るかは区別しない (後述の抽出規則の限界)。

**抽出規則**: ``mode`` の文脈でバッククォートに囲まれた語、``**<語> mode**``
の形、および **出力スキーマ例の ``"mode": "..."`` 行の値**。
``ARTIFACT_MODES`` という識別子そのものや、``scripts/`` への参照は語彙の
名乗りではないので除く。

3 つ目 (JSON の値) は後から足した。初版は散文のバッククォート語だけを見ており、
instrument が judge に書かせる **出力スキーマ例**は JSON の二重引用符なので
バッククォートが無く、まるごと死角に入っていた。そしてまさにその死角に
``"mode": "diff | full"`` が残っていた —— この検査が消すために作られた
``full`` が、この検査の緑のまま ``maintainability-review`` に居残っていた
(独立保守性レビュー high, 2026-11-10)。**緑のガードは信頼されるので、
見えない形で残る名乗りは無ガードより悪い。**

JSON の値は列挙なので ``|`` と ``/`` で割り、**割った断片がまるごと mode の形
(``[a-z][a-z-]{2,}``) のときだけ**語彙として数える。断片に説明文が混じる形
(例 ``"bundle の mode の値をそのまま (diff / whole-target / ...)"``) で
``bundle`` のような非 mode 語を拾わないための形。除外リスト (``_NOT_A_MODE``)
を育てて黙らせる方向には **しない** —— あれは育つほど本物のズレを隠す。

plain script (pytest 非依存) — `scripts/ci-strict-drift-guards.sh` から呼ばれる。
終了コード: 揃っていれば 0、ズレていれば 1。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import review_spine  # noqa: E402

#: 語彙を名乗る instrument。review-type.json を持つ judge-run の instrument 全部。
INSTRUMENTS = ("ax-review", "maintainability-review", "philosophy-review")

#: ``mode`` の近くでバッククォートに囲まれた語 / ``**<語> mode**`` の形。
_BACKTICKED = re.compile(r"`([a-z][a-z-]{2,})`")
_BOLD_MODE = re.compile(r"\*\*`?([a-z][a-z-]{2,})`?\s*mode\*\*")

#: 出力スキーマ例の ``"mode": "..."`` 行。JSON の値はバッククォートが無いので
#: ``_BACKTICKED`` では見えない (初版の死角 — docstring の抽出規則を参照)。
_JSON_MODE_VALUE = re.compile(r'"mode"\s*:\s*"([^"]*)"')

#: JSON の値を列挙として割る区切り。
_MODE_VALUE_SPLIT = re.compile(r"[|/]")

#: 割った断片が **まるごと** これに一致したときだけ mode の名乗りと数える
#: (説明文が混じる断片から非 mode 語を拾わないため)。
_MODE_SHAPED = re.compile(r"[a-z][a-z-]{2,}")

#: mode の語彙ではないが、同じ形で現れる語 (除外しないと偽の赤になる)。
#: ここに足すときは「本当に mode 名ではない」ことを確かめること — 安易に足すと
#: 本物のズレを除外して偽の緑になる。
_NOT_A_MODE = frozenset({
    # ``mode`` は **フィールド名** (bundle の `mode` キー) であって mode 値ではない。
    "mode",
    "artifact-modes", "review-spine", "check-review-mode-vocab",
    "beacon-review-run", "application-map", "project-vision",
    "principles", "whole-target-artifact",
})


def _declared_modes(path: str) -> set:
    """instrument が mode として名指す語を集める。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    found = set(_BOLD_MODE.findall(text))
    # ``mode`` という語を含む行のバッククォート語だけを見る (散文全体から拾うと
    # 無関係な識別子が大量に混ざり、除外リストが育って本物のズレを隠す)。
    for line in text.splitlines():
        if "mode" not in line:
            continue
        found.update(_BACKTICKED.findall(line))
    # 出力スキーマ例の ``"mode": "..."`` の値 (JSON なのでバッククォートが無い)。
    for value in _JSON_MODE_VALUE.findall(text):
        for seg in _MODE_VALUE_SPLIT.split(value):
            seg = seg.strip().strip("()`'\"").strip()
            if _MODE_SHAPED.fullmatch(seg):
                found.add(seg)
    return {m for m in found if m not in _NOT_A_MODE}


def main() -> int:
    kernel = set(review_spine.ARTIFACT_MODES)
    if not kernel:
        print("[review-mode-vocab] NG: kernel の ARTIFACT_MODES が空 "
              "— 語彙の単一定義が消えている", file=sys.stderr)
        return 1

    bad = 0
    for name in INSTRUMENTS:
        path = os.path.join(ROOT, "skills", name, "SKILL.md")
        if not os.path.isfile(path):
            print(f"[review-mode-vocab] NG: {name} の SKILL.md が無い "
                  f"(instrument の一覧が実態とズレている)", file=sys.stderr)
            bad += 1
            continue
        declared = _declared_modes(path)
        unknown = sorted(declared - kernel)
        if unknown:
            print(f"[review-mode-vocab] NG: {name} が kernel に無い mode を"
                  f"名乗っています: {', '.join(unknown)}", file=sys.stderr)
            print(f"  kernel (lib/review_spine.py ARTIFACT_MODES): "
                  f"{', '.join(sorted(kernel))}", file=sys.stderr)
            print("  層をまたいで語彙を 2 回定義すると、片方だけ増えて黙ってズレます "
                  "(ms-119 e-4204: 実際に instrument が kernel に無い `full` を "
                  "書いており、kernel が自動設定する `whole-target` の指針が"
                  "どこにも無かった)。instrument は kernel の語を参照してください。",
                  file=sys.stderr)
            bad += 1

    if bad:
        return 1
    print(f"[review-mode-vocab] OK: instrument {len(INSTRUMENTS)} 件が名乗る mode は"
          f"すべて kernel の語彙 ({', '.join(sorted(kernel))}) の中です。")
    print("  **この緑は instrument の説明が正しいことを主張しません** — 語彙の"
          "集合が kernel の部分集合であることだけを見ています (網羅も中身も"
          "見ていない。詳細と限界は docstring)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
