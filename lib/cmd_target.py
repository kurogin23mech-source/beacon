#!/usr/bin/env python3
"""cmd_target.py — the `beacon target *` command family (ms-127 e-4852).

Extracted verbatim from commands.py (god-module split). The target family is the
profession-neutral target-class engine surface: review-request / approve /
attach-evidence / attach-disposition / reject (the attainment-review gate,
ms-119), plus create / advance / close / instances / work-item / evidence / ball
/ class-add / class-list (the descriptor-driven data targets, ms-122/124).

Depends only on commands_shared (upward) + leaf domain modules (core /
work_model / transition_approval / target_descriptor / target_engine), never on
commands.py — acyclic (SPEC 方針4). Unlike the milestone split (e-4849), this
family needed NO helper promotion: the 4 shared helpers it touches
(_release_occupation_for_transition / _print_evidence_guidance /
_spec_exists_for_ms / _spec_updated_at_for_target) were already lifted into
commands_shared during e-4849, so target references them there.

commands.py re-imports the PUBLIC handlers for dispatch + `commands.cmd_target_*`;
the family-private helpers (_apply_transition / _resolve_descriptor /
_backlog_*_message / ...) are NOT re-exported (patch them at cmd_target.<name>).

Test patch target (the e-4320 rule): a test driving a cmd_target_* handler must
patch the name in cmd_target's own namespace — each `from commands_shared import
name` binds an independent copy, so `monkeypatch.setattr(commands, "get_store",
...)` is a silent no-op on this call path. Patch `cmd_target.get_store` instead
(do NOT patch commands_shared.<name> to reach a handler — that couples every
family importing it).
"""

import os
import sys
import json

import core  # noqa: F401
import work_model  # noqa: F401
import transition_approval as _ta  # noqa: F401
import target_descriptor as _td  # noqa: F401
import target_engine as _te  # noqa: F401

from commands_shared import (  # noqa: F401
    _actor_str,
    _fire_review_due_trigger,
    _print_evidence_guidance,
    _release_occupation_for_transition,
    _resolve_session_id,
    _session_kind_is_human,
    _spec_exists_for_ms,
    _spec_exists_for_op,
    _spec_updated_at_for_target,
    get_store,
    load_project,
    save_project,
)


def _apply_transition(data: dict, target_id: str, new_state: str, *,
                      reason: str = "") -> None:
    """Execute an approved target transition on the concrete target.

    The approval primitive records the verdict but leaves execution to the
    caller (per lib/transition_approval.append_verdict). This maps the
    profession-neutral new_state back onto the target-kind-specific mutator.
    milestone completion (done / closed) → milestone_done (this codebase treats
    close as done); milestone → observing (運用移行の完了主張) →
    milestone_update(status=observing); operation retirement (closed) →
    operation_close.
    """
    kind = core._approval_target_kind(target_id)
    if kind == "milestone":
        if new_state == "observing":
            core.milestone_update(data, target_id, status="observing",
                                  reason=reason)
            _release_occupation_for_transition(data, target_id, reason="observe")
        else:
            core.milestone_done(data, target_id, reason=reason)
            _release_occupation_for_transition(data, target_id, reason="done")
    elif kind == "operation":
        core.operation_close(data, target_id)
    else:
        raise ValueError(
            f"transition apply not supported for target {target_id!r} "
            f"(kind={kind or 'unknown'})")
def _evidence_required_message(eid: str, target_id: str) -> str:
    """The approve-refusal message when a pending approval has no review-evidence
    (ms-119 / e-4205). Shares the vocabulary + command spelling with
    _print_evidence_guidance so the two cannot drift."""
    verdicts = "|".join(_ta.REVIEW_EVIDENCE_VERDICTS)
    return (
        f"Error: 承認依頼 {eid} に独立レビューの証拠が添付されていません "
        f"(ms-119 / e-4205)。承認は実装者の自己申告 intent だけでは通りません。\n"
        f"  次のいずれかを:\n"
        f"    1. 独立 judge を回して証拠を添付する — "
        f"`beacon review context --type attainment --target {target_id}` で判定を"
        f"生成し、\n"
        f"       `beacon target attach-review-evidence {eid} --verdict <{verdicts}> "
        f"--summary <text>` で記録する。\n"
        f"    2. 独立証拠なしで承認すると明示する — `--acknowledge-no-evidence` "
        f"(監査に記録が残ります)。\n")
def _backlog_undisposed_message(eid: str, target_id: str, undisposed: list) -> str:
    """The approve-refusal message when a pending approval still has UNSTARTED
    highest/high tasks without a disposition (ms-119 / e-4579). Renders the concrete
    blocking backlog + the recovery command, sharing the block layout with
    _ta.format_backlog_gap so surface and refusal cannot drift."""
    body = _ta.format_backlog_gap(undisposed, target_id=target_id,
                                  spec_updated_at=_spec_updated_at_for_target(target_id))
    return (
        f"Error: 承認依頼 {eid} は未着手の重要タスク (highest/high) の disposition が"
        f"未完です (ms-119 / e-4579)。\n"
        f"  attainment は AC (アウトカム) 軸で定義しますが、未着手の highest/high は"
        f"『必要と符号化された既知作業』であり、掃除機がある≠掃除した の穴です。\n"
        f"  各タスクに done / superseded[理由] / blocks-attainment のいずれかを付ける"
        f"まで、この承認は構造的に未完成として通りません。\n"
        f"  (どうしても skip する場合は --acknowledge-undisposed-backlog、監査に記録が"
        f"残ります。)\n"
        f"{body}\n"
    )
