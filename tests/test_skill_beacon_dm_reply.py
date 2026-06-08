"""Tests for the /beacon-dm-reply Skill (ms-54 follow-up).

Mirrors test_skill_beacon_dm_send: SKILL.md structural check + helpers
unit tests for the CLI composition that the Skill prose delegates to.

The reply path has its own dogfood failure modes we lock down:

  * **budget gate surprise** — sending with --in-reply-to without a
    prior `budget grant` fails. The Skill auto-grants when remaining=0.
  * **--in-reply-to forgotten** — replying without it bypasses the gate
    (= "manual mode") and disconnects the thread. The helper forces the
    flag onto every reply.
  * **cross-project replies need from_project** — replying to a DM that
    crossed projects must use --project <from_project>, not cwd.
  * **--to forgotten** — without it the dm send is dropped (post e-1209).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from beacon_cli.skills_helpers import dm_reply  # noqa: E402


# ---------------------------------------------------------------------------
# SKILL.md structural validation
# ---------------------------------------------------------------------------


SKILL_PATH = REPO / "skills" / "beacon-dm-reply.md"


def test_skill_file_exists():
    assert SKILL_PATH.exists(), (
        "skills/beacon-dm-reply.md must exist — the Skill is invoked via "
        "the markdown filename, not via a Python entry-point."
    )


def test_skill_has_well_formed_frontmatter():
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    end = body.find("\n---\n", 4)
    assert end > 0
    fm = body[4:end]
    assert "name: beacon-dm-reply" in fm
    assert "description:" in fm
    assert "/beacon-dm-reply" in fm


def test_skill_documents_key_workflow_steps():
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Step 1 — inbox listing
    assert "bus listen" in body
    # The Skill must NOT default to --auto-ack (would consume events)
    # We check that the auto-ack absence is intentional by mentioning peek
    assert "auto-ack" in body.lower() or "peek" in body.lower()
    # Step 3 — budget gate auto-handle
    assert "budget" in body.lower()
    assert "grant" in body
    # Step 5 — --in-reply-to auto-added (the helper guarantees this)
    assert "--in-reply-to" in body
    # Step 4 — cross-project handling
    assert "from_project" in body
    # Step 6 — confirmation before send
    assert "yes" in body.lower() and "cancel" in body.lower()


# ---------------------------------------------------------------------------
# Helpers: parse_inbox_events
# ---------------------------------------------------------------------------


def test_parse_inbox_handles_newline_separated_json():
    raw = (
        '{"event_id":"e-1","from_session":"s1","payload":{"text":"hi"}}\n'
        '{"event_id":"e-2","from_session":"s2","payload":{"text":"hello"}}\n'
    )
    events = dm_reply.parse_inbox_events(raw)
    assert len(events) == 2
    assert events[0]["event_id"] == "e-1"
    assert events[1]["event_id"] == "e-2"


def test_parse_inbox_handles_json_array():
    raw = '[{"event_id":"e-1"},{"event_id":"e-2"}]'
    events = dm_reply.parse_inbox_events(raw)
    assert [e["event_id"] for e in events] == ["e-1", "e-2"]


def test_parse_inbox_empty_returns_empty():
    assert dm_reply.parse_inbox_events("") == []
    assert dm_reply.parse_inbox_events("   \n  \n") == []


def test_parse_inbox_skips_malformed_lines():
    raw = (
        '{"event_id":"e-1"}\n'
        'not json\n'
        '{"event_id":"e-2"}\n'
    )
    events = dm_reply.parse_inbox_events(raw)
    assert [e["event_id"] for e in events] == ["e-1", "e-2"]


# ---------------------------------------------------------------------------
# Helpers: build_inbox_rows + render
# ---------------------------------------------------------------------------


def _event(eid: str, *, sender="s-foo", text="hi", project="proj-A",
            created="2026-06-09T15:36:12.000000Z", channel="dm"):
    return {
        "event_id": eid,
        "from_session": sender,
        "from_project": project,
        "channel": channel,
        "created_at": created,
        "payload": {"text": text},
    }


def test_build_inbox_rows_sorts_newest_first():
    events = [
        _event("e-old", created="2026-06-09T10:00:00.000000Z"),
        _event("e-new", created="2026-06-09T15:00:00.000000Z"),
        _event("e-mid", created="2026-06-09T12:00:00.000000Z"),
    ]
    rows = dm_reply.build_inbox_rows(events)
    assert [r["event_id"] for r in rows] == ["e-new", "e-mid", "e-old"]
    assert rows[0]["index"] == 1
    assert rows[-1]["index"] == 3


def test_build_inbox_rows_respects_limit():
    events = [_event(f"e-{i}", created=f"2026-06-09T15:00:{i:02d}.000000Z")
              for i in range(15)]
    rows = dm_reply.build_inbox_rows(events, limit=5)
    assert len(rows) == 5


def test_build_inbox_rows_truncates_long_text():
    long_text = "a" * 200
    rows = dm_reply.build_inbox_rows([_event("e-1", text=long_text)])
    preview = rows[0]["text_preview"]
    assert len(preview) <= 80
    assert preview.endswith("...")


def test_build_inbox_rows_collapses_newlines_in_preview():
    rows = dm_reply.build_inbox_rows([_event("e-1", text="line1\nline2\nline3")])
    assert "\n" not in rows[0]["text_preview"]


def test_render_inbox_line_includes_essentials():
    row = dm_reply.build_inbox_rows([_event("e-1", sender="abcdefghxx",
                                              text="hello")])[0]
    line = dm_reply.render_inbox_line(row)
    assert "1." in line
    assert "abcdefgh" in line  # truncated session prefix
    assert "channel=dm" in line
    assert "hello" in line


# ---------------------------------------------------------------------------
# Helpers: budget gate
# ---------------------------------------------------------------------------


def test_parse_budget_show_handles_unarmed_default():
    assert dm_reply.parse_budget_show('{"armed":false}') == {"armed": False}


def test_parse_budget_show_handles_armed_with_remaining():
    raw = '{"armed":true,"total":5,"used":2,"remaining":3}'
    b = dm_reply.parse_budget_show(raw)
    assert b["armed"] is True
    assert dm_reply.remaining_from_budget(b) == 3


def test_parse_budget_show_recovers_remaining_from_total_used():
    raw = '{"armed":true,"total":5,"used":2}'  # no remaining key
    b = dm_reply.parse_budget_show(raw)
    assert dm_reply.remaining_from_budget(b) == 3


def test_parse_budget_show_handles_garbage():
    assert dm_reply.parse_budget_show("") == {"armed": False}
    assert dm_reply.parse_budget_show("not json") == {"armed": False}


def test_needs_auto_grant_when_unarmed():
    """Failure mode (a) from dogfood: budget default state → must auto-grant."""
    assert dm_reply.needs_auto_grant({"armed": False})


def test_needs_auto_grant_when_exhausted():
    """Failure mode (b): used == total → must auto-grant."""
    assert dm_reply.needs_auto_grant(
        {"armed": True, "total": 3, "used": 3, "remaining": 0}
    )


def test_no_auto_grant_when_budget_available():
    assert not dm_reply.needs_auto_grant(
        {"armed": True, "total": 3, "used": 1, "remaining": 2}
    )


def test_build_budget_grant_cmd_default_three_turns():
    argv = dm_reply.build_budget_grant_cmd()
    assert argv == ["beacon", "bus", "budget", "grant", "--turns", "3"]


def test_build_budget_grant_cmd_rejects_zero_turns():
    with pytest.raises(ValueError):
        dm_reply.build_budget_grant_cmd(turns=0)


def test_build_budget_show_cmd_includes_json_flag():
    argv = dm_reply.build_budget_show_cmd()
    assert "--json" in argv
    assert "show" in argv


# ---------------------------------------------------------------------------
# Helpers: build_reply_cmd
# ---------------------------------------------------------------------------


def test_build_reply_cmd_same_project_no_project_flag():
    parent = _event("e-parent", sender="sender-sid", project="proj-A")
    argv = dm_reply.build_reply_cmd(
        parent_event=parent,
        text="thanks!",
        cwd_project_id="proj-A",
    )
    # --to MUST be the parent's from_session
    to_idx = argv.index("--to")
    assert argv[to_idx + 1] == "sender-sid"
    # --in-reply-to MUST be set (failure mode "forgot --in-reply-to")
    irt_idx = argv.index("--in-reply-to")
    assert argv[irt_idx + 1] == "e-parent"
    # Same project → no --project flag
    assert "--project" not in argv


def test_build_reply_cmd_cross_project_auto_adds_project_flag():
    """Failure mode (c): cross-project reply needs --project <from_project>."""
    parent = _event("e-parent", sender="sender-sid", project="proj-OTHER")
    argv = dm_reply.build_reply_cmd(
        parent_event=parent,
        text="ack",
        cwd_project_id="proj-MINE",
    )
    proj_idx = argv.index("--project")
    assert argv[proj_idx + 1] == "proj-OTHER"


def test_build_reply_cmd_rejects_parent_without_event_id():
    parent = _event("", sender="x")
    with pytest.raises(ValueError, match="event_id"):
        dm_reply.build_reply_cmd(parent_event=parent, text="hi")


def test_build_reply_cmd_rejects_parent_without_sender():
    """Can't reply to a broadcast — no concrete recipient."""
    parent = {"event_id": "e-x", "from_session": "", "channel": "dm"}
    with pytest.raises(ValueError, match="from_session"):
        dm_reply.build_reply_cmd(parent_event=parent, text="hi")


