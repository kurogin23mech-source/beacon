#!/usr/bin/env python3
"""cmd_decision.py — the `beacon decision *` command family (ms-154 e-5594).

The CLI path for recording a decision-arm event to the server's unified
``decision_events`` stream. Used by the /beacon-log log-time backstop (= AI
adversarially self-reports the non-trivial decisions a commit embodied) and, in
general, by any caller that wants to leave an auditable "誰が/なぜ/何を根拠に"
record without going through a dedicated mutating route.

The decision stream is server-side (cloud), so these verbs are cloud-only: in
local mode there is no stream to append to, and the command says so and exits
cleanly (= it never hard-fails a caller like the log Skill). Depends only on
commands_shared (upward) + leaf modules — acyclic (SPEC 方針4).
"""

import json
import os
import sys

from commands_shared import _is_cloud_mode, _get_api_client

# ms-154 e-5652: decided_by 語彙は decision_vocab.DECIDED_BY が単一ソース。CLI 側でも
# 語彙外を弾いて server 400 を待たず早期に気付けるようにするが、語彙の定義は server と
# 共有する (旧: 二重定義していた = 片方だけ増やすと silent に割れる §2 SSoT 違反)。
from decision_vocab import DECIDED_BY as _DECIDED_BY  # noqa: F401


def _split_evidence(raw: str) -> list:
    """改行区切りの evidence link を list に。空行は落とす。"""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_limit(raw: str) -> int:
    """--limit を正の整数に。未指定は 100。不正値は exit 1 (ms-154 e-5649).

    旧実装は ``int(limit) if limit.isdigit() else 100`` で、``--limit abc`` が
    silently 100 に fallback していた (= 要求とは別の page 数を返しながら成功に
    見える silent 破壊、audit する側は検知不能)。非整数 / 非正は明示エラーで
    落とす (= argparse の type=int が返す拒否と対称)。
    """
    if not raw:
        return 100
    try:
        value = int(raw)
    except ValueError:
        print(f"Error: --limit must be an integer (got {raw!r})", file=sys.stderr)
        sys.exit(1)
    if value < 1:
        print(f"Error: --limit must be a positive integer (got {value})",
              file=sys.stderr)
        sys.exit(1)
    return value