def _backlog_blocked_message(eid: str, target_id: str, blocking: list) -> str:
    """The approve-refusal message when ≥1 gated backlog task is disposed
    ``blocks-attainment`` (ms-119 / e-4579, #551 MUST-2). A task explicitly recorded
    as still-required-and-not-done means the attainment claim is, on the record,
    false — so approve refuses rather than passing on the disposition's mere
    existence. Shares the block layout with _ta.format_blocks_attainment."""
    body = _ta.format_blocks_attainment(blocking, target_id=target_id)
    return (
        f"Error: 承認依頼 {eid} は blocks-attainment と判定されたタスクが残っている"
        f"ため通りません (ms-119 / e-4579)。\n"
        f"  blocks-attainment は「まだ必要で未完」の明示記録なので、attainment 主張は"
        f"その記録上 false です。\n"
        f"  各タスクを完了して disposition を done に更新するか superseded[理由必須] に"
        f"切り替えるまで、この承認は構造的に通りません。\n"
        f"  (どうしても skip する場合は --acknowledge-undisposed-backlog、監査に記録が"
        f"残ります。)\n"
        f"{body}\n"
    )
def _parse_evidence() -> list:
    raw = os.environ.get("BEACON_EVIDENCE", "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]
def cmd_target_review_request():
    """Create a pending 目的達成レビュー on a target transition (e-3912).

    beacon target review-request <target-id> --new-state <state>
        [--old-state <state>] [--intent <text>] [--evidence e-1,e-2]
    """
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    new_state = os.environ.get("BEACON_NEW_STATE", "").strip()
    old_state = os.environ.get("BEACON_OLD_STATE", "").strip()
    intent = os.environ.get("BEACON_INTENT", "").strip()
    evidence = _parse_evidence()
    if not target_id or not new_state:
        print("Usage: beacon target review-request <target-id> --new-state "
              "<state> [--old-state <state>] [--intent <text>] "
              "[--evidence e-1,e-2]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        target = core._find_approval_target(data, target_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not old_state:
        old_state = target.get("status", "")
    kind = core._approval_target_kind(target_id)
    if not _ta.requires_spine_approval(kind, old_state, new_state):
        print(
            f"Error: {target_id} の {old_state} → {new_state} は目的達成レビュー"
            f"対象外です (完了主張でない routine 遷移、または sales opportunity は"
            f"既存 judge 経路)。", file=sys.stderr)
        sys.exit(1)
    eid = core.target_transition_approval_add(
        data, target_id, old_state=old_state, new_state=new_state,
        intent=intent, evidence=evidence, actor=_actor_str(),
        session_id=_resolve_session_id() or "")
    save_project(data, op={"op": "target_transition_approval_add",
                           "target_id": target_id, "entry_id": eid})
    print(f"目的達成レビュー依頼を作成: {eid}")
    print(f"  {target_id}: {old_state} -> {new_state} (人間承認待ち)")
    if intent:
        print(f"  intent: {intent}")
    if evidence:
        print(f"  evidence: {', '.join(evidence)}")
    # ms-119 e-3911 §5 AC6: weak-AC な target では gap を gentle に炙り出す
    # (hard-block しない — 依頼は既に作成済)。原典が無ければ何を満たせば done かを
    # 承認前に確認するよう促す forcing function。
    _gap = _ta.format_criteria_gap(
        _ta.assess_completion_criteria(
            has_spec=_spec_exists_for_ms(target_id) if kind == "milestone"
            else _spec_exists_for_op(target_id) if kind == "operation" else False,
            objective=target.get("objective", ""),
            acceptance=target.get("acceptance_criteria", ""),
            intent=intent),
        target_id=target_id)
    if _gap:
        print(_gap)
    # ms-119 e-4579: surface the unstarted highest/high backlog up front so the
    # approver knows the disposition table must be filled before approve will pass.
    _bgap = _ta.format_backlog_gap(
        core.unstarted_priority_tasks(target), target_id=target_id,
        spec_updated_at=_spec_updated_at_for_target(target_id))
    if _bgap:
        print(_bgap)
    _print_evidence_guidance(eid, target_id)
    print(f"  確定 (= 遷移実行): beacon target approve {eid} [--rationale <text>]")
    print(f"  却下 (= 遷移せず): beacon target reject {eid} [--rationale <text>]")
def cmd_target_approve():
    """Approve a pending target transition — records the verdict AND executes
    the transition on the target (e-3912)."""
    entry_id = os.environ.get("BEACON_ENTRY_ID", "").strip()
    rationale = os.environ.get("BEACON_RATIONALE", "").strip()
    if not entry_id:
        print("Usage: beacon target approve <entry-id> [--rationale <text>]",
              file=sys.stderr)
        sys.exit(1)
    # ms-119 / e-4006 — the 目的達成 verdict is the human's (SPEC § 方針2). An AI
    # session may assemble evidence and open the review-request, but pressing
    # approve (= confirming the target met its goal AND executing the transition)
    # is human-gated. Refuse unless an explicit human signal is present.
    if _ai_session_attainment_approve_ban_active():
        print(
            "Error: approving a 目的達成 (target attainment) verdict from an AI "
            "session is refused (ms-119 / e-4006 structural guard).\n"
            "  SPEC § 方針2: the verdict that a target met its goal is *owned* by "
            "the human, not the AI. The AI assembles evidence; the human confirms.\n"
            "  Bypass paths (= one of these makes the approval proceed):\n"
            "    1. BEACON_TARGET_APPROVE_USER_OVERRIDE=1 — explicit user opt-in "
            "for this approval.\n"
            "    2. BEACON_SESSION_KIND=human — declare the calling session is "
            "human-driven (= straight terminal use).",
            file=sys.stderr,
        )
        sys.exit(2)
    data = load_project()
    # ms-119 / e-4205 + e-4579: BOTH the "no SILENT approval without review-evidence"
    # and the "no SILENT approval while important backlog is undisposed" invariants are
    # enforced in core.target_transition_approval_approve (the choke point every
    # approval path passes), NOT here — a CLI-only guard would be bypassed by a future
    # API caller, re-opening the very hole this closes (#504 / #551 maint review). The
    # owning target is self-derived inside core (the container that holds the approval
    # entry IS the target), so the backlog gate runs on every path with no caller
    # opt-in to forget. Here we only translate the two explicit acknowledge flags and
    # render the recovery paths when core refuses.
    _ack_no_ev = os.environ.get("BEACON_ACK_NO_EVIDENCE", "") == "1"
    _ack_backlog = os.environ.get("BEACON_ACK_UNDISPOSED_BACKLOG", "") == "1"
    try:
        entry, new_state = core.target_transition_approval_approve(
            data, entry_id, rationale=rationale, actor=_actor_str(),
            gate=_approval_gate_record(), allow_no_evidence=_ack_no_ev,
            allow_undisposed_backlog=_ack_backlog)
    except core.BacklogBlockedError as e:
        _r = core.find_entry(data, entry_id)
        _tid = (_r[2]["meta"].get("target_id") if _r else "") or "<target-id>"
        sys.stderr.write(_backlog_blocked_message(entry_id, _tid, e.blocking))
        sys.exit(2)
    except core.BacklogUndisposedError as e:
        _r = core.find_entry(data, entry_id)
        _tid = (_r[2]["meta"].get("target_id") if _r else "") or "<target-id>"
        sys.stderr.write(_backlog_undisposed_message(entry_id, _tid, e.undisposed))
        sys.exit(2)
    except core.EvidenceRequiredError:
        _r = core.find_entry(data, entry_id)
        _tid = (_r[2]["meta"].get("target_id") if _r else "") or "<target-id>"
        sys.stderr.write(_evidence_required_message(entry_id, _tid))
        sys.exit(2)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    target_id = entry["meta"]["target_id"]
    # ms-119 / e-4579 (#551 MUST-2): warn when a disposition says "done" but the live
    # task never left todo (台帳 done / 実状態 todo 乖離). Non-blocking — the approval
    # already passed the gate — but surface the one-line recovery so the divergence is
    # visible, not hidden. Read the backlog off the resolved container before applying
    # the transition (statuses read the same either way; the container holds the tasks).
    _r_stale = core.find_entry(data, entry_id)
    _stale = []
    if _r_stale:
        _stale = _ta.stale_done_dispositions(
            entry, core.unstarted_priority_tasks(_r_stale[0]))
    try:
        _apply_transition(data, target_id, new_state, reason=rationale)
    except ValueError as e:
        print(f"Error applying transition: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_transition_approval_approve",
                           "entry_id": entry_id, "target_id": target_id})
    print(f"承認: {target_id} を {new_state} に遷移しました ({entry_id})")
    if rationale:
        print(f"  rationale: {rationale}")
    _stale_txt = _ta.format_stale_done_warning(_stale, target_id=target_id)
    if _stale_txt:
        print(_stale_txt)
    # ms-119 e-4006 audit: surface HOW the gate was passed so an override
    # approval is visible at the point of use, not just in the record.
    _gate = entry["meta"].get("approval_gate", {})
    if _gate:
        _es = f", evidence={_gate['evidence_source']}" if _gate.get("evidence_source") else ""
        print(f"  gate: {_gate.get('signal')} (session_kind={_gate.get('session_kind')}{_es})")
        if _gate.get("signal") == "user-override" and _gate.get("session_kind") != "human":
            print("  ⚠ AI セッションが override で承認しました — この遷移は監査対象です。")
        if _gate.get("no_evidence_ack"):
            print("  ⚠ 独立レビュー証拠なしで承認されました "
                  "(--acknowledge-no-evidence) — この遷移は監査対象です (ms-119 / e-4205)。")
        if _gate.get("backlog_check_skipped"):
            print("  ⚠ 未着手の重要タスク (highest/high) の disposition ゲートを "
                  "skip して承認されました (--acknowledge-undisposed-backlog) — "
                  "この遷移は監査対象です (ms-119 / e-4579)。")
def cmd_target_attach_evidence():
    """Attach a 目的達成 review's evidence to a pending transition-approval
    (ms-119 / e-4205).

    beacon target attach-review-evidence <entry-id> --verdict
        <attained|partial|not-attained> --summary <text> [--source <text>]

    Records the 目的達成 judge's verdict + grounds onto the pending approval, so
    `beacon target approve` no longer refuses it for lack of evidence. The verdict
    recorded here is the JUDGE's proposal; the human's own verdict is still pressed
    via `beacon target approve` (SPEC § 方針2). ``--source`` is the self-declared
    provenance — recorded verbatim (no fabricated "independent-judge" default; #504
    AX review), since independence is operational, not structurally verified."""
    entry_id = os.environ.get("BEACON_ENTRY_ID", "").strip()
    verdict = os.environ.get("BEACON_EV_VERDICT", "").strip()
    summary = os.environ.get("BEACON_EV_SUMMARY", "").strip()
    source = os.environ.get("BEACON_EV_SOURCE", "").strip()
    _verdicts = "|".join(_ta.REVIEW_EVIDENCE_VERDICTS)
    if not entry_id or not verdict or not summary:
        print(f"Usage: beacon target attach-review-evidence <entry-id> --verdict "
              f"<{_verdicts}> --summary <text> [--source <text>]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        entry = core.target_transition_approval_attach_evidence(
            data, entry_id, verdict=verdict, summary=summary,
            source=source, actor=_actor_str())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_transition_approval_attach_evidence",
                           "entry_id": entry_id})
    n = len(entry["meta"].get("review_evidence", []))
    print(f"独立レビュー証拠を添付: {entry_id} (verdict={verdict}, 計 {n} 件)")
    print(f"  承認へ: beacon target approve {entry_id} [--rationale <text>]")
