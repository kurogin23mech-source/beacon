"""Unit tests for the ms-180 tool ledger (``lib/tool_ledger.py``, e-7000).

The ledger is the declared registry of the tools a project uses: 道具 / 用途 /
作用 / 記録先 (+ grain). Its two readers are machinery that already runs on every
tool call — the ``PreToolUse`` effect gate and the ``PostToolUse`` recorder — so
the properties pinned here are the ones those readers depend on:

  * the record sink stays THREE-valued (specified / declined / undecided), and
    ``undecided`` never degrades into "decided not to record" (SPEC 方針4),
  * a declaration cannot SOFTEN a pinned built-in's effect, enforced on BOTH the
    write path and the read path (two writers reach this file: the CLI and a text
    editor — a guard covering only one is not a guard),
  * an unreadable / corrupt ledger yields NO declarations rather than wrong ones,
  * lookup is exact-match, so no reader can be satisfied by a coincidence.

Each test states the failure it prevents, not just the behaviour it observes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import tool_ledger as tl  # noqa: E402


def _ledger(*rows: dict) -> dict:
    led = tl.empty_ledger()
    for row in rows:
        tl.upsert(led, row)
    return led


# ---------------------------------------------------------------------------
# The 3-valued record sink.
# ---------------------------------------------------------------------------

def test_naming_a_recorder_yields_specified_and_a_record_plan():
    row = tl.build_record("mcp__gmail__send_email", purpose="顧客にメールを送る",
                          effect=tl.EFFECT_OUTWARD, recorder=tl.RECORDER_EVIDENCE)
    assert row["record"]["sink"] == tl.SINK_SPECIFIED
    plan = tl.record_plan(_ledger(row), "mcp__gmail__send_email")
    assert plan == {"action": "record", "recorder": tl.RECORDER_EVIDENCE,
                    "grain": tl.GRAIN_EACH}


def test_declining_requires_a_reason_so_it_stays_distinct_from_undecided():
    """``declined`` without a reason is indistinguishable on disk from
    ``undecided`` — the 3-value split would silently collapse back to 2."""
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("Grep", purpose="下調べ", effect=tl.EFFECT_READ_ONLY,
                        sink=tl.SINK_DECLINED)


def test_registering_without_a_decision_is_undecided_not_skip():
    """The failure this prevents: a row nobody has judged yet reading as a
    decision not to record, so the gap closes silently (SPEC 方針4)."""
    row = tl.build_record("mcp__slack-ga__channels_list",
                          purpose="チャンネル一覧を見る",
                          effect=tl.EFFECT_READ_ONLY)
    assert row["record"]["sink"] == tl.SINK_UNDECIDED
    assert tl.record_plan(_ledger(row), "mcp__slack-ga__channels_list") == {
        "action": "undecided"}


def test_undecided_tools_lists_exactly_the_unjudged_rows():
    led = _ledger(
        tl.build_record("a", purpose="p", effect=tl.EFFECT_READ_ONLY,
                        recorder=tl.RECORDER_SAVE),
        tl.build_record("b", purpose="p", effect=tl.EFFECT_READ_ONLY,
                        decline_reason="証跡ではない"),
        tl.build_record("c", purpose="p", effect=tl.EFFECT_READ_ONLY),
    )
    assert [r["name"] for r in tl.undecided_tools(led)] == ["c"]


def test_recorder_and_decline_cannot_both_be_given():
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("x", purpose="p", effect=tl.EFFECT_READ_ONLY,
                        recorder=tl.RECORDER_SAVE, decline_reason="なぜか")


def test_specified_sink_naming_an_unknown_recorder_reports_as_debt_not_record():
    """A hand-written row claiming ``specified`` but naming a recorder that does
    not exist must not read as "recording happens" — nothing would be written and
    nobody would know. It surfaces as undecided (= debt) instead."""
    led = {"version": 1, "tools": [
        {"name": "t", "effect": tl.EFFECT_OUTWARD,
         "record": {"sink": tl.SINK_SPECIFIED, "recorder": "nonexistent",
                    "grain": tl.GRAIN_EACH}}]}
    assert tl.record_plan(led, "t") == {"action": "undecided"}
    assert [r["name"] for r in tl.undecided_tools(led)] == ["t"]


# ---------------------------------------------------------------------------
# The non-softenable effect guard — both writers.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pinned", sorted(tl.NON_SOFTENABLE_EFFECT))
def test_write_path_refuses_declaring_a_pinned_builtin_read_only(pinned):
    """If the ledger could declare ``Bash`` read-only, registering one row would
    disarm the ms-169 approval gate for the widest side-effect surface there is."""
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record(pinned, purpose="p", effect=tl.EFFECT_READ_ONLY)


@pytest.mark.parametrize("pinned", sorted(tl.NON_SOFTENABLE_EFFECT))
def test_read_path_ignores_a_hand_edited_softening(pinned):
    """The file is a file: the CLI is one writer, a text editor is another. The
    read path must refuse what the write path refuses (ms-174's lesson —
    enumerate every writer to the state you protect)."""
    led = {"version": 1, "tools": [
        {"name": pinned, "effect": tl.EFFECT_READ_ONLY,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_EACH}}]}
    assert tl.effect_of(led, pinned) is None


def test_hardening_a_pinned_builtin_is_allowed():
    """The guard is one-directional: declaring a pinned tool MORE dangerous than
    the heuristic guessed must stay possible, or the ledger could never tighten."""
    row = tl.build_record("Bash", purpose="シェルを実行する",
                          effect=tl.EFFECT_OUTWARD)
    assert tl.effect_of(_ledger(row), "Bash") == tl.EFFECT_OUTWARD


def test_is_softening_only_fires_on_read_only_for_pinned_names():
    assert tl.is_softening("Bash", tl.EFFECT_READ_ONLY) is True
    assert tl.is_softening("Bash", tl.EFFECT_STATE_CHANGE) is False
    assert tl.is_softening("mcp__x__y", tl.EFFECT_READ_ONLY) is False


# ---------------------------------------------------------------------------
# Vocabulary is closed.
# ---------------------------------------------------------------------------

def test_unknown_effect_is_refused():
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("x", purpose="p", effect="maybe-writes")


def test_unknown_grain_is_refused():
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("x", purpose="p", effect=tl.EFFECT_READ_ONLY,
                        grain="hourly")


def test_unknown_recorder_is_refused_at_write_time():
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("x", purpose="p", effect=tl.EFFECT_OUTWARD,
                        recorder="webhook")


def test_explicit_sink_contradicting_the_arguments_is_refused():
    """Passing ``sink=undecided`` while also naming a recorder is a caller bug;
    storing it would make the row's own two fields disagree."""
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("x", purpose="p", effect=tl.EFFECT_OUTWARD,
                        recorder=tl.RECORDER_SAVE, sink=tl.SINK_UNDECIDED)