def test_build_reply_cmd_preserves_channel():
    parent = _event("e-parent", channel="dm")
    argv = dm_reply.build_reply_cmd(parent_event=parent, text="hi")
    ch_idx = argv.index("--channel")
    assert argv[ch_idx + 1] == "dm"


def test_build_reply_cmd_payload_round_trips_text():
    parent = _event("e-parent")
    text = 'Mac → Win 返信テスト\n"quoted" content'
    argv = dm_reply.build_reply_cmd(parent_event=parent, text=text)
    payload_idx = argv.index("--payload")
    payload = json.loads(argv[payload_idx + 1])
    assert payload["text"] == text


# ---------------------------------------------------------------------------
# Helpers: build_inbox_listen_cmd
# ---------------------------------------------------------------------------


def test_build_inbox_listen_cmd_omits_auto_ack():
    """Critical: peek must not consume events (other consumers may want them)."""
    argv = dm_reply.build_inbox_listen_cmd()
    assert "--auto-ack" not in argv
    assert "--once" in argv  # one-shot, not streaming
    assert "--channel" in argv and "dm" in argv


def test_build_inbox_listen_cmd_optional_recipient_and_project():
    argv = dm_reply.build_inbox_listen_cmd(
        recipient="my-session", project_id="other-proj",
    )
    assert "--recipient" in argv
    rec_idx = argv.index("--recipient")
    assert argv[rec_idx + 1] == "my-session"
    proj_idx = argv.index("--project")
    assert argv[proj_idx + 1] == "other-proj"


