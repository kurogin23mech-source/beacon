#!/usr/bin/env python3
"""cmd_tool.py — the `beacon tool *` command family (ms-180 e-7000 / e-7002 / e-7006).

The human / AI-facing door onto the tool ledger (``lib/tool_ledger.py``): register
a tool, read the ledger back, look a tool up by what it is FOR, and read the debt
list of tools that were used without being declared.

What is deliberately absent: a ``tool run`` verb. Beacon declares tools; it does
not execute them (SPEC ms-180 方針2/方針5). Wrapping execution was the rejected
alternative — it would make Beacon carry the implementation of every MCP tool and
would add a layer the AI has to remember to route through, which is the "wiring
exists but is never used" failure ms-166 spent a milestone removing. The two
interrupts (PreToolUse / PostToolUse) see every call already, so nothing needs to
be routed anywhere.

Depends only on commands_shared (upward) + leaf modules — acyclic (SPEC 方針4).
"""

import json
import os
import sys

import tool_ledger as tl

from commands_shared import get_project_file


def _beacon_dir():
    return os.path.dirname(get_project_file()) or ".beacon"


def _ledger_path():
    """Path of the ledger's local truth file. Derives the filename from
    ``tool_ledger.LEDGER_RELPATH`` so the CLI and the hooks (which resolve it from
    a project root) can never disagree about where the ledger lives."""
    return os.path.join(_beacon_dir(), tl.LEDGER_RELPATH[-1])


def _load():
    return tl.load_ledger(_ledger_path())


def _sync_mirror(ledger):
    """Push the one-way cloud mirror. Best-effort by design: the ledger is already
    on local disk — which IS the truth — so a failed mirror costs visibility, not
    data. The failure is REPORTED on stderr rather than swallowed, because a
    silently absent mirror is indistinguishable from an empty one (the e-6655
    lesson: a silent read/write failure invents absence)."""
    try:
        import tool_ledger_mirror as mirror
        if not mirror.sync_mirror(ledger):
            print("Warning: 道具台帳のクラウドミラーを書けませんでした "
                  "(台帳そのものはローカルに保存済み)。", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: 道具台帳のクラウドミラー同期に失敗 "
              f"({type(exc).__name__}: {exc})。台帳はローカルに保存済みです。",
              file=sys.stderr)


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _actor():
    """Who registered a row. Best-effort identity, "" when unresolvable."""
    try:
        from agent import resolve_agent_id
        return resolve_agent_id() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# tool add
# ---------------------------------------------------------------------------