def test_effect_outside_the_vocabulary_on_disk_yields_no_declaration():
    led = {"version": 1, "tools": [{"name": "t", "effect": "whatever"}]}
    assert tl.effect_of(led, "t") is None


# ---------------------------------------------------------------------------
# Lookup is exact — no coincidental match.
# ---------------------------------------------------------------------------

def test_lookup_does_not_match_on_prefix_or_substring():
    led = _ledger(tl.build_record("mcp__gmail__send_email", purpose="p",
                                  effect=tl.EFFECT_OUTWARD))
    assert tl.lookup(led, "mcp__gmail__send_email") is not None
    for near_miss in ("mcp__gmail__send", "send_email",
                      "mcp__gmail__send_email_v2", "MCP__GMAIL__SEND_EMAIL"):
        assert tl.lookup(led, near_miss) is None, near_miss


def test_unregistered_is_its_own_action_distinct_from_skip():
    """``PreToolUse`` must be able to tell "declared not to record" from "nobody
    declared this tool at all" — they are different debts."""
    led = _ledger(tl.build_record("a", purpose="p", effect=tl.EFFECT_READ_ONLY,
                                  decline_reason="証跡ではない"))
    assert tl.record_plan(led, "a")["action"] == "skip"
    assert tl.record_plan(led, "b") == {"action": "unregistered"}


# ---------------------------------------------------------------------------
# Persistence: a corrupt ledger yields NO declarations, never wrong ones.
# ---------------------------------------------------------------------------

def test_missing_file_is_an_empty_ledger_not_an_error(tmp_path):
    assert tl.load_ledger(str(tmp_path / "nope.json")) == tl.empty_ledger()