def cmd_target_attach_disposition():
    """Record a disposition for one UNSTARTED highest/high backlog task on a pending
    transition-approval (ms-119 / e-4579).

    beacon target attach-disposition <entry-id> --task <task-id>
        --disposition <done|superseded|blocks-attainment> [--reason <text>] [--source <text>]

    Answers "what happened to this important, unstarted task?" so an attainment claim
    cannot silently skip it. ``superseded`` requires --reason. The disposition is the
    JUDGE / human's determination against the 原典 — not the implementer's self-report
    (task e-4579; this design lives in the task, not the ms-119 SPEC); --source records
    the self-declared provenance verbatim.

    #551 SHOULD-1: the flag is ``--disposition`` (value domain
    done|superseded|blocks-attainment). ``--verdict`` is a migration alias — its value
    domain differs from attach-review-evidence's ``--verdict``
    (attained|partial|not-attained), and the two collided. A caller who passes a
    review-evidence verdict here is steered to the right flag/values by the value-domain
    error in ``_ta.append_disposition``."""
    entry_id = os.environ.get("BEACON_ENTRY_ID", "").strip()
    task_id = os.environ.get("BEACON_DISP_TASK", "").strip()
    disposition = os.environ.get("BEACON_DISP_DISPOSITION", "").strip()
    verdict_alias = os.environ.get("BEACON_DISP_VERDICT", "").strip()
    reason = os.environ.get("BEACON_DISP_REASON", "").strip()
    source = os.environ.get("BEACON_DISP_SOURCE", "").strip()
    _verdicts = "|".join(_ta.DISPOSITION_VERDICTS)
    # #551 SHOULD-1: --disposition is canonical, --verdict is the migration alias.
    # Passing both with conflicting values is ambiguous — refuse rather than silently
    # pick one.
    if disposition and verdict_alias and disposition != verdict_alias:
        print("Error: --disposition と --verdict (旧 alias) が両方指定され値が異なります。"
              "--disposition のみを使ってください "
              f"(値域: {_verdicts})。", file=sys.stderr)
        sys.exit(1)
    verdict = disposition or verdict_alias
    if not entry_id or not task_id or not verdict:
        print(f"Usage: beacon target attach-disposition <entry-id> --task <task-id> "
              f"--disposition <{_verdicts}> [--reason <text> (superseded 時必須)] "
              f"[--source <text>]",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        entry = core.target_transition_approval_attach_disposition(
            data, entry_id, task_id=task_id, verdict=verdict, reason=reason,
            source=source, actor=_actor_str())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_transition_approval_attach_disposition",
                           "entry_id": entry_id})
    n = len(_ta.disposition_map(entry))
    print(f"disposition を記録: {entry_id} ({task_id} → {verdict}, "
          f"disposition 済 {n} タスク)")
    # Surface remaining undisposed backlog so the operator knows what's left.
    _tgt = core.find_entry(data, entry_id)
    if _tgt:
        remaining = _ta.undisposed_backlog(
            entry, core.unstarted_priority_tasks(_tgt[0]))
        _rtid = entry["meta"].get("target_id", "")
        _gap = _ta.format_backlog_gap(
            remaining, target_id=_rtid,
            spec_updated_at=_spec_updated_at_for_target(_rtid))
        if _gap:
            print(_gap)
        else:
            print(f"  全 backlog disposition 済 — 承認へ: "
                  f"beacon target approve {entry_id} [--rationale <text>]")
