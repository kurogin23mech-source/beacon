"""Beacon deadline engine — the L2 (class-abstraction) core for the rule
'対象が期日を持つ → 時間的ステータスが派生する' (= a target/work item with a
deadline derives a time-based status).

Every occupation has the same relation: a milestone has ``target_date``, a sales
opportunity has ``transition_date`` on its open gate, a sales activity has
``deadline``, a development task (ms-139) gets a ``deadline``. The *rule* for
"is this overdue?" is occupation-common; only the FIELD NAME differs per
occupation (L3). This module owns the common rule (L2); each caller keeps its
own field name.

Positioned per the capability 共有スコープ台帳 (CORE doc
``37Svg6nD2FccJM27yBjq``): L2 = "target 抽象に触れ、規則は職種共通・具象は職種
ごと". Accordingly this module is occupation-agnostic — it never imports sales /
dev concrete code, and reads a deadline off a plain dict via a tolerant
accessor. It was extracted verbatim from ``sales_entities`` (ms-107 e-3271, whose
comment predicted this extraction) into a standalone engine at ms-139 e-4948, so
``milestone.target_date`` / ``task.deadline`` become second/third consumers of
the same core alongside the original sales consumer.

Like ``work_base`` / ``work_model`` this module performs no I/O: every function
is a pure transform over the values it is handed.
"""

from __future__ import annotations

import work_model


# ---------------------------------------------------------------------------
# Temporal status vocabulary — the derived, date-relative state of a target.
# ``status`` (open/won/lost / todo/done) is orthogonal: an overdue item can
# still be ``open``/``todo``. The derived form is always correct (no resident
# process re-stamps a flag when the date passes), so it never goes stale.
# ---------------------------------------------------------------------------

TRANSITION_UNSET = "unset"          # 非terminal・期日なし (= needs a deadline)
TRANSITION_SCHEDULED = "scheduled"  # 期日が未来
TRANSITION_DUE = "due"              # 期日が今日 (= 判定日)
TRANSITION_OVERDUE = "overdue"      # 期日を過ぎ、まだ非terminal (= 超過, 抜け漏れ候補)
TRANSITION_SETTLED = "settled"      # terminal (= 決着済み / 完了)


# terminal な work item は期日に関わらず SETTLED (= もう催促しない)。done も
# cancelled も「決着済み」= overdue 集合から外れる。work_model が status 語彙の
# 単一真値源なのでそこから引く。
TERMINAL_STATUSES = frozenset({work_model.DONE_STATUS, work_model.CANCELLED_STATUS})


# 締切フィールド名は職種ごと (L3) に残す (ms-139 SPEC 方針1: 規則だけ L2 で統一、
# ストレージ名はリネームしない)。canonical ``deadline`` を優先し、legacy
# ``target_date`` (dev milestone) を fallback で読む tolerant accessor。
# activity / opportunity / task = ``deadline``、milestone = ``target_date``。
DEADLINE_KEYS = ("deadline", "target_date")


def temporal_status(due_date: str, today: str, *, settled: bool = False) -> str:
    """Pure temporal classification of a due date relative to ``today``
    (both ``YYYY-MM-DD``). Target-class generic — no entity knowledge.

    ``settled=True`` (= 決着済み / 完了) → SETTLED regardless of date. No due
    date → UNSET. Otherwise past → OVERDUE, today → DUE, future → SCHEDULED.
    ISO dates compare correctly as plain strings, so no parsing is needed.
    """
    if settled:
        return TRANSITION_SETTLED
    d = (due_date or "").strip()
    if not d:
        return TRANSITION_UNSET
    if d < today:
        return TRANSITION_OVERDUE
    if d == today:
        return TRANSITION_DUE
    return TRANSITION_SCHEDULED