def cmd_tool_add():
    name = os.environ.get("BEACON_TOOL_NAME", "").strip()
    purpose = os.environ.get("BEACON_TOOL_PURPOSE", "")
    effect = os.environ.get("BEACON_TOOL_EFFECT", "").strip()
    recorder = os.environ.get("BEACON_TOOL_RECORD", "").strip()
    no_record = os.environ.get("BEACON_TOOL_NO_RECORD", "")
    grain = os.environ.get("BEACON_TOOL_GRAIN", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not name:
        print("Error: 道具の名前が必要です "
              "(例: beacon tool add mcp__gmail__send_email --purpose ... "
              "--effect outward)", file=sys.stderr)
        sys.exit(1)
    if not effect:
        print(f"Error: --effect が必要です ({', '.join(tl.EFFECTS)} のいずれか)。"
              "作用は推測ではなく宣言するものなので既定値は置きません。",
              file=sys.stderr)
        sys.exit(1)
    if not purpose.strip():
        print("Error: --purpose が必要です "
              "(例: --purpose \"顧客にメールを送る\")。用途は後から「何のために"
              "登録したのか」を読む唯一の列なので、空のままは登録できません。",
              file=sys.stderr)
        sys.exit(1)

    try:
        row = tl.build_record(name, purpose=purpose, effect=effect,
                              recorder=recorder, grain=grain,
                              decline_reason=no_record,
                              at=_now_iso(), by=_actor())
    except tl.ToolLedgerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    ledger = _load()
    existed = tl.lookup(ledger, name) is not None
    tl.upsert(ledger, row)
    tl.save_ledger(_ledger_path(), ledger)
    _sync_mirror(ledger)

    if json_mode:
        print(json.dumps({"tool": row, "updated": existed}, ensure_ascii=False))
        return
    verb = "更新" if existed else "登録"
    print(f"{verb}: {name} [{effect}] — {_record_label(row)}")
    # 受入条件4: say out loud that the grain was DEFAULTED, not chosen. The row
    # records it too; echoing it here is what stops the default from being
    # invisible at the moment it is taken.
    if row["record"].get("grain_source") == "default":
        print(f"  ℹ 粒度 (--grain) 未指定なので {tl.GRAIN_EACH} "
              f"(毎回記録) にしました。1 ターン分を 1 件に畳むなら "
              f"`--grain {tl.GRAIN_TURN}`。", file=sys.stderr)
    if row["record"]["sink"] == tl.SINK_UNDECIDED:
        print("  ℹ 記録先は未判断です。使ったことを証跡として残すかは未決のまま"
              "台帳に載りました (`beacon tool unregistered` ではなく "
              "`beacon tool list --undecided` に出ます)。", file=sys.stderr)


def _record_label(row):
    block = row.get("record") or {}
    sink = block.get("sink")
    if sink == tl.SINK_SPECIFIED:
        return f"記録先 {block.get('recorder')} ({block.get('grain')})"
    if sink == tl.SINK_DECLINED:
        return f"記録しない ({block.get('reason')})"
    return "記録先 未判断"


# ---------------------------------------------------------------------------
# tool list / show / remove
# ---------------------------------------------------------------------------

def cmd_tool_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    only_undecided = os.environ.get("BEACON_TOOL_UNDECIDED", "") == "1"
    ledger = _load()
    rows = tl.undecided_tools(ledger) if only_undecided else (
        sorted(ledger.get("tools") or [], key=lambda r: str(r.get("name") or "")))

    if json_mode:
        print(json.dumps({"tools": rows, "count": len(rows),
                          "undecided_count": len(tl.undecided_tools(ledger))},
                         ensure_ascii=False))
        return
    if not rows:
        if only_undecided:
            print("記録先が未判断の道具はありません。")
        else:
            print("登録されている道具はまだありません。")
            print("  beacon tool add <道具名> --purpose \"<何のために使うか>\" "
                  "--effect <read-only|state-change|outward>")
        return
    for row in rows:
        print(f"  {row.get('name')} [{row.get('effect')}] — "
              f"{_record_label(row)}")
        if row.get("purpose"):
            print(f"      用途: {row['purpose']}")
    if only_undecided:
        return
    undecided = tl.undecided_tools(ledger)
    if undecided:
        print(f"\n⚠ 記録先が未判断: {len(undecided)} 件 "
              f"(`beacon tool list --undecided`)")
    # 用途が空の行 (= build_record が拒否するので、旧ファイルか手編集だけ)。
    # 片方の writer にしか効かない規則は規則ではない (ms-174 の教訓)。
    purposeless = tl.purposeless_tools(ledger)
    if purposeless:
        print(f"\n⚠ 用途が空の行: {len(purposeless)} 件 "
              f"— {', '.join(str(r.get('name')) for r in purposeless[:5])}")
        print("   用途は今は登録時に必須なので、これは古いファイルか手編集です。"
              "`beacon tool add <道具名> --purpose ...` で埋め直してください。")
    # 受入条件3: a `turn` grain that silently became `each` because no turn
    # boundary was observable. Saying nothing here would make the degradation
    # indistinguishable from a working fold.
    try:
        import tool_turn
        degraded = tool_turn.degraded_tools(tool_turn.turn_path(_beacon_dir()))
    except Exception:
        degraded = []
    if degraded:
        print(f"\n⚠ ターン境界が観測できず、粒度 {tl.GRAIN_TURN} が "
              f"{tl.GRAIN_EACH} に縮退した道具: {len(degraded)} 件")
        for row in degraded[:5]:
            print(f"   {row['name']} — {row['count']} 回 "
                  f"(最後 {row['last_seen']})")
        print("   記録は毎回残っています (取り落ちではなく重複側に倒しています)。"
              "畳みを効かせるには UserPromptSubmit で "
              "beacon-save-hook.sh が呼ばれる設定が必要です。")


def cmd_tool_show():
    name = os.environ.get("BEACON_TOOL_NAME", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not name:
        print("Error: 道具の名前が必要です", file=sys.stderr)
        sys.exit(1)
    row = tl.lookup(_load(), name)
    if row is None:
        if json_mode:
            print(json.dumps({"tool": None, "name": name}, ensure_ascii=False))
        else:
            print(f"未登録: {name}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps({"tool": row}, ensure_ascii=False))
        return
    print(f"道具:   {row.get('name')}")
    print(f"用途:   {row.get('purpose') or '(未記載)'}")
    print(f"作用:   {row.get('effect')}")
    print(f"記録先: {_record_label(row)}")
    if row.get("registered_at"):
        print(f"登録:   {row['registered_at']} {row.get('registered_by') or ''}")


def cmd_tool_remove():
    name = os.environ.get("BEACON_TOOL_NAME", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not name:
        print("Error: 道具の名前が必要です", file=sys.stderr)
        sys.exit(1)
    ledger = _load()
    if not tl.remove(ledger, name):
        print(f"未登録なので何も消していません: {name}", file=sys.stderr)
        sys.exit(1)
    tl.save_ledger(_ledger_path(), ledger)
    _sync_mirror(ledger)
    if json_mode:
        print(json.dumps({"removed": name}, ensure_ascii=False))
    else:
        print(f"削除: {name}")


# ---------------------------------------------------------------------------
# tool lookup — 用途から道具を引く (e-7006)
# ---------------------------------------------------------------------------

def cmd_tool_lookup():
    """Answer 「この用途にはどの道具を使うのか」 from the ledger.

    The 用途 column is the one column no interrupt consumes (SPEC 方針9). It exists
    so two ways of doing the same job do not quietly coexist — and a column nobody
    can query would not achieve that, which is why this verb is part of the same
    milestone rather than a later nicety."""
    query = os.environ.get("BEACON_TOOL_QUERY", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not query.strip():
        print("Error: 探したい用途が必要です "
              "(例: beacon tool lookup \"メールを確認する\")", file=sys.stderr)
        sys.exit(1)
    hits = tl.find_by_purpose(_load(), query)
    if json_mode:
        print(json.dumps({"query": query, "tools": hits, "count": len(hits)},
                         ensure_ascii=False))
        return
    if not hits:
        print(f"「{query}」に合う道具は台帳にありません。")
        print("  台帳は網羅を目的にしていないので、未登録なだけかもしれません "
              "(`beacon tool add` で足せます)。")
        return
    print(f"「{query}」で引ける道具 {len(hits)} 件:")
    for row in hits:
        print(f"  {row.get('name')} [{row.get('effect')}] — "
              f"{row.get('purpose') or '(用途未記載)'}")
    if len(hits) > 1:
        print("\nℹ 同じ用途に複数の道具が出ています。これが『同じ業務に 2 つの"
              "流儀が混在している』状態かどうかは読み手が判断してください "
              "(台帳は正典化の材料を出すだけで、使用を強制しません)。")


# ---------------------------------------------------------------------------
# tool unregistered — 台帳に無い道具が使われた記録 (e-7002)
# ---------------------------------------------------------------------------

def cmd_tool_unregistered():
    """Uses that left NO record, split by WHY (ms-180 受入条件2).

    Two reasons, kept apart because the fix differs: a tool nobody declared has
    to be registered, while a declared-but-unjudged tool has an open question to
    close. Merging them into one "no record" list would hide which action each
    row needs — and merging *absent* with *unexamined* is the collapse the
    3-valued record sink exists to prevent.

    A ``declined`` tool never appears here: that is a decision, and listing it
    would turn a settled answer into permanent noise.
    """
    import tool_usage_debt as debt
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if os.environ.get("BEACON_TOOL_CLEAR", "") == "1":
        removed = debt.clear_debt(debt.debt_path(_beacon_dir()))
        print(f"記録が残らなかった使用の記録を {removed} 件消しました。"
              if not json_mode
              else json.dumps({"cleared": removed}, ensure_ascii=False))
        return
    rows = debt.load_debt(debt.debt_path(_beacon_dir()))
    unregistered = debt.debt_by_reason(rows, debt.REASON_UNREGISTERED)
    undecided = debt.debt_by_reason(rows, debt.REASON_UNDECIDED)
    if json_mode:
        print(json.dumps({"unregistered": unregistered,
                          "undecided": undecided,
                          "count": len(rows)}, ensure_ascii=False))
        return
    if not rows:
        print("記録が残らなかった道具の使用はありません。")
        return
    print(f"⚠ 使ったのに記録が残らなかった道具 {len(rows)} 件:")
    if unregistered:
        print(f"\n  [台帳に無い] {len(unregistered)} 件 "
              f"— `beacon tool add <道具名> ...` で登録して閉じる")
        for row in unregistered:
            print(f"    {row.get('name')} — {row.get('count')} 回 "
                  f"(最初 {row.get('first_seen')} / 最後 {row.get('last_seen')})")
    if undecided:
        print(f"\n  [記録先が未判断] {len(undecided)} 件 "
              f"— 台帳に在るが記録するかを誰も判断していない。"
              f"`--record <recorder>` か `--no-record \"<理由>\"` で閉じる")
        for row in undecided:
            print(f"    {row.get('name')} — {row.get('count')} 回 "
                  f"(最初 {row.get('first_seen')} / 最後 {row.get('last_seen')})")
    print("\n台帳は事前に全部埋める必要はありません (端末の走査はしない方針)。"
          "実際に使われた道具だけがここに出ます。")
    print("消すだけなら `beacon tool unregistered --clear`。")


# ---------------------------------------------------------------------------
# tool used — 道具の利用 1 回を証跡として書く、職種中立の口 (e-7004)
# ---------------------------------------------------------------------------

def cmd_tool_used():
    """Record ONE use of a tool against the worked Target — dev or sales alike.

    This is the writer the ``PostToolUse`` interrupt calls. The JUDGMENT (record
    or not, and at what grain) is made once, by ``tool_ledger.record_plan``, in
    the hook — this verb carries it out. ``--record`` is how the hook passes the
    already-resolved decision; omitted, this verb consults the ledger itself, so a
    human or AI can run it by hand and get the same routing (one judgment
    function, two entry points, never two policies).

    ``--no-ledger-check`` exists for the explicitly manual case: record a use even
    though the tool is not in the ledger. It never fires from the hook, because a
    hook that recorded unregistered tools anyway would make the ledger's promise
    ("what is listed gets recorded") untestable in the other direction.
    """
    name = os.environ.get("BEACON_TOOL_NAME", "").strip()
    summary = os.environ.get("BEACON_TOOL_SUMMARY", "").strip()
    # Same env var name as `tool add`'s --record: the flag is spelled --record on
    # BOTH subcommands now. It used to be --record on `add` and --recorder on
    # `used` — one concept, two spellings, inside one verb family, which is the
    # transfer mistake AX principle 1 names (independent review of PR #802).
    recorder = os.environ.get("BEACON_TOOL_RECORD", "").strip()
    target = os.environ.get("BEACON_TOOL_TARGET", "").strip()
    url = os.environ.get("BEACON_TOOL_URL", "").strip()
    force = os.environ.get("BEACON_TOOL_NO_LEDGER_CHECK", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not name:
        print("Error: 道具の名前が必要です "
              "(例: beacon tool used mcp__gmail__send_email --summary \"1 通送った\")",
              file=sys.stderr)
        sys.exit(1)

    ledger = _load()
    row = tl.lookup(ledger, name)
    effect = (row or {}).get("effect", "")
    if not recorder:
        plan = tl.record_plan(ledger, name)
        if plan["action"] == "record":
            recorder = plan["recorder"]
        elif not force:
            # Report WHICH of the three non-recording states this is. Collapsing
            # them would hide the one that is a debt (undecided) behind the two
            # that are answers (declined / unregistered).
            reasons = {
                "skip": f"記録しないと決めた道具です ({plan.get('reason')})",
                "undecided": "記録先が未判断の道具です "
                             "(`beacon tool add <道具名> --record ...` か "
                             "`--no-record \"<理由>\"` で閉じてください)",
                "unregistered": "台帳に無い道具です "
                                "(`beacon tool add <道具名> ...` で登録してください)",
            }
            msg = reasons.get(plan["action"], plan["action"])
            if json_mode:
                print(json.dumps({"recorded": False, "reason": plan["action"],
                                  "message": msg, "tool": name},
                                 ensure_ascii=False))
            else:
                print(f"記録しませんでした: {msg}", file=sys.stderr)
            # The exit code must AGREE with the body. An earlier version always
            # exited 0, so a caller checking only ``$?`` — the cheapest and most
            # common automation signal — read "recorded: false" as success
            # (independent AX review of PR #802, high).
            #
            # Which outcomes are failures is not arbitrary: ``declined`` is an
            # ANSWER (someone decided not to record this tool), so nothing went
            # wrong and 0 is correct. ``unregistered`` / ``undecided`` are DEBTS —
            # the use went unrecorded because a question is unanswered — so they
            # exit non-zero. Same three-way distinction the record sink already
            # makes, now visible at the process boundary too.
            sys.exit(0 if plan["action"] == "skip" else 1)
        else:
            recorder = tl.RECORDER_EVIDENCE

    if recorder not in tl.RECORDERS:
        print(f"Error: --record は {list(tl.RECORDERS)} のいずれかです "
              f"(指定: {recorder!r})", file=sys.stderr)
        sys.exit(1)

    text = summary or f"{name} を使用"
    if recorder == tl.RECORDER_SAVE:
        result = _record_via_save(name, text, target, url)
    else:
        result = _record_via_evidence(name, text, effect, target, url)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result.get("recorded"):
        print(f"記録: {name} → {result.get('target') or '(project)'} "
              f"[{result.get('arm') or recorder}]")
    else:
        print(f"記録しませんでした: {result.get('reason')}", file=sys.stderr)


def _record_via_evidence(name, text, effect, target, url):
    """Write through the profession-neutral evidence door (occupation.record_evidence).
    Dev records onto the milestone's declared evidence arm, sales onto the
    opportunity's — same call, dispatched by the class's declaration (e-7004)."""
    import occupation
    from commands_shared import load_project, save_project
    data = load_project()
    result = occupation.record_evidence(
        data, target, summary=text, tool=name, effect=effect,
        created_at=_now_iso(), url=url)
    if result.get("recorded"):
        save_project(data, op={"op": "tool_used", "tool": name,
                               "target": result.get("target", "")})
    return result


def _record_via_save(name, text, target, url):
    """Write through the external-artifact recorder (`beacon save`'s core path).

    Kept distinct from the evidence door because the two answer different
    questions: ``save`` records that an ARTIFACT now exists somewhere outside
    Beacon (a Drive doc, a sheet) and carries its url; evidence records that an
    ACT happened once. The ledger picks per tool, which is why both exist here
    rather than one being folded into the other."""
    import occupation
    from commands_shared import load_project, save_project
    data = load_project()
    # ``tool=name`` is the POINT of the structured field (ms-180 e-7004): the
    # question 「何でやったか」 has to be answerable by a query, not by
    # substring-matching prose. An earlier version of this function passed only
    # the ``[tool: …]`` suffix, so every ``save``-recorded use was invisible to a
    # ``meta.tool`` query while the ``evidence`` sibling was fine — found by the
    # independent AX review of PR #802. The lesson: verifying that every WRITER
    # accepts the field is not the same as verifying every CALLER passes it.
    result = occupation.record_target_entry(
        data, target, description=text, source="auto",
        date=_now_iso(), url=url, tool=name)
    if result.get("recorded"):
        save_project(data, op={"op": "tool_used", "tool": name,
                               "target": result.get("target", "")})
        return {"recorded": True, "target": result.get("target", ""),
                "arm": "entries", "via": "save"}
    return {"recorded": False, "reason": result.get("reason", ""),
            "target": target, "arm": ""}


__all__ = ["cmd_tool_add", "cmd_tool_list", "cmd_tool_show", "cmd_tool_remove",
           "cmd_tool_lookup", "cmd_tool_unregistered", "cmd_tool_used"]