@pytest.mark.parametrize("junk", ["not json at all", "[]", '{"tools": 3}',
                                  '{"tools": [{"no_name": 1}]}'])
def test_corrupt_or_wrong_shaped_file_declares_nothing(tmp_path, junk):
    path = tmp_path / "tools.json"
    path.write_text(junk, encoding="utf-8")
    led = tl.load_ledger(str(path))
    assert led["tools"] == []
    assert tl.effect_of(led, "Bash") is None
    assert tl.record_plan(led, "anything") == {"action": "unregistered"}


def test_round_trip_preserves_every_declared_field(tmp_path):
    path = str(tmp_path / "tools.json")
    rows = [
        tl.build_record("mcp__gmail__send_email", purpose="顧客にメールを送る",
                        effect=tl.EFFECT_OUTWARD, recorder=tl.RECORDER_EVIDENCE,
                        grain=tl.GRAIN_EACH, at="2026-10-15T00:00:00Z",
                        by="tester"),
        tl.build_record("mcp__gmail__search_emails", purpose="メールを確認する",
                        effect=tl.EFFECT_READ_ONLY, recorder=tl.RECORDER_EVIDENCE,
                        grain=tl.GRAIN_TURN),
        tl.build_record("Grep", purpose="コードを下調べする",
                        effect=tl.EFFECT_READ_ONLY,
                        decline_reason="下調べは証跡ではない"),
    ]
    tl.save_ledger(path, _ledger(*rows))
    back = tl.load_ledger(path)
    assert sorted(back["tools"], key=lambda r: r["name"]) == \
        sorted(rows, key=lambda r: r["name"])
    assert json.loads(Path(path).read_text(encoding="utf-8"))["version"] == \
        tl.LEDGER_VERSION


def test_save_is_atomic_leaving_no_temp_file_behind(tmp_path):
    path = str(tmp_path / "tools.json")
    tl.save_ledger(path, tl.empty_ledger())
    assert not (tmp_path / "tools.json.tmp").exists()


def test_reading_the_ledger_performs_no_network_io(tmp_path, monkeypatch):
    """SPEC 受入条件 (e-7000 #5): the ledger is read on EVERY tool call, so the
    read path must not reach the network. Pinned by making any socket fatal."""
    import socket
    path = str(tmp_path / "tools.json")
    tl.save_ledger(path, _ledger(tl.build_record(
        "mcp__gmail__send_email", purpose="p", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE)))

    def _boom(*a, **k):
        raise AssertionError("ledger read attempted network I/O")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    led = tl.load_ledger(path)
    assert tl.effect_of(led, "mcp__gmail__send_email") == tl.EFFECT_OUTWARD
    assert tl.record_plan(led, "mcp__gmail__send_email")["action"] == "record"


# ---------------------------------------------------------------------------
# upsert / remove keep the ledger keyed by name.
# ---------------------------------------------------------------------------

def test_re_registering_replaces_rather_than_duplicating():
    """Two rows for one name would make every reader's answer depend on list
    order — the readers are a gate and a recorder, so that is not acceptable."""
    led = _ledger(tl.build_record("t", purpose="古い", effect=tl.EFFECT_READ_ONLY))
    tl.upsert(led, tl.build_record("t", purpose="新しい",
                                   effect=tl.EFFECT_OUTWARD,
                                   recorder=tl.RECORDER_SAVE))
    assert len(led["tools"]) == 1
    assert tl.lookup(led, "t")["purpose"] == "新しい"
    assert tl.effect_of(led, "t") == tl.EFFECT_OUTWARD


def test_remove_reports_whether_a_row_was_actually_dropped():
    led = _ledger(tl.build_record("t", purpose="p", effect=tl.EFFECT_READ_ONLY))
    assert tl.remove(led, "t") is True
    assert tl.remove(led, "t") is False


# ---------------------------------------------------------------------------
# 用途 lookup (e-7006).
# ---------------------------------------------------------------------------

