"""Pending DM action summary helper pin (ms-70 / e-1714).

These tests pin the **structural** behaviour of
``lib/dm_pending.format_pending_dm_summary`` which the
``/beacon-session-start`` Skill (= markdown) calls to render its
"保留中 DM action" banner section.

The Skill itself is markdown and therefore unrenderable / untestable on
its own. Pushing the format logic into a pure Python helper lets us pin
three AC items from e-1714:

(a) Empty list → empty string (no header noise when sidecar has 0
    pending rows; the Skill ``if section: emit(section)`` swallows the
    section entirely).
(b) Multiple pending rows → full summary that includes:
    - header with the row count
    - one bullet per row (event_id / sender / created_at)
    - trailing hint pointing at ``beacon dm show <event_id>`` and
      ``beacon dm respond approve|deny <event_id>`` (= the e-1716 CLI
      primitive case the helper hands off to).
(c) Decided (approved / denied) sidecar rows are filtered out so a
    stale audit row never leaks into the banner even if a caller hands
    the helper a raw mixed list.
"""

from __future__ import annotations

import os
import sys

# Make lib/ importable without packaging gymnastics — same pattern as
# tests/test_envelope_approval_schema.py uses for server/.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lib")
)

import dm_pending  # noqa: E402  (sys.path manipulation must run first)


# ---------------------------------------------------------------------------
# AC (a): empty / None inputs produce an empty string
# ---------------------------------------------------------------------------

def test_empty_list_returns_empty_string():
    """0 件 → 完全に空文字。Skill 側で `if section:` で section ごと省略させる契約。"""
    assert dm_pending.format_pending_dm_summary([]) == ""


def test_none_input_returns_empty_string():
    """None も空文字に正規化 (= endpoint が 200 / null を返した時の defense)。"""
    assert dm_pending.format_pending_dm_summary(None) == ""


def test_all_non_pending_rows_return_empty_string():
    """approved/denied/auto しか無いリストは pending 0 件と同義 → 空文字。

    server の ``list_pending_approvals`` は status=pending で query するが、
    呼び出し側が混在リストを誤って渡しても、helper 側で belt-and-suspenders
    で filter する (= ms-70 / e-1714 設計 c)。
    """
    mixed = [
        {"event_id": "ev-1", "approval_status": "approved",
         "sender_user_id": "u-a", "created_at": "2026-06-17T10:00:00.000000Z"},
        {"event_id": "ev-2", "approval_status": "denied",
         "sender_user_id": "u-a", "created_at": "2026-06-17T11:00:00.000000Z"},
        {"event_id": "ev-3", "approval_status": "auto",
         "sender_user_id": "u-a", "created_at": "2026-06-17T12:00:00.000000Z"},
    ]
    assert dm_pending.format_pending_dm_summary(mixed) == ""


# ---------------------------------------------------------------------------
# AC (b): multiple pending rows produce a full summary
# ---------------------------------------------------------------------------

def test_multiple_pending_rows_render_full_summary():
    """複数の pending sidecar 行 → ヘッダ + 行ごと bullet + 詳細ヒント。

    ヘッダ・hint は module 定数 (= PENDING_DM_HEADER / PENDING_DM_DETAIL_HINT)
    を参照することで「文字列を 1 箇所にしか書かない」契約を維持。
    """
    rows = [
        {"event_id": "ev-100", "approval_status": "pending",
         "sender_user_id": "user-alice",
         "receiver_user_id": "user-bob",
         "created_at": "2026-06-17T09:00:00.000000Z"},
        {"event_id": "ev-101", "approval_status": "pending",
         "sender_user_id": "user-carol",
         "receiver_user_id": "user-bob",
         "created_at": "2026-06-17T09:30:00.000000Z"},
    ]
    out = dm_pending.format_pending_dm_summary(rows)
    # header with count
    assert dm_pending.PENDING_DM_HEADER in out
    assert "2 件" in out
    # each event_id / sender / time present
    assert "ev-100" in out
    assert "user-alice" in out
    assert "2026-06-17T09:00:00.000000Z" in out
    assert "ev-101" in out
    assert "user-carol" in out
    assert "2026-06-17T09:30:00.000000Z" in out
    # detail hint (beacon dm show / beacon dm respond) present
    assert dm_pending.PENDING_DM_DETAIL_HINT in out
    assert "beacon dm show" in out
    assert "beacon dm respond" in out