# ---------------------------------------------------------------------------
# End-to-end composition: reply path simulates the dogfood scenarios
# ---------------------------------------------------------------------------


def test_end_to_end_reply_unarmed_then_send():
    """Simulates today's pain point: receive DM, budget unarmed, hit reply.

    Skill flow:
      1. parse_budget_show → unarmed
      2. needs_auto_grant → True
      3. build_budget_grant_cmd → run it
      4. build_reply_cmd → run it (now budget has 3 turns)
    """
    budget = dm_reply.parse_budget_show('{"armed":false}')
    assert dm_reply.needs_auto_grant(budget)
    grant_cmd = dm_reply.build_budget_grant_cmd()
    assert grant_cmd[-1] == "3"

    parent = _event("e-parent", sender="ws-mac", project="proj-A")
    reply_cmd = dm_reply.build_reply_cmd(
        parent_event=parent,
        text="ack received",
        cwd_project_id="proj-A",
    )
    # Has all the flags that were error-prone to remember:
    assert "--to" in reply_cmd
    assert "--in-reply-to" in reply_cmd
    assert "--channel" in reply_cmd


def test_end_to_end_reply_already_armed_skips_grant():
    budget = dm_reply.parse_budget_show(
        '{"armed":true,"total":3,"used":0,"remaining":3}'
    )
    assert not dm_reply.needs_auto_grant(budget)
    assert dm_reply.remaining_from_budget(budget) == 3
