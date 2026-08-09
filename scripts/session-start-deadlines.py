#!/usr/bin/env python3
"""締切超過 (overdue) work items for session-start (ms-139 e-4952).

session-start が起動のたびに「期日を過ぎた作業」を surface する冪等表示。サーバ発
リマインダ (e-4953) が本命だが、その駆動が落ちても最低限の可視化を保証する二重化
(真値源はサーバ、ここは毎回再計算する冪等な表示)。

職種横断で 3 種類の work item を 1 つの L2 締切規則 (lib/deadline.py:
今日 > 締切 かつ status が terminal(done/cancelled) でない) にかける:

  * milestone   — target_date (開発の target)
  * task        — deadline    (開発の work item、ms-139 e-4949 で新設)
  * activity    — deadline    (営業の準備活動、`beacon opportunity due` 経由)

データ取得は best-effort な `beacon ... --json` subprocess。1 つでも失敗したら
その源だけ空に落として続行する。出力が空なら何も print しない (session-start は
空セクションを省く契約)。常に exit 0。

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


def _today():
    # ISO date; deadline comparison is plain string compare (YYYY-MM-DD).
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _collect_rows(status, today):
    """Build a flat list of overdue/due display rows across milestone /
    task / activity, oldest 期日 first. Reads only best-effort JSON already
    fetched or fetched here; the L2 rule lives in ``deadline``."""
    rows = []
    profession = (status or {}).get("profession", "dev") if status else "dev"

    # (1) milestone target_date — 開発 target。status --json の targets[] を使う。
    for t in (status or {}).get("targets", []) or []:
        if t.get("kind") != "milestone":
            continue
        detail = t.get("detail") or {}
        item = {"deadline": detail.get("target_date", ""), "status": t.get("status", "")}
        st = deadline.work_item_temporal_status(item, today)
        if st in (deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE):
            rows.append({
                "kind": "milestone", "label": t.get("label", t.get("id", "")),
                "deadline": item["deadline"], "temporal": st,
                "context": t.get("id", ""),
            })

    # (2) task deadline — 開発 work item。in_progress な milestone の task を引く。
    for t in (status or {}).get("targets", []) or []:
        if t.get("kind") != "milestone" or t.get("status") != "in_progress":
            continue
        tl = _beacon_json(["task", "list", "-m", t.get("id", "")])
        entries = (tl or {}).get("entries", []) if isinstance(tl, dict) else []
        for e in entries:
            if e.get("type") != "task":
                continue
            st = deadline.work_item_temporal_status(e, today)
            if st in (deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE):
                rows.append({
                    "kind": "task", "label": e.get("description", e.get("id", "")),
                    "deadline": deadline.deadline_of(e), "temporal": st,
                    "context": f"{t.get('id', '')} / {e.get('id', '')}",
                })

    # (3) activity deadline — 営業の準備活動。sales project のみ。
    if profession != "dev":
        due = _beacon_json(["opportunity", "due"])
        for a in (due or {}).get("activities", []) if isinstance(due, dict) else []:
            rows.append({
                "kind": "activity", "label": a.get("description", a.get("act_id", "")),
                "deadline": a.get("deadline", ""), "temporal": a.get("activity_status", ""),
                "context": f"{a.get('opp_title', '')} ({a.get('opp_id', '')})",
            })

    rows.sort(key=lambda r: r.get("deadline") or "")
    return rows


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
    today = _today()
    status = _beacon_json(["status"])
    rows = _collect_rows(status, today)
    text = _format(rows)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