def cmd_target_reject():
    """Reject a pending target transition — records the verdict; the transition
    does NOT execute (e-3912)."""
    entry_id = os.environ.get("BEACON_ENTRY_ID", "").strip()
    rationale = os.environ.get("BEACON_RATIONALE", "").strip()
    if not entry_id:
        print("Usage: beacon target reject <entry-id> [--rationale <text>]",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        entry = core.target_transition_approval_reject(
            data, entry_id, rationale=rationale, actor=_actor_str())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    meta = entry["meta"]
    save_project(data, op={"op": "target_transition_approval_reject",
                           "entry_id": entry_id,
                           "target_id": meta["target_id"]})
    print(f"却下: {meta['target_id']} の {meta['old_state']} -> "
          f"{meta['new_state']} 遷移は実行されません ({entry_id})")
    if rationale:
        print(f"  rationale: {rationale}")
def _task_live_status(container: dict, task_id: str) -> str:
    """Live status of ``task_id`` anywhere under ``container`` (#551 SHOULD-2 helper).

    Walks the container's entries (nested included) and returns the matching task's
    status, or "" when not found. Used so the disposition table can print each task's
    real status next to its ledger disposition."""
    def _walk(entries):
        for e in entries or []:
            if e.get("id") == task_id:
                return e.get("status") or ""
            found = _walk(e.get("entries", []))
            if found is not None:
                return found
        return None
    return _walk(container.get("entries", [])) or ""
def cmd_target_list():
    """List target transition-approval requests (e-3912).

    beacon target list [--target <target-id>] [--pending] [--json]
    """
    target_filter = os.environ.get("BEACON_TARGET_ID", "").strip()
    pending_only = os.environ.get("BEACON_PENDING", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    rows = []
    containers = list(data.get("milestones", [])) + list(data.get("operations", []))
    for c in containers:
        for e in c.get("entries", []):
            if e.get("type") != "target-transition-approval":
                continue
            m = e.get("meta", {})
            if target_filter and m.get("target_id") != target_filter:
                continue
            if pending_only and m.get("approval_status") != "pending":
                continue
            # #551 SHOULD-2: keep the owning container so the disposition table can
            # show each task's LIVE status (台帳 disposition vs 実状態 併記).
            rows.append((e, c))
    if json_mode:
        print(json.dumps([e for e, _c in rows], ensure_ascii=False))
        return
    if not rows:
        print("(no transition-approval requests)")
        return
    for e, _container in rows:
        m = e.get("meta", {})
        st = m.get("approval_status", "?")
        icon = {"pending": "◌", "approved": "●", "rejected": "✗"}.get(st, st)
        print(f"  {icon} [{e.get('id')}] {m.get('target_id')}: "
              f"{m.get('old_state')} -> {m.get('new_state')} [{st}]")
        if m.get("intent"):
            print(f"      intent: {m.get('intent')}")
        # ms-119 e-4579 §方針4: surface the RECORDED claim (independent-review verdict
        # + disposition table) so the human approver reads the true source, not the
        # executor's DM summary. Only for pending rows (the decision is live there).
        if st == "pending":
            for rev in m.get("review_evidence", []) or []:
                _src = f" [{rev.get('source')}]" if rev.get("source") else ""
                print(f"      review: {rev.get('verdict')}{_src} — "
                      f"{rev.get('summary', '')}")
            disp = _ta.disposition_map(e)
            if disp:
                # #551 SHOULD-2: live-status map for the container's tasks, so the
                # table shows disposition (ledger) alongside the task's real status —
                # e.g. disposition=done but status=todo is a visible divergence.
                _live = {t.get("id"): (t.get("status") or "")
                         for t in core.unstarted_priority_tasks(_container)}
                # unstarted_priority_tasks only returns still-unstarted gated tasks;
                # a disposed task that has since been started/finished won't appear, so
                # fall back to walking the container for a status when missing.
                print(f"      disposition 表 ({len(disp)} タスク):")
                for tid, rec in disp.items():
                    _rsn = f" — {rec.get('reason')}" if rec.get("reason") else ""
                    _ls = _live.get(tid) or _task_live_status(_container, tid)
                    _lstxt = f" (live: {_ls})" if _ls else ""
                    print(f"        {tid}: {rec.get('verdict')}{_lstxt}{_rsn}")
def _resolve_descriptor(data: dict, kind: str) -> dict:
    """Return the descriptor for ``kind`` or print a guidance error + exit. When
    the project declares no descriptors at all, the message says so; otherwise
    it lists the kinds that ARE declared so a typo names its neighbours."""
    kind = (kind or "").strip()
    if not kind:
        print("Error: --class <kind> は必須です", file=sys.stderr)
        sys.exit(1)
    desc = _td.get_descriptor(data, kind)
    if desc is None:
        kinds = _td.descriptor_kinds(data)
        if kinds:
            print(f"Error: target-class '{kind}' の記述子がありません "
                  f"(宣言済: {', '.join(kinds)})", file=sys.stderr)
        else:
            print(f"Error: target-class '{kind}' の記述子がありません "
                  f"(このプロジェクトは target_classes を1つも宣言していません)",
                  file=sys.stderr)
        sys.exit(1)
    # ms-122 AX finding: validate the descriptor at the point of use so a
    # malformed record (unknown field type / duplicate keys / required on a
    # phase field / missing required keys) fails loudly here instead of silently
    # producing wrong results downstream. The validator was previously dead code
    # (no CLI caller); this wires it into every descriptor-driven command.
    problems = _td.validate_descriptor(desc)
    if problems:
        print(f"Error: target-class '{kind}' の記述子に問題があります:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("  project.json の target_classes を修正してください。",
              file=sys.stderr)
        sys.exit(1)
    return desc
def _parse_field_pairs() -> dict:
    """Parse BEACON_FIELDS (newline-joined ``key=value`` rows, set by bin/beacon
    from repeated ``--field key=value``) into a dict. Splits on the FIRST ``=``
    so a value may contain ``=``. Blank rows are skipped."""
    out: dict = {}
    raw = os.environ.get("BEACON_FIELDS", "")
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            print(f"Error: --field は key=value 形式です (受領: {line!r})",
                  file=sys.stderr)
            sys.exit(1)
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip()
    return out
def cmd_target_create():
    """Create a data-defined target of a given class (ms-122 e-3956).

    beacon target create --class <kind> --label <text> [--field key=value ...]
    """
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    label = os.environ.get("BEACON_LABEL", "").strip()
    if not label:
        print("Usage: beacon target create --class <kind> --label <text> "
              "[--field key=value ...]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    fields = _parse_field_pairs()
    try:
        rec = _te.create_target(data, desc, label=label, fields=fields,
                                actor=_actor_str())
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_create", "kind": kind,
                           "target_id": rec["id"]})
    phase = rec.get("phase", "")
    print(f"作成: [{rec['id']}] {label} (class={kind})")
    if phase:
        print(f"  phase: {phase}")
def cmd_target_advance():
    """Advance a data-defined target to its next (or a named) phase (e-3956).

    beacon target advance --class <kind> <target-id> [--to <phase>]
                          [--field key=value ...] [--reason <text>]
    """
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    to_phase = os.environ.get("BEACON_TO_PHASE", "").strip()
    reason = os.environ.get("BEACON_REASON", "").strip()
    if not target_id:
        print("Usage: beacon target advance --class <kind> <target-id> "
              "[--to <phase>] [--reason <text>]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    fields = _parse_field_pairs()
    try:
        rec, old, new = _te.advance_target(data, desc, target_id,
                                           to_phase=to_phase, fields=fields,
                                           actor=_actor_str(), reason=reason)
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_advance", "kind": kind,
                           "target_id": target_id})
    print(f"フェーズ進行: [{target_id}] {old} -> {new}")
    for k, v in fields.items():
        print(f"  {k} = {v}")
    if _te.is_terminal_phase(desc, new):
        print(f"  ※ '{new}' は最終フェーズです。完了は "
              f"beacon target close --class {kind} {target_id}")
        # ms-119 e-4087: reaching a terminal phase is a completion claim for a
        # data-defined target, same 節目 as a milestone going done — fire the
        # 目的達成 + 思想 review-due so descriptor targets are reviewed too. One
        # trigger file per target_id, so a later `target close` just overwrites
        # it (no double-fire accumulation).
        label = (rec.get("label") or rec.get("title") or "") if isinstance(rec, dict) else ""
        _fire_review_due_trigger(
            target_id, kind, old, new, target_title=label,
            has_spec=_spec_exists_for_descriptor_target(target_id),
            is_completion=True)
