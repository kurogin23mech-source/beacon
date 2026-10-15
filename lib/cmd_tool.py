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
    grain = os.environ.get("BEACON_TOOL_GRAIN", "").strip() or tl.GRAIN_EACH
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
    undecided = tl.undecided_tools(ledger)
    if undecided and not only_undecided:
        print(f"\n⚠ 記録先が未判断: {len(undecided)} 件 "
              f"(`beacon tool list --undecided`)")


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
    import tool_usage_debt as debt
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if os.environ.get("BEACON_TOOL_CLEAR", "") == "1":
        removed = debt.clear_debt(debt.debt_path(_beacon_dir()))
        print(f"未登録の記録を {removed} 件消しました。" if not json_mode
              else json.dumps({"cleared": removed}, ensure_ascii=False))
        return
    rows = debt.load_debt(debt.debt_path(_beacon_dir()))
    if json_mode:
        print(json.dumps({"unregistered": rows, "count": len(rows)},
                         ensure_ascii=False))
        return
    if not rows:
        print("台帳に無い道具が使われた記録はありません。")
        return
    print(f"⚠ 台帳に無いまま使われた道具 {len(rows)} 件 "
          f"(= 使ったことが記録に残らなかった分):")
    for row in rows:
        print(f"  {row.get('name')} — {row.get('count')} 回 "
              f"(最初 {row.get('first_seen')} / 最後 {row.get('last_seen')})")
    print("\n台帳は事前に全部埋める必要はありません (端末の走査はしない方針)。"
          "使われた道具だけがここに出るので、上から順に "
          "`beacon tool add <道具名> ...` で閉じてください。")
    print("消すだけなら `beacon tool unregistered --clear`。")


__all__ = ["cmd_tool_add", "cmd_tool_list", "cmd_tool_show", "cmd_tool_remove",
           "cmd_tool_lookup", "cmd_tool_unregistered"]
