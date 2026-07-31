"""CI-side independent review judge (ms-119 / e-4143).

e-4073 landed the gate; e-4143 runs the judge IN CI so a review happens with no
AI session present. These tests pin the PURE, network-free logic — model
resolution, cold-judge prompt shaping, best-effort findings parsing, advisory
comment rendering, and the fail-closed contract (a judge that cannot run returns
ok=False so the workflow leaves the gate pending). The Anthropic call is injected
via `call_fn`, so nothing here spends a token or touches the network.

Block policy under test (SPEC 方針4): findings are advisory (rendered into a
comment), the gate blocks on "did the review run?" — so a judge that RAN returns
ok=True even with findings, and a judge that COULD NOT run returns ok=False.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review-judge-ci.py"


def _load():
    spec = importlib.util.spec_from_file_location("review_judge_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


j = _load()


def _bundle(review_type="ax", **over):
    b = {
        "review_type": review_type,
        "mode": "diff",
        "target_ref": "PR #42",
        "origin": {"id": "skills/ax-review/principles.md", "content": "原典 text"},
        "artifact": {"kind": "diff", "ref": "PR #42", "content": "diff --git a b"},
        "gaps": [],
        "external_references": [],
    }
    b.update(over)
    return b


# --- model resolution -------------------------------------------------------

def test_resolve_model_defaults_to_sonnet():
    assert j.resolve_model(_bundle()) == "claude-sonnet-4-6"


def test_resolve_model_override_alias_wins():
    assert j.resolve_model(_bundle(), override="opus") == "claude-opus-4-8"


def test_resolve_model_full_id_passthrough():
    # a full id (not an alias) is used verbatim
    assert j.resolve_model(_bundle(), override="claude-haiku-4-5") == "claude-haiku-4-5"


def test_resolve_model_honors_bundle_alias_hook():
    assert j.resolve_model(_bundle(judge_model_alias="haiku")) == "claude-haiku-4-5"


# --- prompt shaping is context-zero ----------------------------------------

def test_system_prompt_states_cold_judge_and_advisory():
    p = j.build_system_prompt("maintainability")
    assert "context-zero" in p
    assert "maintainability" in p
    assert "ADVISORY" in p  # findings do not block
    assert "did NOT write" in p


def test_user_message_is_the_bundle_verbatim():
    b = _bundle()
    msg = j.build_user_message(b)
    parsed = json.loads(msg)
    assert parsed["origin"]["content"] == "原典 text"
    assert parsed["artifact"]["content"] == "diff --git a b"


# --- findings parsing is best-effort (never raises) ------------------------

def test_parse_findings_clean_json():
    text = '{"summary": "ok", "findings": [{"severity": "high", "title": "x"}]}'
    summary, findings, ok = j.parse_findings(text)
    assert ok and summary == "ok" and findings[0]["title"] == "x"


def test_parse_findings_tolerates_fenced_and_prose():
    text = "Here you go:\n```json\n{\"summary\":\"s\",\"findings\":[]}\n```\n"
    summary, findings, ok = j.parse_findings(text)
    assert ok and summary == "s" and findings == []


def test_parse_findings_unparseable_does_not_raise():
    summary, findings, ok = j.parse_findings("total garbage, no json here")
    assert ok is False and summary == "" and findings == []


def test_parse_findings_drops_malformed_finding_entries():
    text = '{"findings": [{"severity":"low","title":"good"}, "bad", 3]}'
    _, findings, ok = j.parse_findings(text)
    assert ok and len(findings) == 1 and findings[0]["title"] == "good"


# --- run_judge: fail-closed contract ---------------------------------------

def test_run_judge_ok_when_judge_runs_even_with_findings():
    def call_fn(system, user, model):
        return '{"summary":"drift found","findings":[{"severity":"high","title":"t"}]}'
    r = j.run_judge(_bundle(), call_fn=call_fn)
    # findings present, but the review RAN → ok=True (gate flips; verdict is advisory)
    assert r["ok"] is True
    assert r["findings"] and r["model"] == "claude-sonnet-4-6"


def test_run_judge_not_ok_when_call_raises():
    def boom(system, user, model):
        raise RuntimeError("no ANTHROPIC_API_KEY")
    r = j.run_judge(_bundle(), call_fn=boom)
    assert r["ok"] is False
    assert "RuntimeError" in r["error"]
    assert r["findings"] == []


def test_run_judge_ok_even_if_reply_is_unparseable():
    # a judge that replied but not in JSON still RAN — advisory, so ok=True
    r = j.run_judge(_bundle(), call_fn=lambda s, u, m: "I think it's fine")
    assert r["ok"] is True and r["parsed_ok"] is False
    assert r["raw"] == "I think it's fine"


def test_run_judge_passes_resolved_model_to_call_fn():
    seen = {}
    def call_fn(system, user, model):
        seen["model"] = model
        return '{"findings":[]}'
    j.run_judge(_bundle(), model_override="opus", call_fn=call_fn)
    assert seen["model"] == "claude-opus-4-8"


# --- comment rendering labels itself non-blocking --------------------------

def test_render_comment_marks_advisory_and_non_blocking():
    r = {"ok": True, "model": "claude-sonnet-4-6", "summary": "s",
         "findings": [{"severity": "high", "title": "T", "where": "f.py:1", "detail": "d"}],
         "parsed_ok": True}
    md = j.render_comment("ax", "PR #42", r)
    assert "advisory" in md
    assert "ブロックしません" in md  # findings do not block the merge
    assert "[HIGH]" in md and "f.py:1" in md


def test_render_comment_clean_when_no_findings():
    r = {"ok": True, "model": "m", "summary": "", "findings": [], "parsed_ok": True}
    md = j.render_comment("maintainability", "PR #7", r)
    assert "drift" in md and "検出されませんでした" in md


def test_render_comment_surfaces_judge_failure():
    r = {"ok": False, "model": "m", "error": "RuntimeError: no key",
         "summary": "", "findings": [], "parsed_ok": False}
    md = j.render_comment("ax", "PR #1", r)
    assert "実行できませんでした" in md and "no key" in md


# --- CLI: exit code encodes the fail-closed contract -----------------------

def test_cli_judge_exit0_when_ran(tmp_path, monkeypatch, capsys):
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps(_bundle()), encoding="utf-8")
    monkeypatch.setattr(j, "_anthropic_call", lambda s, u, m: '{"findings":[]}')
    comment = tmp_path / "c.md"
    rc = j.main(["judge", "--context-file", str(ctx), "--comment-out", str(comment)])
    assert rc == 0
    assert comment.exists() and "advisory" in comment.read_text(encoding="utf-8")


def test_cli_judge_exit1_when_cannot_run(tmp_path, monkeypatch):
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps(_bundle()), encoding="utf-8")
    def boom(s, u, m):
        raise RuntimeError("no key")
    monkeypatch.setattr(j, "_anthropic_call", boom)
    rc = j.main(["judge", "--context-file", str(ctx)])
    assert rc == 1  # gate must stay pending → merge blocked