def cmd_target_close():
    """Close (mark done) a data-defined target (e-3956).

    beacon target close --class <kind> <target-id> [--reason <text>]
    """
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    reason = os.environ.get("BEACON_REASON", "").strip()
    if not target_id:
        print("Usage: beacon target close --class <kind> <target-id> "
              "[--reason <text>]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    try:
        _te.close_target(data, desc, target_id, actor=_actor_str(),
                         reason=reason)
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_close", "kind": kind,
                           "target_id": target_id})
    print(f"完了: [{target_id}] を done にしました")
    if reason:
        print(f"  reason: {reason}")
    # ms-119 e-4087: closing a data-defined target is a completion claim — the
    # same 節目 that fires 目的達成 + 思想 review-due for a milestone. Descriptor
    # targets were previously invisible to the review spine; wire them in.
    rec_closed = _te.find_target(data, desc, target_id)
    label = (rec_closed.get("label") or rec_closed.get("title") or "") \
        if isinstance(rec_closed, dict) else ""
    prev_phase = _te.current_phase(rec_closed) if isinstance(rec_closed, dict) else ""
    _fire_review_due_trigger(
        target_id, kind, prev_phase or "open", "done", target_title=label,
        has_spec=_spec_exists_for_descriptor_target(target_id),
        is_completion=True)
