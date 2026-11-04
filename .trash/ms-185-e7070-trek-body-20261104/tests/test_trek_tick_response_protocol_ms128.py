"""ms-128 方針1 (e-4372): typed tick-response protocol.

A tick response is an action that advances the Target. "Waiting" (no-op / "")
is not a valid response — it is classified as ``no-response`` and handed to
server intervention, exactly as if no pulse had arrived. These tests pin:

  1. classify_tick_response maps the 4 advancing actions → "action", the
     waiting tokens → "no-response", and unknown tokens → raise.
  2. record_pulse_ack stamps ``response_class`` on every record + the
     session summary, so the server tick can mechanically distinguish
     "advanced the Target" from "sat here" without re-parsing the token.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import datetime  # noqa: E402

import trek  # noqa: E402
import trek_scheduler  # noqa: E402


def _now():
    return datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize("action", ["continue", "terminal", "dm-leader", "dm-peer"])
def test_advancing_actions_classify_as_action(action):
    assert trek.classify_tick_response(action) == trek.TICK_RESPONSE_CLASS_ACTION


@pytest.mark.parametrize("waiting", ["no-op", "", "  "])
def test_waiting_tokens_classify_as_no_response(waiting):
    assert (
        trek.classify_tick_response(waiting)
        == trek.TICK_RESPONSE_CLASS_NO_RESPONSE
    )


def test_unknown_token_raises():
    with pytest.raises(ValueError):
        trek.classify_tick_response("wait-please")


def test_record_pulse_ack_stamps_action_class():
    doc: dict = {}
    trek.record_pulse_ack(doc, session_id="sv-1", picked_choice="continue")
    entry = doc["pulse_acks"]["sv-1"]
    assert entry["last_response_class"] == trek.TICK_RESPONSE_CLASS_ACTION
    assert entry["history"][-1]["response_class"] == "action"


def test_record_pulse_ack_classifies_no_op_as_no_response():
    # A session that responds "no-op" is recorded (observability stays
    # honest) but its response does NOT count as compliance — it is a
    # no-response that the server tick will hand to intervention.
    doc: dict = {}
    trek.record_pulse_ack(doc, session_id="sv-2", picked_choice="no-op")
    entry = doc["pulse_acks"]["sv-2"]
    assert entry["last_response_class"] == trek.TICK_RESPONSE_CLASS_NO_RESPONSE
    assert entry["history"][-1]["response_class"] == "no-response"


def test_record_pulse_ack_empty_choice_is_no_response():
    doc: dict = {}
    trek.record_pulse_ack(doc, session_id="sv-3", picked_choice="")
    assert (
        doc["pulse_acks"]["sv-3"]["last_response_class"]
        == trek.TICK_RESPONSE_CLASS_NO_RESPONSE
    )


def test_no_op_is_not_in_advancing_actions():
    # Structural guard: no-op must never be an advancing action.
    assert "no-op" not in trek.TICK_RESPONSE_ACTIONS
    assert set(trek.TICK_RESPONSE_ACTIONS) == {
        "continue", "terminal", "dm-leader", "dm-peer",
    }


# ---------------------------------------------------------------------------
# ms-128 方針2 (e-4364) — tick 文面を「進捗確認」から「前進指示」+ telos へ。
# ---------------------------------------------------------------------------

def test_tick_header_is_advance_frame_not_audit():
    # AC3: the header frames the tick as an advance instruction, not a
    # status-check ("進捗確認" primes reporting; "前進" primes action).
    t = {"trek_id": "tk-a", "scope": []}
    body = trek_scheduler.build_progress_check_payload(t, now=_now())["body"]
    assert "前進指示" in body
    assert "進捗確認" not in body


def test_tick_body_injects_telos_every_case():
    telos_marker = "前進させること"
    # empty scope
    empty = trek_scheduler.build_progress_check_payload(
        {"trek_id": "tk-b", "scope": []}, now=_now())["body"]
    # all-done
    all_done = trek_scheduler.build_progress_check_payload(
        {"trek_id": "tk-c", "scope": [{"project": "p", "milestone": "ms-1"}]},
        project_data={"entries": [
            {"id": "e-1", "type": "task", "status": "done", "milestone": "ms-1"}]},
        now=_now())["body"]
    # happy path (a todo present)
    happy = trek_scheduler.build_progress_check_payload(
        {"trek_id": "tk-d", "scope": [{"project": "p", "milestone": "ms-1"}]},
        project_data={"entries": [
            {"id": "e-9", "type": "task", "status": "todo", "milestone": "ms-1"}]},
        now=_now())["body"]
    for body in (empty, all_done, happy):
        assert telos_marker in body


def test_tick_fallbacks_do_not_instruct_waiting():
    # 方針2: scope 空 / 全 done でも「待ってください」と待機を指示しない。
    empty = trek_scheduler.build_progress_check_payload(
        {"trek_id": "tk-e", "scope": []}, now=_now())["body"]
    all_done = trek_scheduler.build_progress_check_payload(
        {"trek_id": "tk-f", "scope": [{"project": "p", "milestone": "ms-1"}]},
        project_data={"entries": [
            {"id": "e-1", "type": "task", "status": "done", "milestone": "ms-1"}]},
        now=_now())["body"]
    # The old waiting instruction wording must be gone.
    assert "次の cadence を待って" not in empty
    assert "待機せず" in empty
    assert "待機せず" in all_done
