#!/usr/bin/env python3
"""cmd_acquisition.py — the `beacon acquisition *` command family (ms-127 e-4839).

Extracted verbatim from commands.py (god-module split). The acquisition
(攻略リスト = target-account outreach list) family builds and drives a table-doc
of prospect accounts. Depends only on commands_shared (upward) + leaf domain
modules (core / work_model / store / table_doc / table_type), never on
commands.py — acyclic (SPEC 方針4). This split was unblocked by the doc-family
split (e-4831), which promoted the table-doc helpers (_persist_table_doc /
_load_table_model / _write_table_model) into commands_shared; both the doc
handlers and these acquisition handlers now share them there.

commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`; the
family-private helpers (_stamp_attack_list_contact / _fire_attack_list_reply_trigger)
are NOT re-exported (patch them at cmd_acquisition.<name>).

Test patch target (the e-4320 rule): a test driving a cmd_acquisition_* handler
must patch EVERY name the handler resolves in cmd_acquisition's own namespace —
each `from commands_shared import name` binds an independent copy, so
`monkeypatch.setattr(commands, "get_store", ...)` is a silent no-op on this call
path. Patch `cmd_acquisition.get_store` (or commands_shared.<name> when testing a
helper directly) instead.
"""

import os
import sys
import json

import core  # noqa: F401
import work_model  # noqa: F401
import store  # noqa: F401

from commands_shared import (  # noqa: F401
    load_project,
    save_project,
    get_store,
    _actor_str,
    _now_iso,
    _today_iso,
    _parse_number,
    _ACKNOWLEDGED_REASON,
    _gate_target_class,
    _ai_session_direct_completion_ban_active,
    _self_close_ban_refuse,
    _get_triggers_dir,
    _refuse_if_bus_origin,
    _read_bus_budget,
    _validate_link_target_exists,
    _load_table_model,
    _write_table_model,
    _persist_table_doc,
)