def test_find_by_purpose_ranks_by_shared_tokens():
    led = _ledger(
        tl.build_record("mcp__gmail__search_emails", purpose="顧客からのメールを確認する",
                        effect=tl.EFFECT_READ_ONLY),
        tl.build_record("mcp__outlook__list_emails", purpose="社内メールを確認する",
                        effect=tl.EFFECT_READ_ONLY),
        tl.build_record("Grep", purpose="コードを検索する", effect=tl.EFFECT_READ_ONLY),
    )
    hits = [r["name"] for r in tl.find_by_purpose(led, "メールを確認する")]
    assert "Grep" not in hits
    assert set(hits) == {"mcp__gmail__search_emails", "mcp__outlook__list_emails"}


def test_find_by_purpose_matches_camel_and_snake_spellings_alike():
    """The NAME is matched too, not only the 用途, so a camelCase tool is findable
    by its snake_case spelling. (A blank purpose is no longer registrable — see
    ``test_a_blank_purpose_is_refused`` — so this row carries one.)"""
    led = _ledger(tl.build_record("mcp__drive__createGoogleDoc",
                                  purpose="資料を保管する",
                                  effect=tl.EFFECT_STATE_CHANGE))
    assert [r["name"] for r in tl.find_by_purpose(led, "create google doc")] == \
        ["mcp__drive__createGoogleDoc"]


def test_find_by_purpose_with_an_empty_query_returns_nothing():
    led = _ledger(tl.build_record("t", purpose="p", effect=tl.EFFECT_READ_ONLY))
    assert tl.find_by_purpose(led, "") == []
    assert tl.find_by_purpose(led, "   ") == []



# ---------------------------------------------------------------------------
# 用途 is required (e-7006 受入条件2) — the ambiguous state is made unreachable
# rather than given a third value. Every tool is used FOR something, so a blank
# 用途 is always an omission, never a meaningful "no purpose".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_a_blank_purpose_is_refused(blank):
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record("t", purpose=blank, effect=tl.EFFECT_READ_ONLY)


def test_a_hand_edited_blank_purpose_is_reported_on_read():
    """The write rule covers one of two writers. A rule enforced on only the CLI
    is not enforced (ms-174), so the read side reports what it cannot prevent."""
    led = {"version": 1, "tools": [
        {"name": "old-row", "purpose": "", "effect": tl.EFFECT_READ_ONLY,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_EACH}},
        {"name": "fine", "purpose": "ちゃんと書いてある",
         "effect": tl.EFFECT_READ_ONLY,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_EACH}}]}
    assert [r["name"] for r in tl.purposeless_tools(led)] == ["old-row"]


# ---------------------------------------------------------------------------
# The grain default is RECORDED, not just taken (e-7014 受入条件4).
# ---------------------------------------------------------------------------

def test_an_omitted_grain_is_marked_as_defaulted():
    """"nobody chose a grain" and "someone chose 毎回" must not be the same bytes
    on disk — the same collapse the 3-valued sink exists to prevent."""
    row = tl.build_record("t", purpose="p", effect=tl.EFFECT_READ_ONLY)
    assert row["record"]["grain"] == tl.GRAIN_EACH
    assert row["record"]["grain_source"] == "default"


def test_an_explicit_grain_is_marked_as_declared():
    for grain in tl.GRAINS:
        row = tl.build_record("t", purpose="p", effect=tl.EFFECT_READ_ONLY,
                              grain=grain)
        assert row["record"]["grain_source"] == "declared"
        assert row["record"]["grain"] == grain


def test_grain_defaulted_tools_lists_exactly_the_unchosen_rows():
    led = _ledger(
        tl.build_record("chosen", purpose="p", effect=tl.EFFECT_READ_ONLY,
                        grain=tl.GRAIN_TURN),
        tl.build_record("unchosen", purpose="p", effect=tl.EFFECT_READ_ONLY))
    assert [r["name"] for r in tl.grain_defaulted_tools(led)] == ["unchosen"]


def test_a_row_predating_grain_source_counts_as_defaulted():
    """Claiming an older row's grain was DECLARED would be an invention — the
    choice was not recorded then, so "not declared" is the only honest reading."""
    led = {"version": 1, "tools": [
        {"name": "legacy", "purpose": "p", "effect": tl.EFFECT_READ_ONLY,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_TURN}}]}
    assert [r["name"] for r in tl.grain_defaulted_tools(led)] == ["legacy"]