def cmd_target_instances():
    """List the instances of a data-defined target-class (e-3956).

    beacon target instances --class <kind> [--json]
    """
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    rows = [_te.project_target(desc, r) for r in _te.list_targets(data, desc)]
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print(f"(class '{kind}' の target はまだありません)")
        return
    for r in rows:
        icon = "●" if r["status"] == work_model.DONE_STATUS else "○"
        detail = r.get("detail", {})
        phase = f" [{detail.get('phase')}]" if detail.get("phase") else ""
        counts = ""
        if r.get("work_items_total"):
            counts = f" WorkItem {r.get('work_items_done', 0)}/{r['work_items_total']}"
        if detail.get("evidence_total"):
            counts += f" Evidence {detail['evidence_total']}"
        ball = detail.get("who_has_the_ball")
        ball_str = f" ball:{ball}" if ball else ""
        print(f"  {icon} [{r['id']}] {r['label']}{phase}{counts}{ball_str} — "
              f"{r['status']}")
        if detail.get("next_move"):
            print(f"      次の一手: {detail['next_move']}")
def cmd_target_work_item():
    """Add / complete / list a WorkItem on a data-defined target (e-4089).

    beacon target work-item add   --class <kind> <target-id> --desc <text>
    beacon target work-item done  --class <kind> <target-id> <item-id> [--reason <text>]
    beacon target work-item list  --class <kind> <target-id> [--json]
    """
    action = os.environ.get("BEACON_WI_ACTION", "").strip()
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    item_id = os.environ.get("BEACON_WI_ITEM_ID", "").strip()
    desc_text = os.environ.get("BEACON_WI_DESC", "").strip()
    reason = os.environ.get("BEACON_REASON", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not target_id:
        print("Usage: beacon target work-item <add|done|list> --class <kind> "
              "<target-id> ...", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    try:
        if action == "add":
            if not desc_text:
                # ms-124 AX review: the body is a --desc flag, not a positional
                # (unlike `beacon task add "text"`). Say so instead of the bare
                # "description は必須" the engine would raise.
                print("Error: WorkItem の説明は --desc <text> で指定してください "
                      "(positional は target-id のみ)", file=sys.stderr)
                sys.exit(1)
            item = _te.add_work_item(data, desc, target_id, desc_text,
                                     actor=_actor_str())
            save_project(data, op={"op": "target_work_item_add", "kind": kind,
                                   "target_id": target_id, "item_id": item["id"]})
            print(f"WorkItem 追加: [{item['id']}] {desc_text}")
        elif action == "done":
            item = _te.complete_work_item(data, desc, target_id, item_id,
                                          actor=_actor_str(), reason=reason)
            save_project(data, op={"op": "target_work_item_done", "kind": kind,
                                   "target_id": target_id, "item_id": item_id})
            print(f"WorkItem 完了: [{item_id}]")
        elif action == "list":
            rec = _te.find_target(data, desc, target_id)
            if rec is None:
                print(f"Error: target が見つかりません: {target_id}",
                      file=sys.stderr)
                sys.exit(1)
            items = _te.list_work_items(rec)
            if json_mode:
                print(json.dumps(items, ensure_ascii=False))
                return
            if not items:
                print(f"({target_id} に WorkItem はまだありません)")
                return
            for it in items:
                icon = "●" if work_model.is_done(it) else "○"
                print(f"  {icon} [{it.get('id')}] {it.get('description', '')}")
        else:
            print("Usage: beacon target work-item <add|done|list> ...",
                  file=sys.stderr)
            sys.exit(1)
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
def cmd_target_evidence():
    """Attach / list Evidence records on a data-defined target (e-4089).

    beacon target evidence add  --class <kind> <target-id> [--summary <text>]
                                [--for <work-item-id>]
    beacon target evidence list --class <kind> <target-id> [--json]

    ms-124 AX review: Evidence mirrors work-item's ``<add|list>`` action shape
    (rather than an implicit bare add) so the two child primitives read alike,
    and a written Evidence can be read back from the CLI.
    """
    action = os.environ.get("BEACON_EV_ACTION", "").strip()
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    summary = os.environ.get("BEACON_EV_SUMMARY", "").strip()
    linked_id = os.environ.get("BEACON_EV_FOR", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not target_id:
        print("Usage: beacon target evidence <add|list> --class <kind> "
              "<target-id> [--summary <text>] [--for <work-item-id>] [--json]",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    try:
        if action in ("", "add"):
            ev = _te.add_evidence(data, desc, target_id, summary=summary,
                                  linked_id=linked_id, actor=_actor_str())
            save_project(data, op={"op": "target_evidence_add", "kind": kind,
                                   "target_id": target_id,
                                   "evidence_id": ev["id"]})
            link = f" → {linked_id}" if linked_id else ""
            print(f"Evidence 追加: [{ev['id']}]{link}")
        elif action == "list":
            rec = _te.find_target(data, desc, target_id)
            if rec is None:
                print(f"Error: target が見つかりません: {target_id}",
                      file=sys.stderr)
                sys.exit(1)
            evs = _te.list_evidence(rec)
            if json_mode:
                print(json.dumps(evs, ensure_ascii=False))
                return
            if not evs:
                print(f"({target_id} に Evidence はまだありません)")
                return
            for ev in evs:
                link = f" → {ev['linked_id']}" if ev.get("linked_id") else ""
                print(f"  [{ev.get('id')}] {ev.get('summary', '')}{link}")
        else:
            print("Usage: beacon target evidence <add|list> ...",
                  file=sys.stderr)
            sys.exit(1)
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
def cmd_target_ball():
    """Set whose court a data-defined target's next move is in (e-4089).

    beacon target ball --class <kind> <target-id> <self|counterpart|none>
                       [--reason <text>]
    """
    kind = os.environ.get("BEACON_TARGET_CLASS", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    ball = os.environ.get("BEACON_BALL", "").strip()
    reason = os.environ.get("BEACON_REASON", "").strip()
    if not target_id or not ball:
        print("Usage: beacon target ball --class <kind> <target-id> "
              "<self|counterpart|none> [--reason <text>]", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    desc = _resolve_descriptor(data, kind)
    try:
        rec = _te.set_ball(data, desc, target_id, ball, actor=_actor_str(),
                           reason=reason)
    except _te.TargetEngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_ball", "kind": kind,
                           "target_id": target_id})
    now = rec.get(_te.BALL_KEY) or "none"
    print(f"ball 更新: [{target_id}] → {now}")
def _parse_spec_lines(env_key: str) -> list:
    """Split a newline-joined env value (set by bin/beacon from repeated flags)
    into stripped non-empty lines."""
    return [ln.strip() for ln in os.environ.get(env_key, "").split("\n")
            if ln.strip()]
def _field_from_spec(raw: str, *, required: bool) -> dict:
    """Parse a ``key:label[:type]`` field spec into a descriptor field dict.
    Type defaults to ``string``. Raises SystemExit with guidance on a malformed
    spec (an empty key can't be recovered from)."""
    parts = raw.split(":")
    key = parts[0].strip()
    if not key:
        print(f"Error: field 指定に key がありません: {raw!r} "
              "(key:label:type 形式)", file=sys.stderr)
        sys.exit(1)
    # ms-124 AX review: target-class add's --field is a SCHEMA declaration
    # (key:label:type), NOT the value assignment (key=value) that target
    # create/advance use. Reject a key that looks like the value grammar so a
    # transferred `--field counterparty=相手方` fails loudly here instead of
    # registering a garbage field key that only errors far downstream.
    if "=" in key or any(c.isspace() for c in key):
        print(f"Error: field の key '{key}' が不正です: {raw!r} は "
              "key:label:type 形式です (key=value は target create/advance 用)",
              file=sys.stderr)
        sys.exit(1)
    field = {"key": key, "label": (parts[1].strip() if len(parts) > 1 else key)}
    ftype = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "string"
    field["type"] = ftype
    if required:
        field["required"] = True
    return field
def _phase_from_spec(raw: str, *, terminal: bool) -> dict:
    """Parse a ``key:label`` phase spec into a descriptor phase dict."""
    parts = raw.split(":")
    key = parts[0].strip()
    if not key:
        print(f"Error: phase 指定に key がありません: {raw!r} (key:label 形式)",
              file=sys.stderr)
        sys.exit(1)
    phase = {"key": key, "label": (parts[1].strip() if len(parts) > 1 else key)}
    if terminal:
        phase["terminal"] = True
    return phase
def cmd_target_class_add():
    """Declare a new data-defined target-class into project.json (e-4091).

    beacon target-class add --kind <k> --label <l> --profession <p>
        --type <single-shot|persistent> --id-prefix <pfx-> --collection <coll>
        [--field key:label:type ...] [--required-field key:label:type ...]
        [--phase key:label ...] [--terminal-phase key:label ...]
    beacon target-class add --stdin        # full descriptor as JSON on stdin
    """
    data = load_project()
    if os.environ.get("BEACON_TC_STDIN", "") == "1":
        # ms-124 AX review: --stdin takes the whole descriptor as JSON; the
        # per-field flags are ignored in that path. Reject the hybrid rather
        # than silently dropping the flags the caller thought would apply.
        conflicting = [n for n, e in (
            ("--kind", "BEACON_TC_KIND"), ("--label", "BEACON_TC_LABEL"),
            ("--type", "BEACON_TC_TYPE"), ("--id-prefix", "BEACON_TC_ID_PREFIX"),
            ("--collection", "BEACON_TC_COLLECTION"),
            ("--field", "BEACON_TC_FIELDS"),
            ("--required-field", "BEACON_TC_REQUIRED_FIELDS"),
            ("--phase", "BEACON_TC_PHASES"),
            ("--terminal-phase", "BEACON_TC_TERMINAL_PHASES"))
            if os.environ.get(e, "").strip()]
        if conflicting:
            print(f"Error: --stdin 使用時は他の記述子フラグを指定できません "
                  f"(競合: {', '.join(conflicting)})", file=sys.stderr)
            sys.exit(1)
        raw = sys.stdin.read()
        try:
            desc = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Error: --stdin の JSON を解釈できません: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(desc, dict):
            print("Error: --stdin の JSON は 1 つの記述子オブジェクトである必要が"
                  "あります", file=sys.stderr)
            sys.exit(1)
    else:
        kind = os.environ.get("BEACON_TC_KIND", "").strip()
        label = os.environ.get("BEACON_TC_LABEL", "").strip()
        profession = os.environ.get("BEACON_TC_PROFESSION", "").strip() \
            or (data.get("profession") or "").strip()
        dtype = os.environ.get("BEACON_TC_TYPE", "").strip()
        id_prefix = os.environ.get("BEACON_TC_ID_PREFIX", "").strip()
        collection = os.environ.get("BEACON_TC_COLLECTION", "").strip()
        missing = [n for n, v in (("--kind", kind), ("--label", label),
                                  ("--type", dtype), ("--id-prefix", id_prefix),
                                  ("--collection", collection)) if not v]
        if missing:
            print(f"Usage: beacon target-class add --kind <k> --label <l> "
                  f"--type <single-shot|persistent> --id-prefix <pfx-> "
                  f"--collection <coll> [--profession <p>] ...\n"
                  f"  (--profession 省略時はプロジェクトの profession を継承)\n"
                  f"  未指定: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        fields = [_field_from_spec(s, required=False)
                  for s in _parse_spec_lines("BEACON_TC_FIELDS")]
        fields += [_field_from_spec(s, required=True)
                   for s in _parse_spec_lines("BEACON_TC_REQUIRED_FIELDS")]
        phases = [_phase_from_spec(s, terminal=False)
                  for s in _parse_spec_lines("BEACON_TC_PHASES")]
        phases += [_phase_from_spec(s, terminal=True)
                   for s in _parse_spec_lines("BEACON_TC_TERMINAL_PHASES")]
        desc = _td.build_descriptor(
            kind=kind, label=label, profession=profession, dtype=dtype,
            id_prefix=id_prefix, collection=collection, fields=fields,
            phases=phases)
    problems = _td.append_descriptor(data, desc)
    if problems:
        print("Error: 記述子を登録できません:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"op": "target_class_add",
                           "kind": desc.get("kind")})
    print(f"target-class 登録: [{desc.get('kind')}] {desc.get('label')} "
          f"(profession={desc.get('profession')}, type={desc.get('type')})")
    print(f"  次: beacon target create --class {desc.get('kind')} "
          f"--label <名前>")
def cmd_target_class_list():
    """List the data-defined target-classes declared in this project (e-4091).

    beacon target-class list [--json]
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    descriptors = _td.load_descriptors(data)
    problems_by_kind = _td.validate_target_classes(data)
    if json_mode:
        # ms-124 AX review: carry the validation problems in the JSON too, so a
        # consumer sees WHAT is wrong, not just a "⚠" it can't act on.
        print(json.dumps({"descriptors": descriptors,
                          "problems": problems_by_kind}, ensure_ascii=False))
        return
    if not descriptors:
        print("(このプロジェクトは target-class を宣言していません — "
              "beacon target-class add で追加できます)")
        return
    for desc in descriptors:
        if not isinstance(desc, dict):
            continue
        kind = (desc.get("kind") or "?").strip() or "?"
        phases = _td.phase_keys(desc)
        ph = f" phases: {' → '.join(phases)}" if phases else " (phase なし)"
        problems = problems_by_kind.get(kind)
        flag = " ⚠ 要修正" if problems else ""
        print(f"  [{kind}] {desc.get('label', '')} "
              f"(profession={desc.get('profession', '')}, "
              f"type={desc.get('type', '')}){ph}{flag}")
        # ms-124 AX review: show the actual problems, not just the ⚠ marker.
        for p in (problems or []):
            print(f"      - {p}")
def _spec_exists_for_descriptor_target(target_id: str) -> bool:
    """True if a spec-scoped document is attached to a data-defined (descriptor)
    target (ms-119 / e-4087).

    Descriptor targets are not milestones/operations, so they don't carry the
    ``milestone`` / ``operation`` doc field; a SPEC is linked via the generic
    ``target`` field. This lets the 思想 (philosophy) review bind at a descriptor
    target's completion when it has a written 原典, mirroring milestones."""
    if not target_id:
        return False
    try:
        docs = get_store().list_documents()
    except Exception:
        return False
    for doc in docs:
        if doc.get("scope") == "spec" and doc.get("target") == target_id:
            return True
    return False
def _ai_session_attainment_approve_ban_active() -> bool:
    """ms-119 / e-4006 — refuse AI-session self-approval of a 目的達成 verdict.

    `beacon target approve` records the *owned* verdict that a target met its
    goal and then executes the transition. SPEC § 方針2 says that verdict is the
    human's, not the AI's — but the CLI had no structural guard, so an AI session
    could assemble evidence AND press the button on the same target (the exact
    self-approval this session did to ms-119 before an independent judge caught
    it). This is the approval-side twin of ``_ai_session_merge_ban_active`` (the
    AI writes / proposes; the human confirms).

    Ban fires by default for AI sessions; bypassed only by an explicit human
    signal:

      * ``BEACON_TARGET_APPROVE_USER_OVERRIDE=1`` — user explicit opt-in (the
        user prompt authorised this specific approval).
      * ``BEACON_SESSION_KIND=human`` — non-AI session (straight terminal use).

    Both env vars are per-process (not persisted), so a stale value can't turn
    a future AI session into a self-approver. Returns True if the ban fires.
    """
    if os.environ.get("BEACON_TARGET_APPROVE_USER_OVERRIDE", "") == "1":
        return False
    return not _session_kind_is_human()
def _approval_gate_record() -> dict:
    """ms-119 / e-4006 audit (思想レビュー finding ①b) — capture HOW an approval
    passed the human-only guard.

    The env guard is a self-report, so this MS's value is not "an AI can't
    approve" but "an AI that approves cannot HIDE it". Recording which signal
    opened the gate turns an autonomous AI self-approval from something
    indistinguishable in the record into a grep-able footprint (an
    ``ai-session`` actor + ``user-override`` signal is the smoking gun).

    ``evidence_source`` is an optional, self-declared provenance string the Skill
    sets after actually running the independent judge (e.g.
    ``independent-judge:fable``) — a recorded claim, not a lock.
    """
    override = os.environ.get("BEACON_TARGET_APPROVE_USER_OVERRIDE", "") == "1"
    if override:
        signal = "user-override"
    elif _session_kind_is_human():
        signal = "human-session"
    else:
        # Defensive: the approve ban should have refused before we get here.
        signal = "ai-session-unguarded"
    return {
        "signal": signal,
        "session_kind": (os.environ.get("BEACON_SESSION_KIND", "") or "").strip() or "unset",
        "evidence_source": (os.environ.get("BEACON_EVIDENCE_SOURCE", "") or "").strip(),
    }