def cmd_acquisition_add():
    import sales_entities
    title = os.environ.get("BEACON_ACQ_TITLE", "")
    description = os.environ.get("BEACON_ACQ_DESCRIPTION", "")
    assignee = os.environ.get("BEACON_ACQ_ASSIGNEE", "")
    data = load_project()
    _gate_target_class(data, "acquisition")
    try:
        acq_id = sales_entities.acquisition_add(
            data, title, description=description, assignee=assignee,
            created_at=_now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added acquisition {acq_id}: {title}")
    if description:
        print(f"  目標 / メモ: {description}")
    print(f"  次: `beacon acquisition status {acq_id} in_progress` で着手 / "
          f"`beacon acquisition list` で一覧")


def cmd_acquisition_list():
    import json as _json
    data = load_project()
    # e-4507 follow-up (独立 AX レビュー #558): a `list` command is an inventory of
    # record — show ALL acquisitions including 打ち切った (cancelled) ones (with their
    # cancelled status visible), matching the sibling `beacon account list`. Hiding
    # tombstoned rows here would be a silent omission (an AI can't see/discover a
    # 打ち切った施策). The curated Web board is the place that hides terminal items.
    acqs = data.get("acquisitions", [])
    if os.environ.get("BEACON_JSON") == "1":
        print(_json.dumps(acqs, ensure_ascii=False))
        return
    if not acqs:
        print('(顧客獲得ターゲットはまだありません — `beacon acquisition add "<title>"`)')
        return
    for a in acqs:
        items = a.get("work_items", [])
        done = sum(1 for w in items if w.get("status") == "done")
        cnt = f" [{done}/{len(items)}]" if items else ""
        label = a.get("label") or a.get("title", "")
        print(f"{a.get('id')}  {a.get('status',''):<12} {label}{cnt}")
        desc = a.get("description", "")
        if desc:
            print(f"    目標: {desc}")


def cmd_acquisition_status():
    import sales_entities
    import target_state as _tstate
    acq_id = os.environ.get("BEACON_ACQ_ID", "")
    status = os.environ.get("BEACON_ACQ_STATUS", "")
    # ms-142 T3 / e-5158 — reaching a completion terminal is a completion claim;
    # give this previously-ungated class the same anti-self-close gate every
    # target-class must have (Scope B: the lightweight structural ban). The
    # completion terminal is DERIVED from the declared state model's routed_states
    # (not a hardcoded "done") so a new terminal added to the model is auto-gated —
    # single source of truth (T3 AX+maint review consensus). The cancel status is
    # EXCLUDED: soft-cancel (`beacon acquisition delete`) is an abandon path, not a
    # completion claim, so it is deliberately ungated (T3 maint review §1).
    _completion_terminals = frozenset(
        _tstate.state_model_for(None, "acquisition")["routed_states"]
    ) - {work_model.CANCELLED_STATUS}
    if status in _completion_terminals and _ai_session_direct_completion_ban_active():
        _self_close_ban_refuse(
            acq_id, f"marking {acq_id} {status}",
            f"beacon acquisition status {acq_id} {status}")
    data = load_project()
    try:
        sales_entities.acquisition_set_status(data, acq_id, status,
                                              at=_now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # ms-163 e-5879/5880: reaching a completion terminal (done 等、cancel は除外済) is a
    # 完遂 — fire the generic seam so acquisition completion records a 完遂 decision like
    # every other target-class. acquisition は非 aggregatable (find_target が引かない) ので
    # 最小 dict を渡す。deliverable slot 無しなので capture は no-op、decision のみ記録。
    if status in _completion_terminals:
        import target_completion
        target_completion.on_target_completion(data, {"id": acq_id}, verdict=status)
    save_project(data)
    print(f"{acq_id} → {status}")


def cmd_acquisition_delete():
    """Soft-cancel (打ち切り) a 顧客獲得ターゲット — ms-132 e-4507. Discontinuing a
    施策 is expressed as deletion, not a lifecycle status. The record is
    tombstoned (status → cancelled, audit trail kept), never physically removed.
    Env: BEACON_ACQ_ID, BEACON_CANCEL_REASON.
    """
    import sales_entities
    acq_id = os.environ.get("BEACON_ACQ_ID", "")
    reason = os.environ.get("BEACON_CANCEL_REASON", "")
    # e-4507 follow-up (#1 DRY): the acknowledged-no-reason sentinel has ONE
    # definition (_ACKNOWLEDGED_REASON). Callers signal a deliberate no-reason
    # waiver with BEACON_ACKNOWLEDGE=1 and let this path stamp the sentinel,
    # rather than each entrypoint hardcoding the literal string.
    if not reason and os.environ.get("BEACON_ACKNOWLEDGE") == "1":
        reason = _ACKNOWLEDGED_REASON
    if not acq_id:
        print("Usage: beacon acquisition delete <acq-id> "
              "(--reason <text> | --acknowledge)",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        sales_entities.acquisition_cancel(data, acq_id, reason=reason,
                                          at=_now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Cancelled acquisition {acq_id}" + (f": {reason}" if reason else ""))


def cmd_acquisition_attach_list():
    """Create an attack-list (ms-131 table-doc) linked to an Acquisition (ms-132
    e-4501, AC1/AC2).

    The list's columns are the canonical attack-list schema (対象顧客=acc 参照 /
    打診フェーズ / 最終接触日 / メモ). Creation, sales-Target existence validation
    and frontmatter linkage all go through the *single* table-doc create path
    (``cmd_doc_table_create``) — the attach verb only bakes the columns and points
    ``--target`` at the施策, so this path can never diverge from the primitive it
    builds on (方針2: the list IS a table-doc).
    Env: BEACON_ACQ_ID, BEACON_ACQ_LIST_TITLE, BEACON_ACQ_LIST_PHASES (optional,
    comma-separated funnel override for e-4502), BEACON_JSON.
    """
    import attack_list
    import table_doc
    _USAGE = ('Usage: beacon acquisition attack-list <acq-id> "<title>" '
              '[--phases a,b,c] [--json]')
    acq_id = os.environ.get("BEACON_ACQ_ID", "")
    title = os.environ.get("BEACON_ACQ_LIST_TITLE", "")
    phases_raw = os.environ.get("BEACON_ACQ_LIST_PHASES", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not acq_id:
        print("Error: acq-id required\n" + _USAGE
              + "\n  (`beacon acquisition list` で acq- ID を確認)", file=sys.stderr)
        sys.exit(1)
    if not title:
        print("Error: リストのタイトルが必要です\n" + _USAGE, file=sys.stderr)
        sys.exit(1)
    if _refuse_if_bus_origin("acquisition_attach_list",
                             {"acq_id": acq_id, "title": title[:80]}):
        sys.exit(1)
    # Validate the施策 exists up-front so the error names the acquisition (the
    # shared create path re-checks, but this keeps the message on the acq-).
    _validate_link_target_exists(acq_id)
    # Phase funnel resolution (ms-132 e-4502): an explicit --phases override wins;
    # otherwise bake the project's *configured* prospect funnel so a company that
    # edited `beacon phase prospect ...` gets its own vocabulary; falling back to
    # the shipped default when unset.
    phases = [p.strip() for p in phases_raw.split(",") if p.strip()]
    if not phases:
        import sales_entities
        configured = sales_entities.prospect_phases(load_project())
        phases = [p.get("name") for p in configured if p.get("name")]
    try:
        columns = attack_list.attack_list_columns(phases or None)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    # Create through the shared table-doc path (方針2: the list IS a table-doc).
    # scope=memo mirrors the sales table-doc convention; target=acq- links the list
    # so `doc list --target` / `acquisition attack-lists` surface it.
    doc_id, model = _persist_table_doc(
        title=title, columns=columns, scope="memo", target=acq_id)

    if json_mode:
        # Acquisition-scoped shape (not the bare table-doc JSON): the caller asked
        # `acquisition attach-list`, so the output names the acquisition it linked
        # to — structurally comparable to `acquisition attack-lists` (AX review).
        print(json.dumps({"acquisition": acq_id, "doc_id": doc_id, "title": title,
                          "format": table_doc.TABLE_FORMAT,
                          "columns": model.get("columns", [])}, ensure_ascii=False))
    else:
        print(f"Attached attack-list {doc_id} to {acq_id}: {title}")


def cmd_acquisition_lists():
    """List the attack-lists (table-docs) linked to an Acquisition (ms-132
    e-4501, AC3).

    Surfaces each linked list's title, active row count and per-phase breakdown so
    a施策's outreach state reads at a glance. Only table-docs whose columns match
    the attack-list schema are shown (a施策 may carry other linked docs).
    Env: BEACON_ACQ_ID, BEACON_JSON.
    """
    import attack_list
    import table_doc
    import work_model
    acq_id = os.environ.get("BEACON_ACQ_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not acq_id:
        print("Error: acq-id required\nUsage: beacon acquisition attack-lists "
              "<acq-id> [--json]\n  (`beacon acquisition list` で acq- ID を確認)",
              file=sys.stderr)
        sys.exit(1)
    _validate_link_target_exists(acq_id)

    store = get_store()
    linked = [d for d in store.list_documents()
              if d.get("status") != "cancelled"
              and work_model.doc_target(d) == acq_id]

    results = []
    for meta in linked:
        doc_id = meta.get("doc_id")
        # list_documents carries frontmatter metadata but not necessarily the body;
        # fetch the full doc to parse the table payload.
        full = store.get_document(doc_id) or {}
        content = full.get("content", "")
        if not table_doc.is_table_content(content):
            continue
        try:
            model = table_doc.parse_table(content)
        except table_doc.TableDocError:
            continue
        if not attack_list.is_attack_list(model.get("columns", [])):
            continue
        rows = table_doc.active_rows(model)
        phase_counts = {}
        for row in rows:
            phase = row.get("cells", {}).get(attack_list.COL_PHASE) or "(未設定)"
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        results.append({
            "doc_id": doc_id,
            "title": full.get("title") or meta.get("title", ""),
            "row_count": len(rows),
            "phase_counts": phase_counts,
        })

    if json_mode:
        print(json.dumps({"acquisition": acq_id, "lists": results},
                         ensure_ascii=False))
        return
    if not results:
        print(f"{acq_id} に紐づくアタックリストはまだありません "
              f'(`beacon acquisition attack-list {acq_id} "<title>"` で作成)')
        return
    for r in results:
        print(f"{r['doc_id']}  {r['title']}  ({r['row_count']} 件)")
        if r["phase_counts"]:
            brk = " / ".join(f"{k}:{v}" for k, v in r["phase_counts"].items())
            print(f"    {brk}")


def cmd_acquisition_attack_list_fill():
    """Bulk-register未接触 Accounts matching a condition query into an attack-list
    as prospect rows (ms-132 e-4503).

    Query = account phase (default 未接触 = 生リスト) + optional assignee /
    name-substring. Each matched Account becomes one row (Account ref + the list's
    own entry phase). Dedup: an Account already present as a row is skipped, so
    re-running never double-registers (AC3). ``--dry-run`` previews
    matched/to-add/skipped without writing.
    Env: BEACON_DOC_ID, BEACON_FILL_PHASE, BEACON_FILL_ASSIGNEE, BEACON_FILL_NAME,
    BEACON_FILL_LIMIT, BEACON_DRY_RUN, BEACON_JSON.
    """
    import attack_list
    import table_doc
    import table_type
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    _USAGE = ("Usage: beacon acquisition attack-list-fill <attack-list-doc-id> "
              "[--account-phase <name>] [--assignee <user>] [--name-contains <s>] "
              "[--limit N (登録順で先頭N件)] [--dry-run] [--json]")
    if not doc_id:
        print("Error: doc-id required\n" + _USAGE
              + "\n  (`beacon acquisition attack-lists <acq>` でリストの doc-id を確認)",
              file=sys.stderr)
        sys.exit(1)
    if _refuse_if_bus_origin("acquisition_attack_list_fill", {"doc_id": doc_id}):
        sys.exit(1)
    data = load_project()
    # Default the filter to the project's *configured* first account phase (=
    # 生リスト entry, 未接触 by default but a project may have renamed it) so
    # omitting --account-phase never silently matches zero (AX review PR #548).
    phase_filter = (os.environ.get("BEACON_FILL_PHASE", "")
                    or sales_entities.default_account_phase(data))
    assignee_filter = os.environ.get("BEACON_FILL_ASSIGNEE", "") or None
    name_contains = os.environ.get("BEACON_FILL_NAME", "") or None
    limit_raw = os.environ.get("BEACON_FILL_LIMIT", "")
    limit = int(_parse_number(limit_raw, "--limit")) if limit_raw.strip() else None
    dry_run = os.environ.get("BEACON_DRY_RUN", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    content, title, model = _load_table_model(doc_id)
    if not attack_list.is_attack_list(model.get("columns", [])):
        print(f"Error: {doc_id} はアタックリスト (attack-list) ではありません "
              f"(対象顧客 ref + 打診フェーズ enum の列が要ります)", file=sys.stderr)
        sys.exit(1)
    # New prospects start at the list's own entry phase (the funnel may be
    # customized per company, so read it from the list, not the global default).
    phase_col = next((c for c in model.get("columns", [])
                      if c.get("key") == attack_list.COL_PHASE), {})
    entry_phase = (phase_col.get("values")
                   or [attack_list.INITIAL_PROSPECT_PHASE])[0]

    matched = sales_entities.filter_accounts(
        data, phase=phase_filter, assignee=assignee_filter,
        name_contains=name_contains)
    if limit is not None:
        matched = matched[:limit]
    # Dedup key = the account cell of each live row. Drop None so a row missing
    # the account cell can't make a real Account (whose id is never None) collide.
    existing = {r.get("cells", {}).get(attack_list.COL_ACCOUNT)
                for r in table_doc.active_rows(model)}
    existing.discard(None)
    to_add = [a for a in matched if a.get("id") not in existing]
    skipped_ids = [a.get("id") for a in matched if a.get("id") in existing]
    target_ids = [a.get("id") for a in to_add]

    if not dry_run and to_add:
        table_type.install()
        at = _now_iso()
        actor = _actor_str()
        for a in to_add:
            table_doc.add_row(model, {attack_list.COL_ACCOUNT: a.get("id"),
                                      attack_list.COL_PHASE: entry_phase},
                              actor=actor, at=at)
        _write_table_model(doc_id, title, content, model)

    if json_mode:
        # One canonical result field: ``target_ids`` = the Accounts that were
        # added (or, with --dry-run, would be). ``dry_run`` says which it is, so a
        # reader never has to pick between two fields (AX review PR #548).
        print(json.dumps({
            "doc_id": doc_id, "dry_run": dry_run, "matched": len(matched),
            "target_ids": target_ids, "skipped_duplicates": skipped_ids,
            "entry_phase": entry_phase, "filter_phase": phase_filter},
            ensure_ascii=False))
        return
    verb = "追加予定" if dry_run else "追加"
    print(f"{doc_id}: 条件一致 {len(matched)} 件 / {verb} {len(target_ids)} 件 "
          f"(phase={entry_phase}) / 重複 skip {len(skipped_ids)} 件")
    if dry_run:
        print("  (--dry-run: 書き込みなし。実行は --dry-run を外す)")


def cmd_acquisition_attack_list_send():
    """Plan (dry-run, default) or authorize (--confirm) a bulk outreach to an
    attack-list's prospects — the human 1-confirm gate on external send (ms-132
    e-4504, SPEC 方針4).

    Default (no --confirm): resolve recipients (active rows at --from-phase =
    the list's entry phase 未接触 by default, whose Account has an email), render
    the message, write a *pending* send batch and print the plan (宛先 N 件 +
    サンプル文面 + 差出人). Nothing is sent or recorded — confirm 前は 1 通も出さない.

    --confirm: authorize the doc's pending batch. Hard-gated by
    ``_refuse_if_bus_origin`` so a bus / DM / auto-execute context can NEVER
    authorize — the confirm is structurally human-only. Sending itself is done by
    the Skill (MCP) after this, and each send is booked via
    ``attack-list-send-record`` which refuses outside an authorized batch.
    Env: BEACON_DOC_ID, BEACON_SEND_SUBJECT, BEACON_SEND_MESSAGE,
    BEACON_SEND_FROM_PHASE, BEACON_SEND_LIMIT, BEACON_CONFIRM, BEACON_JSON.
    """
    import hashlib
    import attack_list
    import table_doc
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    confirm = os.environ.get("BEACON_CONFIRM", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    _USAGE = ("Usage: beacon acquisition attack-list-send <attack-list-doc-id> "
              "[--subject <s>] [--message-file <f> | --message <body>] "
              "[--from-phase <name>] [--limit N] [--confirm] [--json]\n"
              "  --from-phase 省略時=リストの先頭フェーズ(未接触)を対象にする。\n"
              "  既定=計画のみ(dry-run、送信も記録もしない、直前の pending 計画は上書き)。"
              "--confirm で人間が1回承認 (bus/armed からは不可)。送信自体は Skill が行う。")
    if not doc_id:
        print("Error: doc-id required\n" + _USAGE, file=sys.stderr)
        sys.exit(1)

    if confirm:
        # ---- authorize the pending batch = the single human confirm ----------
        # Two structural refusals so no *autonomous* context authorizes a bulk
        # external send (SPEC 方針4). This does NOT prove a human typed the
        # command — in an agentic system the AI operates the CLI — but it closes
        # the paths where no human is in the loop at all:
        #   (1) bus / DM / auto-execute origin (BEACON_BUS_ORIGIN);
        #   (2) an *armed* session (a budget grant = autonomous DM-reply mode).
        #       Arming grants auto-reply budget, NOT bulk-send authorization, so a
        #       per-batch human confirm is still required in armed mode.
        # The remaining case (a dialog AI acting on its human's instruction) is
        # the intended operator; the Skill's explicit human-confirm step and the
        # ``authorized_by`` audit trail cover it (philosophy review PR #550).
        if _refuse_if_bus_origin("acquisition_attack_list_send_confirm",
                                 {"doc_id": doc_id}):
            sys.exit(1)
        if _read_bus_budget() is not None:
            print("Error: このセッションは armed (自律 DM 応答モード) です。"
                  "一括連絡の承認は arming とは別に、対話でその都度 人間が行う必要が"
                  "あります。`beacon bus budget` を落としてから承認してください。",
                  file=sys.stderr)
            sys.exit(1)
        data = load_project()
        batch = (sales_entities.pending_send_batch_for_doc(data, doc_id)
                 or sales_entities.authorized_send_batch_for_doc(data, doc_id))
        if batch is None:
            print(f"Error: {doc_id} に承認対象の送信計画(pending batch)がありません。"
                  f"先に `beacon acquisition attack-list-send {doc_id}` "
                  f"(--confirm 無し) で計画を作ってください。", file=sys.stderr)
            sys.exit(1)
        try:
            b = sales_entities.authorize_send_batch(
                data, batch["id"], at=_now_iso(), by=_actor_str())
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        save_project(data)
        n = len(b.get("recipients", []))
        if json_mode:
            print(json.dumps({"doc_id": doc_id, "batch_id": b["id"],
                              "status": b["status"], "recipient_count": n},
                             ensure_ascii=False))
        else:
            print(f"承認しました: batch {b['id']} / 宛先 {n} 件。")
            print(f"  各宛先へ送信後 `beacon acquisition attack-list-send-record "
                  f"{doc_id} <acc-id> --message-id <id>` で記録すると、行が "
                  f"未接触→連絡済 に進み証跡が残ります。")
        return

    # ---- plan (dry-run, default): build the pending batch, print the plan ----
    subject = os.environ.get("BEACON_SEND_SUBJECT", "")
    message = os.environ.get("BEACON_SEND_MESSAGE", "")
    limit_raw = os.environ.get("BEACON_SEND_LIMIT", "")
    limit = int(_parse_number(limit_raw, "--limit")) if limit_raw.strip() else None
    if not message.strip():
        print("Error: メッセージ本文が必要です (--message-file / --message)\n"
              + _USAGE, file=sys.stderr)
        sys.exit(1)

    content, title, model = _load_table_model(doc_id)
    if not attack_list.is_attack_list(model.get("columns", [])):
        print(f"Error: {doc_id} はアタックリスト (attack-list) ではありません",
              file=sys.stderr)
        sys.exit(1)
    phase_col = next((c for c in model.get("columns", [])
                      if c.get("key") == attack_list.COL_PHASE), {})
    entry_phase = (phase_col.get("values")
                   or [attack_list.INITIAL_PROSPECT_PHASE])[0]
    from_phase = os.environ.get("BEACON_SEND_FROM_PHASE", "") or entry_phase

    data = load_project()
    recipients, no_email = [], []
    for row in table_doc.active_rows(model):
        cells = row.get("cells", {})
        if cells.get(attack_list.COL_PHASE) != from_phase:
            continue
        acc_id = cells.get(attack_list.COL_ACCOUNT)
        acc = sales_entities.find_account(data, acc_id) if acc_id else None
        email = ""
        if acc:
            for c in acc.get("contacts", []):
                if c.get("email"):
                    email = c["email"]
                    break
        if not email:
            no_email.append(acc_id)
            continue
        recipients.append({"acc_id": acc_id, "row_id": row.get("id", ""),
                           "email": email})
    if limit is not None:
        recipients = recipients[:limit]

    digest = hashlib.sha256((subject + "\n" + message).encode("utf-8")).hexdigest()
    preview = message.strip()[:200]
    try:
        route = sales_entities.resolve_route(data, "gmail")
    except Exception:
        route = None
    from_email = (route or {}).get("email", "")

    superseded = sales_entities.pending_send_batch_for_doc(data, doc_id)
    superseded_id = superseded["id"] if superseded else None
    batch = sales_entities.create_send_batch(
        data, doc_id=doc_id, recipients=recipients, message_digest=digest,
        message_preview=preview, created_at=_now_iso(), created_by=_actor_str())
    save_project(data)

    if json_mode:
        print(json.dumps({
            "doc_id": doc_id, "batch_id": batch["id"], "status": "pending",
            # ``authorized`` is the self-describing not-yet-confirmed flag: a
            # reader must not mistake status=pending for "queued to send" (AX
            # review PR #550). Nothing sends/records until authorized == true.
            "authorized": False,
            "from_phase": from_phase, "recipient_count": len(recipients),
            "recipients": [{"acc_id": r["acc_id"], "email": r["email"]}
                           for r in recipients],
            "skipped_no_email": no_email, "subject": subject,
            "message_digest": digest, "from_email": from_email,
            "superseded_batch": superseded_id}, ensure_ascii=False))
        return
    print(f"[送信計画 dry-run] batch {batch['id']} / {doc_id}")
    if superseded_id:
        print(f"  (直前の pending 計画 {superseded_id} を上書きしました)")
    print(f"  差出人: {from_email or '(未設定 — 送信時に Skill が identity を解決)'}")
    print(f"  対象フェーズ: {from_phase} / 宛先 {len(recipients)} 件"
          + (f" / email 無しで除外 {len(no_email)} 件" if no_email else ""))
    print(f"  件名: {subject or '(なし)'}")
    print(f"  本文プレビュー: {preview}")
    for r in recipients[:10]:
        print(f"    - {r['acc_id']} <{r['email']}>")
    if len(recipients) > 10:
        print(f"    … 他 {len(recipients) - 10} 件")
    print("\n  confirm 前は 1 通も送信/記録されません。")
    print(f"  この宛先・文面で送るなら: "
          f"`beacon acquisition attack-list-send {doc_id} --confirm`")


def _stamp_attack_list_contact(model, doc_id, title, content, row_id, date_str,
                               new_phase):
    """Stamp an attack-list row's 最終接触日 (and optionally advance its 打診フェーズ)
    in ONE ``set_cell`` pair + a single write, so the date and the phase always
    commit together — never a partial write. ms-132 e-4623 shared skeleton for
    send-record / reply-record (PR #559 保守性レビュー M2: the two flows had a
    parallel-but-divergent write path; this makes the row-mutation step identical
    for both, with the per-flow difference expressed only as ``new_phase``).

    ``new_phase`` is the target 打診フェーズ, or ``None`` to leave the phase as-is
    (date-only). Raises ``table_doc.TableDocError`` on a write failure; the caller
    surfaces it and owns the 証跡 save ordering (PR #553 atomicity stays caller-side).
    """
    import table_doc
    import attack_list
    table_doc.set_cell(model, row_id, attack_list.COL_LAST_CONTACT, date_str,
                       actor=_actor_str(), at=_now_iso())
    if new_phase is not None:
        table_doc.set_cell(model, row_id, attack_list.COL_PHASE, new_phase,
                           actor=_actor_str(), at=_now_iso())
    _write_table_model(doc_id, title, content, model)


def cmd_acquisition_attack_list_send_record():
    """Book one sent email inside an attack-list's AUTHORIZED send batch (ms-132
    e-4504): drive the prospect row 未接触→連絡済, write the outbound Communication
    (証跡) on the Account, and mark the recipient sent.

    Refuses unless the doc has an authorized batch containing this Account, and
    (when the batch carries a digest) unless the message actually sent matches the
    one the human confirmed — the recorded effect of a send is structurally
    unreachable without the human confirm, and bound to the confirmed 文面.
    Idempotent: a recipient already booked is refused (no double-send record).

    The 証跡 (project.json) and the row phase-drive (table-doc) are two stores; the
    phase-drive is done FIRST as a precondition and a failure aborts loudly (exit
    1) — project.json is saved LAST so a phase-drive failure leaves neither store
    changed (maintainability/philosophy review PR #550).
    Env: BEACON_DOC_ID, BEACON_SEND_ACC_ID, BEACON_SEND_MESSAGE_ID (required),
    BEACON_SEND_URL, BEACON_SEND_SUBJECT, BEACON_SEND_MESSAGE, BEACON_JSON.
    """
    import hashlib
    import attack_list
    import table_doc
    import table_type
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    acc_id = os.environ.get("BEACON_SEND_ACC_ID", "")
    message_id = os.environ.get("BEACON_SEND_MESSAGE_ID", "")
    url = os.environ.get("BEACON_SEND_URL", "")
    subject = os.environ.get("BEACON_SEND_SUBJECT", "")
    message = os.environ.get("BEACON_SEND_MESSAGE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    _USAGE = ("Usage: beacon acquisition attack-list-send-record "
              "<attack-list-doc-id> <acc-id> --message-id <id> "
              "[--message-file <f>|--message <body>] [--subject <s>] "
              "[--url <permalink>] [--json]")
    if not doc_id or not acc_id:
        print(_USAGE, file=sys.stderr)
        sys.exit(1)
    # --message-id is the RFC822 trace ref; without it the 証跡 loses its origin
    # pointer, so require it rather than silently record an empty ref (AX review).
    if not message_id.strip():
        print("Error: --message-id は必須です "
              "(MCP send_email の戻り値の message-id を渡してください)", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    batch = sales_entities.authorized_send_batch_for_doc(data, doc_id)
    if batch is None:
        print(f"Error: {doc_id} に authorized な送信バッチがありません "
              f"(先に `beacon acquisition attack-list-send {doc_id} --confirm` で"
              f"人間が承認する必要があります)", file=sys.stderr)
        sys.exit(1)
    rec = sales_entities.batch_recipient(batch, acc_id)
    if rec is None:
        print(f"Error: {acc_id} は承認済みバッチ {batch['id']} の宛先ではありません",
              file=sys.stderr)
        sys.exit(1)
    if rec.get("sent_at"):
        print(f"Error: {acc_id} は既にバッチ {batch['id']} で送信記録済みです",
              file=sys.stderr)
        sys.exit(1)
    # Bind confirmed↔sent: the message actually sent must digest to what the human
    # confirmed at plan time (philosophy review PR #550 — otherwise the 文面 could
    # be swapped after confirm and still record as a legitimate send).
    expected_digest = batch.get("message_digest") or ""
    if expected_digest:
        if not message.strip():
            print("Error: 送信した文面 (--message-file / --message) を渡して"
                  "承認時の文面と照合してください", file=sys.stderr)
            sys.exit(1)
        got = hashlib.sha256((subject + "\n" + message).encode("utf-8")).hexdigest()
        if got != expected_digest:
            print("Error: 送信した文面が承認時の文面と一致しません "
                  "(承認された文面のみ送信・記録できます)", file=sys.stderr)
            sys.exit(1)

    # Phase-drive FIRST (precondition). A no-op (row not at the funnel entry) is
    # fine; a real write failure aborts before any project.json is saved.
    _content, title, model = _load_table_model(doc_id)
    table_type.install()
    target_row = attack_list.find_row_by_account(
        table_doc.active_rows(model), acc_id)
    driven_to = None
    last_contact = None
    if target_row is not None:
        vals = attack_list.phase_values(model)
        cur = target_row.get("cells", {}).get(attack_list.COL_PHASE)
        # 未接触 → 連絡済 (funnel entry → contacted); None = leave phase as-is.
        new_phase = (vals[attack_list.PHASE_IDX_CONTACTED]
                     if (len(vals) > attack_list.PHASE_IDX_CONTACTED
                         and cur == vals[attack_list.PHASE_IDX_UNTOUCHED])
                     else None)
        last_contact = _today_iso()
        try:
            # e-4623: stamp 最終接触日 (= 送信日) on every send-record + advance the
            # phase together in one write. The schema's date column was dead (no
            # flow wrote it), so the row phase advanced while its sibling date
            # stayed empty. Shared skeleton keeps date+phase atomic (no partial write).
            _stamp_attack_list_contact(model, doc_id, title, _content,
                                       target_row["id"], last_contact, new_phase)
            driven_to = new_phase
        except table_doc.TableDocError as exc:
            print(f"Error: 行の更新に失敗しました ({exc})。"
                  f"証跡は記録していません。", file=sys.stderr)
            sys.exit(1)

    # Now book the 証跡 + mark the recipient sent, and save project.json LAST.
    try:
        import occupation
        sales_entities.record_batch_send(data, doc_id, acc_id, at=_now_iso(),
                                         message_id=message_id)
        # ms-143: record the 証跡 via profession-generic occupation.add_evidence
        # (byte-identical to the sales-concrete recorder), so this verb stops
        # symbol-reaching a PROFESSION_CONCRETE_SYMBOL.
        comm_id = occupation.add_evidence(
            data, acc_id, summary=subject or f"アタックリスト一括連絡 ({doc_id})",
            direction="outbound", channel="email",
            source={"ref": message_id, "url": url},
            occurred_at=_now_iso(), created_at=_now_iso())["id"]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    sales_entities.batch_recipient(batch, acc_id)["comm_id"] = comm_id
    save_project(data)

    if json_mode:
        # e-4623 (PR #559 AX F1): disclose the stamped 最終接触日 so an AI can observe
        # the date write from the result (not a silent state change), independent of
        # whether the phase advanced.
        print(json.dumps({"doc_id": doc_id, "acc_id": acc_id, "comm_id": comm_id,
                          "phase_driven_to": driven_to,
                          "last_contact": last_contact}, ensure_ascii=False))
    else:
        drive_note = f" / 行 → {driven_to}" if driven_to else ""
        date_note = f" / 接触日 {last_contact}" if last_contact else ""
        print(f"記録: {acc_id} outbound email 証跡 {comm_id}{drive_note}{date_note}")


def cmd_acquisition_attack_list_awaiting_reply():
    """List an attack-list's prospects awaiting a reply (ms-132 e-4505): rows at
    連絡済 (contacted, not yet replied), with each Account's email and the
    message-id we sent — the worklist the reply-watch polls for inbound replies.
    Read-only. Env: BEACON_DOC_ID, BEACON_JSON.
    """
    import attack_list
    import table_doc
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not doc_id:
        print("Usage: beacon acquisition attack-list-awaiting-reply "
              "<attack-list-doc-id> [--json]", file=sys.stderr)
        sys.exit(1)
    _content, _title, model = _load_table_model(doc_id)
    if not attack_list.is_attack_list(model.get("columns", [])):
        print(f"Error: {doc_id} はアタックリスト (attack-list) ではありません",
              file=sys.stderr)
        sys.exit(1)
    vals = attack_list.phase_values(model)
    # 連絡済 = the "sent, awaiting reply" funnel position.
    contacted = (vals[attack_list.PHASE_IDX_CONTACTED]
                 if len(vals) > attack_list.PHASE_IDX_CONTACTED else None)
    data = load_project()
    sent_ref = sales_entities.sent_message_ids_for_doc(data, doc_id)
    waiting = []
    for row in table_doc.active_rows(model):
        cells = row.get("cells", {})
        if contacted is None or cells.get(attack_list.COL_PHASE) != contacted:
            continue
        acc_id = cells.get(attack_list.COL_ACCOUNT)
        acc = sales_entities.find_account(data, acc_id) if acc_id else None
        email = ""
        if acc:
            for c in acc.get("contacts", []):
                if c.get("email"):
                    email = c["email"]
                    break
        waiting.append({"acc_id": acc_id, "row_id": row.get("id", ""),
                        "email": email, "message_id": sent_ref.get(acc_id, ""),
                        "last_contact": cells.get(attack_list.COL_LAST_CONTACT, "")})
    if json_mode:
        print(json.dumps({"doc_id": doc_id, "phase": contacted,
                          "awaiting": waiting}, ensure_ascii=False))
        return
    if not waiting:
        print(f"{doc_id}: 返信待ち (連絡済) の宛先はありません")
        return
    print(f"{doc_id}: 返信待ち {len(waiting)} 件 (phase={contacted})")
    for w in waiting:
        print(f"  - {w['acc_id']} <{w['email']}> msg={w['message_id'] or '(不明)'}")


def _fire_attack_list_reply_trigger(acc_id, doc_id, phase, message_id=""):
    """Notify the human that a prospect replied (ms-132 e-4505). Writes the
    trigger file directly (mirrors the other _fire_*_trigger helpers) rather than
    mutating os.environ to call cmd_trigger_fire. The name carries the reply's
    message-id so a *later, distinct* reply from the same prospect fires its own
    trigger instead of being deduped away by name."""
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    suffix = f"-{message_id}" if message_id else ""
    name = f"attack-list-reply-{acc_id}{suffix}"
    trigger_path = os.path.join(triggers_dir, f"{name}.json")
    if os.path.exists(trigger_path):
        return False
    import datetime
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump({
            "name": name, "kind": "attack-list-reply",
            "acc_id": acc_id, "doc_id": doc_id,
            "message": (f"{acc_id} からアタックリスト打診への返信が届きました "
                        f"({doc_id}, 行 → {phase})。返信内容を確認して次の一手 "
                        f"(日程調整 / 詳細回答) を人が判断してください。"),
            "created_at": datetime.datetime.now().isoformat(),
        }, f, ensure_ascii=False)
        f.write("\n")
    return True


def cmd_acquisition_attack_list_reply_record():
    """Record a detected reply from a prospect (ms-132 e-4505): drive the row
    連絡済→返信あり, write the INBOUND Communication (証跡) on the Account, and fire a
    trigger to notify the human. Detection-only — it books an observed inbound
    event and never sends a reply.

    Safe to run autonomously (the reply-watch Operation is the intended caller),
    so it carries no confirm gate — it records an *inbound* fact, not an outward
    send. The phase advances only 連絡済→返信あり (funnel 2nd→3rd); a reply on a row
    at another phase still records the 証跡 + notifies but does not misadvance.
    Env: BEACON_DOC_ID, BEACON_SEND_ACC_ID, BEACON_SEND_MESSAGE_ID,
    BEACON_SEND_URL, BEACON_COMM_SUMMARY, BEACON_JSON.
    """
    import attack_list
    import table_doc
    import table_type
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    acc_id = os.environ.get("BEACON_SEND_ACC_ID", "")
    message_id = os.environ.get("BEACON_SEND_MESSAGE_ID", "")
    url = os.environ.get("BEACON_SEND_URL", "")
    summary = os.environ.get("BEACON_COMM_SUMMARY", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not doc_id or not acc_id:
        print("Usage: beacon acquisition attack-list-reply-record (INBOUND 検知記録) "
              "<attack-list-doc-id> <acc-id> "
              "[--message-id <相手返信のid、検知時に不明なら省略可>] [--url <link>] "
              "[--summary <相手返信の1行要約>] [--json]", file=sys.stderr)
        sys.exit(1)

    _content, title, model = _load_table_model(doc_id)
    if not attack_list.is_attack_list(model.get("columns", [])):
        print(f"Error: {doc_id} はアタックリスト (attack-list) ではありません",
              file=sys.stderr)
        sys.exit(1)
    table_type.install()
    target_row = attack_list.find_row_by_account(
        table_doc.active_rows(model), acc_id)
    if target_row is None:
        print(f"Error: {acc_id} は {doc_id} の行に居ません — "
              f"`beacon acquisition attack-list-awaiting-reply {doc_id}` で返信待ち"
              f"一覧を確認してください", file=sys.stderr)
        sys.exit(1)
    vals = attack_list.phase_values(model)
    cur = target_row.get("cells", {}).get(attack_list.COL_PHASE)
    # Advance only 連絡済 → 返信あり (funnel 2nd → 3rd). A reply on a row at another
    # phase still records the 証跡 + notifies but does not misadvance the phase.
    will_advance = (len(vals) > attack_list.PHASE_IDX_REPLIED
                    and cur == vals[attack_list.PHASE_IDX_CONTACTED])

    # Build the 証跡 in memory FIRST (no save), so if the phase-drive write fails
    # below nothing is persisted — comm cannot land phase-advanced-without-証跡
    # nor 証跡-without-attempted-phase (保守性レビュー PR #553: atomicity).
    data = load_project()
    try:
        import occupation
        # ms-143: record the 証跡 via profession-generic occupation.add_evidence
        # (byte-identical to the sales-concrete recorder), so this verb stops
        # symbol-reaching a PROFESSION_CONCRETE_SYMBOL.
        comm_id = occupation.add_evidence(
            data, acc_id, summary=summary or f"アタックリスト打診先からの返信 ({doc_id})",
            direction="inbound", channel="email",
            source={"ref": message_id, "url": url},
            occurred_at=_now_iso(), created_at=_now_iso())["id"]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    driven_to = None
    last_contact = _today_iso()
    new_phase = vals[attack_list.PHASE_IDX_REPLIED] if will_advance else None
    try:
        # e-4623: stamp 最終接触日 (= 返信日) on every reply-record + advance the phase
        # (when at 連絡済) together in one write via the shared skeleton — 打診フェーズ
        # と 最終接触日 が同時に確定する (片方だけの部分書込を作らない)。証跡は行の
        # 書込が成功した後にだけ save する (PR #553 の atomicity を維持)。
        _stamp_attack_list_contact(model, doc_id, title, _content,
                                   target_row["id"], last_contact, new_phase)
        driven_to = new_phase
    except table_doc.TableDocError as exc:
        print(f"Error: 行の更新に失敗しました ({exc})。証跡は記録して"
              f"いません。", file=sys.stderr)
        sys.exit(1)
    save_project(data)  # persist the 証跡 only after the row write succeeded

    # Notify the human (idiomatic path = a trigger), writing the file directly.
    notified = _fire_attack_list_reply_trigger(
        acc_id, doc_id, driven_to or cur, message_id)

    if json_mode:
        print(json.dumps({
            "doc_id": doc_id, "acc_id": acc_id, "comm_id": comm_id,
            "phase_driven_to": driven_to,
            # explicit so an AI never reads a null phase as a failure: the phase
            # was intentionally left as-is because the row was not at 連絡済.
            "phase_guard_skipped": driven_to is None,
            # e-4623 (PR #559 AX F1): disclose the stamped 最終接触日 (= 返信日) — the
            # date write happens on every reply-record, independent of phase advance.
            "last_contact": last_contact,
            "notified": notified}, ensure_ascii=False))
    else:
        drive_note = (f" / 行 → {driven_to}" if driven_to
                      else f" (phase 変更なし: 行は {cur})")
        note = "" if notified else " (通知は既出のためスキップ)"
        print(f"返信記録: {acc_id} inbound 証跡 {comm_id}{drive_note} / 接触日 "
              f"{last_contact}{note}")


def cmd_acquisition_attack_list_promote():
    """Promote a reacted prospect to an Opportunity — lead conversion (ms-132
    e-4506). Requires the row to be at 返信あり or アポ. Creates an Opportunity for
    the Account, drives the Account's lifecycle phase 未接触→リード, and leaves the
    attack-list row in place (its history is preserved). Idempotent-ish: refuses
    if the Account already has a live Opportunity (no duplicate deal).
    Env: BEACON_DOC_ID, BEACON_SEND_ACC_ID, BEACON_OPP_TITLE, BEACON_JSON.
    """
    import attack_list
    import table_doc
    import sales_entities
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    acc_id = os.environ.get("BEACON_SEND_ACC_ID", "")
    title = os.environ.get("BEACON_OPP_TITLE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not doc_id or not acc_id:
        print("Usage: beacon acquisition attack-list-promote "
              "<attack-list-doc-id> <acc-id> [--title <商談名>] [--json]",
              file=sys.stderr)
        sys.exit(1)

    _content, _title, model = _load_table_model(doc_id)
    if not attack_list.is_attack_list(model.get("columns", [])):
        print(f"Error: {doc_id} はアタックリスト (attack-list) ではありません",
              file=sys.stderr)
        sys.exit(1)
    row = attack_list.find_row_by_account(table_doc.active_rows(model), acc_id)
    if row is None:
        print(f"Error: {acc_id} は {doc_id} の行に居ません", file=sys.stderr)
        sys.exit(1)
    vals = attack_list.phase_values(model)
    cur = row.get("cells", {}).get(attack_list.COL_PHASE)
    # Only a *reacted* prospect (返信あり / アポ) is a lead; untouched/contacted rows
    # are not yet convertible.
    if not attack_list.is_reacted(cur, vals):
        print(f"Error: {acc_id} の行は '{cur}' です。返信あり / アポ の行のみ商談へ"
              f"引き上げできます。先に返信を記録: "
              f"`beacon acquisition attack-list-reply-record {doc_id} {acc_id} "
              f"--message-id <id>`", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    acc = sales_entities.find_account(data, acc_id)
    if acc is None:
        print(f"Error: Account {acc_id} が見つかりません", file=sys.stderr)
        sys.exit(1)
    # No duplicate deal: refuse if the Account already has a live Opportunity.
    # ``live_opportunities`` owns the "not terminal/cancelled" definition, so a
    # new terminal status added later can't silently slip past this guard.
    live_opps = [o.get("id") for o in sales_entities.live_opportunities(data)
                 if o.get("account_id") == acc_id]
    if live_opps:
        print(f"Error: {acc_id} には既に商談があります ({', '.join(live_opps)})。"
              f"重複した商談は作りません (確認: `beacon opportunity show {live_opps[0]}`)。",
              file=sys.stderr)
        sys.exit(1)

    acc_name = acc.get("name") or acc_id
    pre_phase = acc.get("phase")
    try:
        # opportunity_add itself derives the Account's lifecycle phase upward via
        # _auto_advance_account_phase (新規商談 → 未接触→リード; sales_entities L1018),
        # so we don't set it separately — we read and report the actual result.
        opp_id = sales_entities.opportunity_add(
            data, title or f"{acc_name} 商談 (アタックリスト由来)",
            account_id=acc_id, created_at=_now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)

    final_phase = sales_entities.find_account(data, acc_id).get("phase")
    driven_account_phase = final_phase if final_phase != pre_phase else None

    if json_mode:
        print(json.dumps({
            "doc_id": doc_id, "acc_id": acc_id, "opportunity": opp_id,
            "account_phase": final_phase,          # the actual resulting phase (truth)
            "account_phase_driven_to": driven_account_phase,  # None = unchanged
            "row_phase": cur, "row_kept": True}, ensure_ascii=False))
    else:
        ph = f" / 顧客 phase → {driven_account_phase}" if driven_account_phase else ""
        print(f"引き上げ: {acc_id} → 商談 {opp_id}{ph} "
              f"(リスト行 '{cur}' は保持)")


# --- send-account ledger (ms-107 e-3365) -----------------------------------
# label → {email, routes{service:{namespace, alias}}}. Internal verbs invoked by
# the sales Skills to register accounts and resolve the concrete MCP route a
# send must use. Not user-facing CLI verbs (kept out of bin/beacon / README).