def scan_overdue(items, due_date_of, settled_of, today: str) -> list:
    """Generic 締切精査 (deadline review): return ``(item, status)`` pairs whose
    temporal_status is DUE or OVERDUE, oldest due date first. ``due_date_of`` /
    ``settled_of`` are callables reading a due date / settled flag off each item,
    so any target class plugs in (opportunities via transition_date; work items
    via the default accessors below)."""
    out = []
    for it in items:
        st = temporal_status(due_date_of(it), today, settled=bool(settled_of(it)))
        if st in (TRANSITION_DUE, TRANSITION_OVERDUE):
            out.append((it, st))
    out.sort(key=lambda pair: (due_date_of(pair[0]) or ""))
    return out


# ---------------------------------------------------------------------------
# L2 accessors — plug a plain work item / target dict into the pure core above
# without the caller having to know the field name or the terminal-status set.
# ---------------------------------------------------------------------------

def deadline_of(item: dict) -> str:
    """Tolerant read of a work item / target の締切: canonical ``deadline`` 優先、
    legacy ``target_date`` (dev milestone) を fallback。無ければ ``''``。フィールド
    名は職種ごとに残し (L3)、規則だけ L2 で共通化する (ms-139 SPEC 方針1)."""
    for key in DEADLINE_KEYS:
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def is_settled(item: dict) -> bool:
    """work item / target が決着済み (``status`` ∈ TERMINAL_STATUSES) か。True の
    間は期日に関わらず overdue にならない (done / cancelled は催促対象外)."""
    return (item.get("status") or "") in TERMINAL_STATUSES


def work_item_temporal_status(item: dict, today: str) -> str:
    """L2 accessor: item の締切 (deadline / target_date) と status から
    temporal_status を導く。各クラスは自分の締切フィールドを持つだけでよい。"""
    return temporal_status(deadline_of(item), today, settled=is_settled(item))


def overdue_work_items(items, today: str) -> list:
    """DUE / OVERDUE な work item を古い順で返す ``(item, status)`` の list。
    ``scan_overdue`` の L2 便利 wrapper — 既定アクセサで締切 (deadline /
    target_date) と status(terminal) を読むので、呼び出し側はクラスを問わず
    items を渡すだけでよい (task / milestone / activity 共通)."""
    return scan_overdue(items, deadline_of, is_settled, today)


# ---------------------------------------------------------------------------
# Reminder dedup — サーバ発の締切超過リマインダ (ms-139 e-4953) が「同じ締切に
# ついて二重配信しない」ための純粋な判定。真値源はサーバの tick だが、判定規則は
# ここに置き、状態 (最後にリマインドした締切値) は work item 自身に刻む。
# ---------------------------------------------------------------------------

# 最後にリマインドした締切値を控える key。現在の締切と一致する間は再送しない。
# 締切を延ばして再び過ぎたら値が変わるので再通知される (状態は派生で stale しない)。
REMINDED_FOR_KEY = "deadline_reminded_for"


def pending_reminders(items, today: str) -> list:
    """DUE / OVERDUE な work item のうち、**現在の締切値についてまだリマインドして
    いない** ものを古い順で返す ``(item, status)`` の list — ms-139 e-4953。

    二重配信の dedup は ``item[REMINDED_FOR_KEY]`` (前回リマインドした締切値) と
    現在の ``deadline_of(item)`` の一致で判定する。値が違えば (= 新しい締切が過ぎた)
    再通知する。純粋関数 — 渡された list を読むだけで I/O も collection 直読みも
    しない。"""
    out = []
    for item, st in overdue_work_items(items, today):
        if item.get(REMINDED_FOR_KEY) == deadline_of(item):
            continue
        out.append((item, st))
    return out


def mark_reminded(item: dict) -> None:
    """work item に「現在の締切値でリマインド済み」を刻む (二重配信防止)。呼び出し側は
    通知の配信に成功した後に呼ぶ。締切が変われば次の tick で再び pending になる。"""
    item[REMINDED_FOR_KEY] = deadline_of(item)
