#!/usr/bin/env python3
"""締切超過 (overdue) work items for session-start (ms-139 e-4952).

session-start が起動のたびに「期日を過ぎた作業」を surface する冪等表示。サーバ発
リマインダ (e-4953) が本命だが、その駆動が落ちても最低限の可視化を保証する二重化
(真値源はサーバ、ここは毎回再計算する冪等な表示)。

ms-142 e-5010 で列挙を ``beacon deadline due --json`` (= occupation.iter_deadline_
candidates を consume する単一経路) に一本化した。これによりサーバの overdue
リマインダとこの表示が同じ列挙を歩き、milestone の target_date / task・activity の
deadline を職種で分岐せず拾う (新職種は manifest 宣言だけで乗り、この script は
無改修)。CLI が L2 締切規則 (今日 > 締切 かつ status が terminal でない) と
terminal な Target 配下の work item 除外まで済ませて返すので、ここは整形だけ行う。

  * milestone   — target_date (開発の target)
  * task        — deadline    (開発の work item、ms-139 e-4949 で新設)
  * activity    — deadline    (営業の準備活動)

データ取得は best-effort な `beacon deadline due --json` subprocess。失敗したら
空に落として続行する。出力が空なら何も print しない (session-start は空セクションを
省く契約)。常に exit 0。

Output contract:
  * 期日超過/本日 が 0 件 -> 何も print しない。
  * 1 件以上 -> 「⏰ 締切超過 (overdue) work items:」の block を 1 つ print。
    各行: [kind] label — 期日 YYYY-MM-DD (⚠超過/⏰本日) [文脈]。古い期日順。

Call sites:
  * /beacon-session-start Skill (締切超過 surface step)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import deadline  # noqa: E402


def _beacon_json(args):
    """Run ``beacon <args> --json`` and parse stdout, or return None on any
    failure (best-effort; a missing/erroring source degrades to empty)."""
    try:
        out = subprocess.run(
            ["beacon"] + args + ["--json"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _format(rows):
    if not rows:
        return ""
    lines = ["⏰ 締切超過 (overdue) work items:"]
    label_jp = {"milestone": "MS", "task": "タスク", "activity": "活動"}
    for r in rows:
        mark = "⚠ 超過" if r["temporal"] == deadline.TRANSITION_OVERDUE else "⏰ 本日"
        ctx = f" — {r['context']}" if r.get("context") else ""
        lines.append(
            f"  [{label_jp.get(r['kind'], r['kind'])}] {r['label']} / "
            f"期日 {r['deadline']} {mark}{ctx}")
    lines.append("  → 済んだら完了/期日を延ばす/やめたら取消 で盤面から外してください。")
    return "\n".join(lines)


def main():
    # 職種横断の締切列挙は CLI に一本化 (ms-142 e-5010)。CLI が temporal 判定・
    # terminal Target 配下の除外・古い期日順ソートまで済ませて {"items": [...]} を返す。
    due = _beacon_json(["deadline", "due"])
    rows = (due or {}).get("items", []) if isinstance(due, dict) else []
    text = _format(rows)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
