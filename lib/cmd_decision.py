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


# decided_by の一級 enum (server/decision_event.py DECIDED_BY と一致させる)。
# CLI 側でも語彙外を弾いて、server 400 を待たずに早期に気付けるようにする。
_DECIDED_BY = {
    "autonomous-AI", "AI-proposed-human-chose", "human-delegated", "programmatic",
}


def _split_evidence(raw: str) -> list:
    """改行区切りの evidence link を list に。空行は落とす。"""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


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
