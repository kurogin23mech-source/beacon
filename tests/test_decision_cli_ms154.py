"""ms-154 e-5594 — `beacon decision record` CLI verb (log-time backstop の記録口)。

log-time backstop が「動く」ことの CLI 側検証: 環境変数インターフェース経由で
決定を組み立て、cloud mode では server の decision 書き込み口へ正しい payload を
post し、local mode では no-op、不正入力 (what / evidence 欠落) は非ゼロ終了。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_decision  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-42", "kind": decision["kind"]}


def _set_env(monkeypatch, **kw):
    for k in ("BEACON_DECISION_WHAT", "BEACON_DECISION_KIND",
              "BEACON_DECISION_RATIONALE", "BEACON_DECISION_DECIDED_BY",
              "BEACON_DECISION_EVIDENCE", "BEACON_DECISION_RELATED_TASK",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_split_evidence_drops_blanks():
    assert cmd_decision._split_evidence("a\n\n  \nb") == ["a", "b"]
    assert cmd_decision._split_evidence("") == []


def test_decided_by_vocab_is_single_source(monkeypatch):
    # ms-154 e-5652: the CLI's _DECIDED_BY and the server's DECIDED_BY are the SAME
    # object, both imported from decision_vocab. If someone re-introduces a second
    # literal definition, this identity check fails — no silent vocabulary drift.
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
    import decision_vocab
    import decision_event
    assert cmd_decision._DECIDED_BY is decision_vocab.DECIDED_BY
    assert decision_event.DECIDED_BY is decision_vocab.DECIDED_BY
    assert decision_vocab.DECIDED_BY == frozenset(
        {"autonomous-AI", "AI-proposed-human-chose", "human-delegated", "programmatic"})


def test_record_requires_what(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_EVIDENCE="commit:abc")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_requires_evidence(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="chose X")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_rejects_bad_decided_by(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc",
             BEACON_DECISION_DECIDED_BY="the-vibes")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_local_mode_is_noop(monkeypatch, capsys):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: False)
    cmd_decision.cmd_decision_record()  # no raise, no post
    assert "local mode" in capsys.readouterr().out


def test_record_posts_to_cloud(monkeypatch):
    fake = _FakeClient()
    _set_env(monkeypatch, BEACON_DECISION_WHAT="chose the additive schema",
             BEACON_DECISION_KIND="log-backstop",
             BEACON_DECISION_RATIONALE="new-field would break callers",
             BEACON_DECISION_EVIDENCE="server/decision_event.py:75\ncommit:b2a3927",
             BEACON_DECISION_RELATED_TASK="e-5591")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_decision.cmd_decision_record()
    assert len(fake.posted) == 1
    pid, rec = fake.posted[0]
    assert pid == "p1"
    assert rec["kind"] == "log-backstop"
    assert rec["decision"] == "chose the additive schema"
    assert rec["decided_by"] == "autonomous-AI"  # default
    assert rec["rationale"] == "new-field would break callers"
    assert rec["evidence"] == ["server/decision_event.py:75", "commit:b2a3927"]
    assert rec["related"] == {"task_id": "e-5591"}


def test_record_defaults_kind_to_log_backstop(monkeypatch):
    fake = _FakeClient()
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_decision.cmd_decision_record()
    assert fake.posted[0][1]["kind"] == "log-backstop"


# --- e-5595: decision list (read side for independent verification) ---

class _ListClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def list_decisions(self, project_id, *, kind="", limit=100, since="",
                       session="", target=""):
        self.calls.append((project_id, kind, limit, session, target))
        rows = [r for r in self._rows if not kind or r.get("kind") == kind]
        if session:
            rows = [r for r in rows
                    if (r.get("who") or {}).get("session_id") == session]
        if target:
            rows = [r for r in rows
                    if (r.get("related") or {}).get("target_id") == target
                    or r.get("target_id") == target]
        return {"decisions": rows, "count": len(rows)}


def test_list_local_mode_empty(monkeypatch, capsys):
    for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: False)
    cmd_decision.cmd_decision_list()
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out == {"decisions": [], "count": 0}


def test_list_cloud_json(monkeypatch, capsys):
    rows = [{"decision_id": "dec-1", "kind": "task-done", "decision": "done"}]
    client = _ListClient(rows)
    for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    cmd_decision.cmd_decision_list()
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["count"] == 1 and out["decisions"][0]["decision_id"] == "dec-1"


def test_list_forwards_session_and_target_env_to_client(monkeypatch, capsys):
    """ms-164 e-6030 (maintainability review PR#708): the env → cmd_decision_list →
    api_client seam must carry --session / --target. Guards against a future rename
    of the env key or the keyword silently dropping the filter before the HTTP call."""
    client = _ListClient([])
    for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DECISION_SESSION", "sv-test")
    monkeypatch.setenv("BEACON_DECISION_TARGET", "ms-9")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    cmd_decision.cmd_decision_list()
    # calls tuple = (project_id, kind, limit, session, target)
    assert client.calls[0] == ("p1", "", 100, "sv-test", "ms-9")


def test_list_local_mode_signals_filter_not_applied(monkeypatch, capsys):
    """ms-164 (AX review PR#708): in local mode an empty result with a filter given
    must NOT read as 'no decisions' — signal filter_applied:false."""
    for k in ("BEACON_DECISION_KIND", "BEACON_DECISION_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DECISION_SESSION", "sv-test")
    monkeypatch.delenv("BEACON_DECISION_TARGET", raising=False)
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: False)
    cmd_decision.cmd_decision_list()
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out == {"decisions": [], "count": 0, "filter_applied": False}


def test_parse_limit_defaults_and_validates():
    # ms-154 e-5649: unspecified → 100; valid int passes; non-int / non-positive exit 1.
    assert cmd_decision._parse_limit("") == 100
    assert cmd_decision._parse_limit("25") == 25
    for bad in ("abc", "1.5", "-3", "0"):
        with pytest.raises(SystemExit) as e:
            cmd_decision._parse_limit(bad)
        assert e.value.code == 1


def test_list_rejects_non_integer_limit(monkeypatch):
    # A malformed --limit must exit 1, not silently fall back to 100 (silent 破壊).
    client = _ListClient([])
    monkeypatch.setenv("BEACON_DECISION_LIMIT", "the-vibes")
    monkeypatch.delenv("BEACON_DECISION_KIND", raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_list()
    assert e.value.code == 1
    assert client.calls == []  # rejected before any server call


def test_list_passes_valid_limit(monkeypatch):
    client = _ListClient([])
    monkeypatch.setenv("BEACON_DECISION_LIMIT", "7")
    monkeypatch.delenv("BEACON_DECISION_KIND", raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    cmd_decision.cmd_decision_list()
    assert client.calls[0][2] == 7  # limit forwarded verbatim


def test_list_passes_kind_filter(monkeypatch, capsys):
    rows = [{"decision_id": "dec-1", "kind": "task-done", "decision": "done"},
            {"decision_id": "dec-2", "kind": "review-adjudication", "decision": "approve"}]
    client = _ListClient(rows)
    monkeypatch.setenv("BEACON_DECISION_KIND", "task-done")
    monkeypatch.delenv("BEACON_DECISION_LIMIT", raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    cmd_decision.cmd_decision_list()
    assert client.calls[0][1] == "task-done"


# --- e-5595: decision-verification review type registered + kernel assembles ---

def test_decision_verification_review_type_registered():
    """The plug-in review type is discovered from its on-disk descriptor
    (ms-154 e-5595 — 独立検証 harness への接続)."""
    import review_spine
    reg = review_spine.load_review_types()
    assert "decision-verification" in reg
    desc = reg["decision-verification"]
    assert desc["judge_run"] is True
    assert desc["origin"]["kind"] == "repo-file"
    assert desc["origin"]["ref"].endswith("decision-verification/principles.md")


def test_decision_audit_mode_is_valid_artifact_mode():
    import review_spine
    assert review_spine.MODE_DECISION_AUDIT in review_spine.ARTIFACT_MODES


def test_assemble_decision_verification_kernel():
    """assemble_review_context builds a valid decision-verification kernel with
    the declared decisions as the artifact (not a code diff)."""
    import review_spine
    decisions_json = '[{"decision_id":"dec-1","decision":"done","rationale":"AC met","evidence":["task:e-1"]}]'
    bundle = review_spine.assemble_review_context(
        "decision-verification",
        origin_id="skills/decision-verification/principles.md",
        origin_content="原典 body",
        diff_text="",
        mode=review_spine.MODE_DECISION_AUDIT,
        target_ref="task-done",
        artifact={"kind": "decisions", "ref": "task-done", "content": decisions_json},
    )
    assert bundle["review_type"] == "decision-verification"
    assert bundle["mode"] == "decision-audit"
    assert bundle["artifact"]["kind"] == "decisions"
    assert "dec-1" in bundle["artifact"]["content"]
    assert bundle["origin"]["id"].endswith("principles.md")