def cmd_decision_record():
    kind = os.environ.get("BEACON_DECISION_KIND", "").strip() or "log-backstop"
    what = os.environ.get("BEACON_DECISION_WHAT", "").strip()
    rationale = os.environ.get("BEACON_DECISION_RATIONALE", "").strip()
    decided_by = os.environ.get("BEACON_DECISION_DECIDED_BY", "").strip() or "autonomous-AI"
    evidence = _split_evidence(os.environ.get("BEACON_DECISION_EVIDENCE", ""))
    related_task = os.environ.get("BEACON_DECISION_RELATED_TASK", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not what:
        print("Error: --what (the decision made) is required", file=sys.stderr)
        sys.exit(1)
    if decided_by not in _DECIDED_BY:
        print(f"Error: --decided-by must be one of {sorted(_DECIDED_BY)}",
              file=sys.stderr)
        sys.exit(1)
    # decided_by を立てるなら evidence 必須 (= server の schema 不変条件を CLI で先取り)。
    if not evidence:
        print("Error: --evidence is required (a first-class decision must link "
              "its grounds; give a commit hash / file:line / url)", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        # local mode には決定ストリームが無い。呼び出し側 (log Skill 等) を壊さない
        # よう、明示メッセージを出して正常終了する。
        print("decision stream は cloud プロジェクトのみ (local mode では記録しません)")
        return

    payload = {
        "kind": kind,
        "decision": what,
        "decided_by": decided_by,
        "evidence": evidence,
    }
    if rationale:
        payload["rationale"] = rationale
    if related_task:
        payload["related"] = {"task_id": related_task}

    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        result = client.record_decision(project_id, payload)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: failed to record decision: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        did = result.get("decision_id", "?") if isinstance(result, dict) else "?"
        print(f"Decision recorded [{did}]: {kind} — {what[:60]}")


def cmd_decision_list():
    """List decisions from the unified stream (ms-154 e-5595).

    The read side for the independent-verification path (別 AI が宣言 rationale を
    実コードに照合する) and for auditing. cloud-only.
    """
    kind = os.environ.get("BEACON_DECISION_KIND", "").strip()
    limit = _parse_limit(os.environ.get("BEACON_DECISION_LIMIT", "").strip())
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not _is_cloud_mode():
        if json_mode:
            print(json.dumps({"decisions": [], "count": 0}, ensure_ascii=False))
        else:
            print("decision stream は cloud プロジェクトのみ (local mode では記録なし)")
        return

    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        result = client.list_decisions(project_id, kind=kind, limit=limit)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: failed to list decisions: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = result.get("decisions", []) if isinstance(result, dict) else []
    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return
    if not rows:
        print("(決定なし)")
        return
    for r in rows:
        did = r.get("decision_id", "?")
        k = r.get("kind", "?")
        what = r.get("decision", "")
        by = r.get("decided_by") or "?"
        ev = r.get("evidence") or []
        print(f"  [{did}] {k} / {by}: {what}")
        if r.get("rationale"):
            print(f"      なぜ: {r['rationale']}")
        if ev:
            print(f"      根拠: {', '.join(ev)}")


def cmd_decision_derive():
    """Derive decisions from existing artifacts that already carry the "why"
    (ms-166 e-5972). Currently: **PR intent** — every recorded PR that declares an
    intent becomes a ``pr-intent`` decision, so a change's "why" is a DERIVED product
    of what was already written (no separate ``beacon decision record``). Idempotent:
    PRs already on the arm are skipped, so re-running never duplicates. cloud-only.

    (task done_reason は task-done seam、採否は review-adjudication、完遂 verdict は
    completion-verdict で既に捕獲済み。commit rationale の導出は別 task = 粒度と
    正規化規則が別物。) 会話の純粋内省判断は artifact に書かれていないので導出対象外。"""
    import decision_derive
    from commands_shared import load_project

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # dry-run 既定 (deliverable backfill と同じ安全側): 既存 PR は数百件ありうるので、
    # --apply を明示しない限り「何件導出するか」を報告するだけで書き込まない。
    apply = os.environ.get("BEACON_APPLY", "") == "1"
    if not _is_cloud_mode():
        print("decision derive は cloud プロジェクトのみ "
              "(local mode では決定ストリームが無い)")
        return
    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        data = load_project()
        artifacts = list(decision_derive.iter_pr_intent_artifacts(data))
        # 既存 pr-intent decision を読んで covered set を作る (dedup = 冪等)。
        try:
            res = client.list_decisions(
                project_id, kind=decision_derive.DERIVED_PR_INTENT_KIND, limit=2000)
            existing = res.get("decisions", []) if isinstance(res, dict) else []
        except Exception:
            existing = []
        covered = decision_derive.covered_pr_numbers(existing)
        from cmd_pr import _decided_by_for_review  # decided_by の単一真実源
        decided_by = _decided_by_for_review()
        to_derive = [
            (pr_number, title, intent)
            for (pr_number, title, intent) in artifacts
            if str(pr_number) not in covered
        ]
        derived = 0
        if apply:
            for pr_number, title, intent in to_derive:
                payload = decision_derive.build_pr_intent_decision(
                    pr_number, title, intent, decided_by=decided_by)
                if payload is None:
                    continue
                try:
                    client.record_decision(project_id, payload)
                    derived += 1
                except Exception as exc:
                    print(f"  ⚠ pr:{pr_number} の導出に失敗: {exc}", file=sys.stderr)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: failed to derive decisions: {exc}", file=sys.stderr)
        sys.exit(1)

    pending = len(to_derive)
    skipped = len(artifacts) - pending
    if json_mode:
        print(json.dumps({"apply": apply, "derived": derived, "pending": pending,
                          "already_covered": skipped,
                          "pr_intent_artifacts": len(artifacts)}, ensure_ascii=False))
    elif apply:
        print(f"decision derive: {derived} 件を導出 / {skipped} 件は既存済み "
              f"(intent を持つ PR {len(artifacts)} 件が対象)")
    else:
        print(f"decision derive (dry-run): {pending} 件が導出対象 / {skipped} 件は既存済み "
              f"(intent を持つ PR {len(artifacts)} 件)。実行するには --apply を付けてください。")