def test_detail_hint_can_be_suppressed():
    """``detail_hint=False`` の時は hint 行のみ落ちる (本体出力は維持)。

    将来 ``--brief`` モードなどで hint を抑止する余地を contract 化。
    body 部 (= ヘッダ + 行) は detail_hint に依らない。
    """
    rows = [
        {"event_id": "ev-1", "approval_status": "pending",
         "sender_user_id": "u-a", "created_at": "2026-06-17T00:00:00.000000Z"},
    ]
    with_hint = dm_pending.format_pending_dm_summary(rows, detail_hint=True)
    without = dm_pending.format_pending_dm_summary(rows, detail_hint=False)
    assert dm_pending.PENDING_DM_DETAIL_HINT in with_hint
    assert dm_pending.PENDING_DM_DETAIL_HINT not in without
    # body content stays identical regardless of hint flag
    assert "ev-1" in without
    assert dm_pending.PENDING_DM_HEADER in without


# ---------------------------------------------------------------------------
# AC (c): decided rows do not leak when mixed with pending rows
# ---------------------------------------------------------------------------

def test_mixed_pending_and_decided_filters_decided_out():
    """pending + approved + denied 混在 → pending だけが出力に残る。

    server の list_pending_approvals は既に status=pending で filter するが、
    将来「全 sidecar 行」を返す debug endpoint 等から helper を直接叩く経路を
    想定した defense-in-depth。
    """
    rows = [
        {"event_id": "ev-pending", "approval_status": "pending",
         "sender_user_id": "u-a", "created_at": "2026-06-17T08:00:00.000000Z"},
        {"event_id": "ev-approved", "approval_status": "approved",
         "sender_user_id": "u-a", "created_at": "2026-06-17T07:00:00.000000Z",
         "decision_by": "u-b", "decision_at": "2026-06-17T07:05:00.000000Z"},
        {"event_id": "ev-denied", "approval_status": "denied",
         "sender_user_id": "u-c", "created_at": "2026-06-17T06:00:00.000000Z",
         "decision_by": "u-b", "decision_at": "2026-06-17T06:05:00.000000Z"},
    ]
    out = dm_pending.format_pending_dm_summary(rows)
    # only the pending row appears
    assert "ev-pending" in out
    assert "ev-approved" not in out
    assert "ev-denied" not in out
    # count reflects only the pending row, not the input size
    assert "1 件" in out


def test_filter_pending_only_returns_subset():
    """``filter_pending_only`` も単独で contract 検証可能。

    Skill が直接 endpoint を bypass して helper だけ使う将来の経路向け。
    """
    rows = [
        {"event_id": "ev-1", "approval_status": "pending"},
        {"event_id": "ev-2", "approval_status": "approved"},
        {"event_id": "ev-3", "approval_status": "pending"},
        {"event_id": "ev-4", "approval_status": "auto"},
    ]
    pending = dm_pending.filter_pending_only(rows)
    assert [r["event_id"] for r in pending] == ["ev-1", "ev-3"]


# ---------------------------------------------------------------------------
# Defensive: missing fields degrade gracefully (no KeyError, no crash)
# ---------------------------------------------------------------------------

def test_missing_fields_render_unknown_markers():
    """schema-incomplete な row (= 過去 migration / debug 書き込み) でも
    helper が落ちないこと。""(unknown)" 等の placeholder で degrade する。"""
    rows = [
        {"approval_status": "pending"},  # event_id / sender / time すべて欠落
    ]
    out = dm_pending.format_pending_dm_summary(rows)
    assert "1 件" in out
    assert "(unknown)" in out or "(unknown sender)" in out \
        or "(unknown time)" in out
