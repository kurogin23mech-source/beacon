"""ms-159 / e-6534 — drift guard: the WIRED Stop hook must write context_pct.

The context-usage monitor had two implementations kept in parallel: the bash
``bin/context-usage-monitor.sh`` (the one actually wired as Claude Code's Stop
hook) and the Python ``beacon_cli/hooks/context_monitor.py``. e-6499 taught the
Python side to persist ``context_pct`` (the roster's context% badge value) into
``.claude/context-usage-state.json``, but the bash file was missed — so the wired
path kept writing only ``{session_id, notified_thresholds}`` and the whole
monitor→state-file→bridge→heartbeat→server→roster chain died silently at stage 1.

e-6534 collapsed the two copies into ONE canonical implementation (the Python
module) by turning the bash file into a thin ``exec`` delegator. These tests pin
that resolution so the same class of drift cannot silently return:

  * ``test_wired_sh_writes_context_pct`` is the behavioural guard — it runs the
    real ``.sh`` end-to-end and asserts the state file carries ``context_pct``.
    If anyone re-forks the bash into an independent reimplementation that forgets
    context_pct (exactly the e-6499 regression), this goes RED.
  * ``test_sh_delegates_not_reimplements`` is the structural guard — it asserts
    the bash file has no second, independent state writer of its own, catching a
    reintroduced parallel implementation by inspection before it can drift.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "context-usage-monitor.sh"


def _write_transcript(path: Path, *, model: str, input_tokens: int) -> None:
    entry = {
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    }
    path.write_text(json.dumps(entry) + "\n")


@pytest.fixture
def project_dir(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({"name": "t", "milestones": []}))
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _run(transcript: Path, project_dir: Path, session_id: str = "drift-sess"):
    env = os.environ.copy()
    env.pop("BEACON_CONTEXT_LIMIT", None)
    payload = json.dumps({"session_id": session_id,
                          "transcript_path": str(transcript)})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=payload, capture_output=True, text=True,
        cwd=str(project_dir), env=env, timeout=30,
    )


def test_wired_sh_writes_context_pct(project_dir, tmp_path):
    """The wired bash hook must persist context_pct (the e-6499 field it was
    silently dropping). 300K / 1M window (opus-4-8) = 30%."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, model="claude-opus-4-8", input_tokens=300_000)

    result = _run(transcript, project_dir)
    assert result.returncode == 0, result.stderr

    state = json.loads((project_dir / ".claude" / "context-usage-state.json").read_text())
    # The regression this guards: context_pct absent because the wired path never
    # wrote it. It must now be present with the computed percent.
    assert state["context_pct"] == 30, state
    assert state["context_used"] == 300_000, state
    assert state["context_limit"] == 1_000_000, state
    # notified_thresholds behaviour must NOT regress (item 3 of the done-when).
    assert state["session_id"] == "drift-sess"
    assert 20 in state["notified_thresholds"]
    # e-6588: the wired path must ALSO write the per-session record — the one the
    # monitor reads back for dedup and the bridge matches by pid identity.
    own = json.loads((project_dir / ".claude" / "context-usage" / "drift-sess.json").read_text())
    assert own["context_pct"] == 30 and 20 in own["notified_thresholds"], own
    assert isinstance(own.get("pids"), list) and own["pids"], own


def test_sh_delegates_not_reimplements():
    """Structural guard: the bash file must delegate to the Python canonical
    impl, not carry its own state-writing logic. Catches a reintroduced parallel
    implementation before it can drift out of sync.

    Inspects only EXECUTABLE lines (comments stripped): the file's prose
    deliberately mentions ``notified_thresholds`` / ``context-usage-state.json``
    to explain the history, so a naive substring scan over the whole file would
    false-positive. We scan the code, not the documentation."""
    code_lines = [
        ln for ln in SCRIPT.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # It delegates to the standalone Python entry point via exec.
    assert "exec" in code and "context-usage-monitor.py" in code, code
    # The old bash version built state JSON in-file. None of those constructs may
    # appear in EXECUTABLE code — a delegator writes no state of its own.
    for reimpl_marker in ("STATE_FILE=", "json.dump", "notified_thresholds",
                          "context-usage-state.json"):
        assert reimpl_marker not in code, (
            f"bash hook has executable code containing {reimpl_marker!r} — it "
            "looks like a reintroduced in-file reimplementation. Delegate to "
            "beacon_cli/hooks/context_monitor.py (the single canonical impl).")
    # M-F5 (#755 review): the shim must resolve its own directory robustly, not
    # rely on a bare `dirname "$0"` that returns "." under bare-name invocation.
    assert "pwd -P" in code, (
        "shim should resolve SCRIPT_DIR via `cd -- $(dirname -- $0) && pwd -P` "
        "so delegation is location-independent (bare-name invocation edge).")


def _load_standalone():
    """Import bin/context-usage-monitor.py (hyphenated name) as a module."""
    path = Path(__file__).resolve().parent.parent / "bin" / "context-usage-monitor.py"
    spec = importlib.util.spec_from_file_location("cum_standalone_e6534", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_standalone_no_op_is_loud_not_silent(monkeypatch, capsys):
    """AX-F3 (#755 review): when neither import path resolves beacon_cli, the
    Stop hook can do nothing — but a SILENT no-op is the exact silent-non-function
    class ms-159 fixes. It must (a) never block the Stop event (rc 0) and (b)
    surface the breakage LOUDLY, both on stderr and in-band via the Stop hook's
    own additionalContext channel."""
    mod = _load_standalone()
    monkeypatch.setattr(mod, "_try_package_import", lambda: None)
    monkeypatch.setattr(mod, "_try_in_tree_import", lambda: None)

    rc = mod.main()
    captured = capsys.readouterr()

    assert rc == 0  # never blocks the Stop event
    assert "beacon_cli" in captured.err  # stderr line stays
    # loud, in-band: a Stop hookSpecificOutput the AI/human actually sees
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "beacon_cli" in payload["hookSpecificOutput"]["additionalContext"]