# ---------------------------------------------------------------------------
# A declaration may not contradict POSITIVE evidence (e-7003 受入条件3).
#
# The guard grew beyond the 8 pinned built-ins: a name carrying a WRITE VERB is
# positive evidence of effect, so 「読み取りだけ」 about it is a detectable lie.
# What stays declarable is the case the ledger exists for — a name with no
# recognisable verb, which the heuristic can only default-deny.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "mcp__gmail__send_email", "mcp__drive__createGoogleDoc",
    "mcp__slack__post_message", "mcp__sf__dml_records",
    "mcp__drive__deleteItem", "mcp__cal__update-event",
])
def test_a_write_verb_name_cannot_be_declared_read_only(name):
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record(name, purpose="p", effect=tl.EFFECT_READ_ONLY)
    led = {"version": 1, "tools": [
        {"name": name, "purpose": "p", "effect": tl.EFFECT_READ_ONLY,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_EACH}}]}
    assert tl.effect_of(led, name) is None, "read side must refuse it too"


@pytest.mark.parametrize("name", [
    "mcp__weird__frobnicate", "mcp__x__thing", "mcp__y__zzz",
])
def test_a_verbless_name_remains_declarable_read_only(name):
    """This is the ledger's whole point: the heuristic default-denies an
    unrecognised name, and a declaration is allowed to replace that GUESS. If
    this went away, the ledger could only ever add gating."""
    row = tl.build_record(name, purpose="状態を見るだけ",
                          effect=tl.EFFECT_READ_ONLY)
    assert tl.effect_of(_ledger(row), name) == tl.EFFECT_READ_ONLY


@pytest.mark.parametrize("name", [
    "mcp__gmail__send_email", "mcp__drive__createGoogleDoc", "Bash", "Write",
])
def test_declaring_a_write_verb_name_MORE_dangerous_is_allowed(name):
    """One-directional: tightening must always be possible."""
    for effect in (tl.EFFECT_STATE_CHANGE, tl.EFFECT_OUTWARD):
        row = tl.build_record(name, purpose="p", effect=effect)
        assert tl.effect_of(_ledger(row), name) == effect


def test_a_read_verb_name_is_not_treated_as_positive_write_evidence():
    """Over-reach check: if the guard fired on read names too, declaring a
    genuinely read-only tool would be impossible and the feature would be dead."""
    for name in ("mcp__gmail__search_emails", "mcp__x__list_things",
                 "mcp__y__get_status"):
        assert tl.is_softening(name, tl.EFFECT_READ_ONLY) is False


def test_the_write_verb_check_looks_at_the_tool_segment_not_the_server():
    """A server whose NAME contained a write verb would otherwise make every one
    of its read tools undeclarable."""
    import tool_effect as te
    assert te.has_write_verb("mcp__sender-service__get_status") is False
    assert te.has_write_verb("mcp__x__send_thing") is True


def test_the_write_verb_veto_declares_that_it_narrows_e7003_ac1():
    """A neutral audit of this milestone's adjudications found the decline's
    justification one-sided: e-7003 AC3's supporting clause says 「…形も考える」
    ("also consider"), not a requirement, while the same task's AC1 says
    「名前の形から推測しない」 — which clause 2 of ``is_softening`` contradicts for
    write-verb names.

    The decision stands (positive evidence beats absence of evidence, and SPEC
    受入条件6 is the stronger constraint), but the tension must be WRITTEN DOWN
    rather than implied, so the next reader does not have to rediscover that the
    ledger's promise has an exception. This pins the disclosure, because the
    whole failure mode here was a claim reading as more absolute than the code.
    """
    doc = tl.is_softening.__doc__ or ""
    assert "DECLARED TENSION" in doc
    assert "e-7003" in doc, "the AC it narrows must be named"
    # The doc DOES still mention e-7004 — as the correction of its own earlier
    # miscitation, which is history worth keeping. What must not come back is the
    # miscitation USED AS the rule's source.
    assert "e-7004 受入条件3)" not in doc, "the wrong-task citation is back"
    assert "wrong task" in doc or "誤り" in doc, (
        "the miscitation should be recorded as corrected, not silently dropped")
    assert "も考える" in doc, "the AC's soft wording must be quoted, not paraphrased"
    assert "受入条件1" in doc, "the AC it contradicts must be named"
    # the residual cost must be stated, not just the justification
    assert "read-only" in doc and ("過剰" in doc or "gated" in doc)
