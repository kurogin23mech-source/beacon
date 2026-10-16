"""The tool ledger's cloud mirror — a generated, read-only view (ms-180 e-7000).

The ledger's truth is the LOCAL file ``.beacon/tools.json``, because it is read on
every tool call and a network round trip on that path would tax calling any tool
at all (SPEC 方針8). But a truth that only ever exists on one machine cannot be
audited or read by another session, which is the other half of 方針8. So the
ledger syncs OUT to a document — one direction only.

One direction, and one door. The document is never read back into the ledger:
if it were, the hot path would acquire a network dependency through the back
door, and two stores would each think they were authoritative. And every write
goes through ``commands_shared.rewrite_document_body`` — ms-166 e-6937 was the
cost of a generated mirror growing a second place that could write it.

Rendering as a markdown table rather than dumping the JSON: the mirror's readers
are a person auditing "what tools is this project declared to use, and which of
them leave a record", plus an AI reading it in another worktree. Neither is
helped by raw JSON, and the machine-readable copy already exists locally.
"""

from __future__ import annotations

import tool_ledger as _tl
from commands_shared import rewrite_document_body

#: Well-known doc id of the mirror. Fixed (not slug-derived) so every project's
#: mirror is reachable by the same name and re-syncs onto itself.
MIRROR_DOC_ID = "tool-ledger"
MIRROR_TITLE = "道具台帳 (tool-ledger)"

#: ``memo`` deliberately, not ``core``: session-start reads every core doc on
#: every launch, and a generated inventory is reference material you look up, not
#: a design principle you re-read each morning. (session-start only surfaces memo
#: docs touched in the last 7 days, so a stable mirror stays quiet.)
MIRROR_SCOPE = "memo"

_BANNER = (
    "> ⚙ **この文書は自動生成です（手編集禁止）。** 道具台帳 "
    "(`.beacon/tools.json`) の写しで、`beacon tool add` / `beacon tool remove` の"
    "度に再生成されます。**真値はローカルのファイル側**で、この文書は監査と他"
    "セッションからの参照のための一方向ミラーです。ここを書き換えても台帳は"
    "変わらず、次の登録で上書きされます。道具を変えるには "
    "`beacon tool ...` を使ってください。\n"
)

_EFFECT_JA = {
    _tl.EFFECT_READ_ONLY: "読み取りだけ",
    _tl.EFFECT_STATE_CHANGE: "状態を変える",
    _tl.EFFECT_OUTWARD: "外に送る",
}

_GRAIN_JA = {
    _tl.GRAIN_EACH: "毎回",
    _tl.GRAIN_TURN: "1ターン1件",
}


def describe_record(row: dict) -> str:
    """One cell describing where a tool's use is recorded.

    The three sinks must stay *visibly* three in the mirror too. A reader who
    cannot tell 「記録しないと決めた」 from 「まだ判断していない」 cannot act on the
    second one, and the whole reason the sink is 3-valued is that the unjudged
    rows are a debt somebody has to clear (SPEC 方針4)."""
    block = row.get("record") if isinstance(row.get("record"), dict) else {}
    sink = block.get("sink")
    if sink == _tl.SINK_SPECIFIED:
        grain = _GRAIN_JA.get(block.get("grain"), str(block.get("grain") or ""))
        return f"`{block.get('recorder')}` に記録 ({grain})"
    if sink == _tl.SINK_DECLINED:
        return f"記録しない — {block.get('reason') or '(理由未記載)'}"
    return "⚠ 未判断"


def render_mirror(ledger: dict) -> str:
    """The generated document body. Pure over ``ledger`` — no I/O — so a test can
    assert the rendering without a store."""
    rows = sorted(ledger.get("tools") or [],
                  key=lambda r: str(r.get("name") or ""))
    lines = [_BANNER, "", "# 道具台帳", ""]
    if not rows:
        lines += ["登録されている道具はまだありません "
                  "(`beacon tool add <道具名> --purpose ... --effect ...`)。", ""]
        return "\n".join(lines)

    undecided = _tl.undecided_tools(ledger)
    lines += [f"登録 {len(rows)} 件"
              + (f" / うち記録先が未判断 **{len(undecided)} 件**" if undecided else "")
              + "", ""]
    lines += ["| 道具 | 用途 | 作用 | 記録先 |", "|---|---|---|---|"]
    for row in rows:
        effect = row.get("effect")
        lines.append(
            f"| `{row.get('name')}` "
            f"| {row.get('purpose') or '—'} "
            f"| {_EFFECT_JA.get(effect, str(effect or '—'))} "
            f"| {describe_record(row)} |")
    lines.append("")
    if undecided:
        lines += [
            "## 記録先が未判断の道具",
            "",
            "この道具は台帳に在るが、使ったことを証跡として残すかを誰も判断していない。"
            "「残さない」と決めたのではなく、問いが開いたまま。",
            "",
        ]
        for row in undecided:
            lines.append(
                f"- `{row.get('name')}` — "
                f"`beacon tool add {row.get('name')} --record <recorder>` か "
                f"`--no-record \"<理由>\"` で閉じる")
        lines.append("")
    return "\n".join(lines)


def sync_mirror(ledger: dict) -> bool:
    """Regenerate the mirror document from ``ledger``. Returns True when written.

    Creates the doc on first sync (``create_as``) because nothing authors a
    generated mirror by hand — unlike the application-map, which had a
    human-written v1 to take over. Best-effort by contract: the CALLER decides
    whether a failed mirror is fatal, and for ``beacon tool add`` it is not (the
    ledger is already on local disk, which is the truth)."""
    return rewrite_document_body(
        MIRROR_DOC_ID, render_mirror(ledger),
        create_as={"title": MIRROR_TITLE, "scope": MIRROR_SCOPE})


__all__ = ["MIRROR_DOC_ID", "MIRROR_TITLE", "MIRROR_SCOPE",
           "describe_record", "render_mirror", "sync_mirror"]
