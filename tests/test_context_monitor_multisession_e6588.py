"""ms-159 / e-6588 — two Claude sessions in ONE cwd must not reset each other's
context-usage threshold dedup, and the bridge must read ITS OWN session's %.

Pre-fix data flow (the bug):
  hook(A) → .claude/context-usage-state.json {session_id: A, notified: [20]}
  hook(B) → same file → "session changed" → notified reset → {B, [20]}
  hook(A) → same file → "session changed" → reset → 20 % note request RE-FIRES
  bridge(A) reads the same file → shows B's percent (last writer wins).

Post-fix: one record per session under .claude/context-usage/<session_id>.json
(no shared write → no reset path), each stamped with the hook's ancestor pids +
BEACON_PARENT_PID; the bridge picks the record whose pids contain its
process.ppid (the common Claude Code parent). Legacy per-cwd file still written
(old bridges) but never read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beacon_cli.hooks import context_monitor as cm  # noqa: E402

CONTEXT_MJS = ROOT / "channel" / "bus-context-usage.mjs"
BUS_MJS = ROOT / "channel" / "bus.mjs"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _write_transcript(path: Path, input_tokens: int) -> None:
    line = {"message": {"role": "assistant", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": input_tokens}}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


def _run_hook(payload: dict, monkeypatch) -> tuple[int, str]:
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = cm.main()
    return rc, out.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A beacon project cwd with NO dry-run (state must really be written) and
    every external side effect (beacon note / status / git) stubbed out."""
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({"name": "t", "milestones": []}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_CONTEXT_MONITOR_DRY_RUN", raising=False)
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)
    monkeypatch.setattr(cm, "_which_beacon", lambda: None)
    monkeypatch.setattr(cm, "_build_recent_commits_text", lambda cwd: "")
    monkeypatch.setattr(cm, "_build_pending_tasks_text", lambda beacon, cwd: "")
    return tmp_path


def _fired(stdout: str) -> bool:
    return bool(stdout.strip())


# --------------------------------------------------------------------------- #
# Layer 1 — the hook: dedup survives an interleaved sibling session
# --------------------------------------------------------------------------- #

class TestHookDedupAcrossSessions:
    def test_interleaved_sessions_do_not_refire_same_threshold(self, project, tmp_path, monkeypatch):
        """The regression itself: A fires 20 %, B fires 20 %, A must stay silent."""
        t = tmp_path / "t.jsonl"
        _write_transcript(t, 250_000)  # 25 % of 1M → threshold 20 crossed

        rc, out = _run_hook({"session_id": "sess-A", "transcript_path": str(t)}, monkeypatch)
        assert rc == 0 and _fired(out), "A's first crossing must notify"

        rc, out = _run_hook({"session_id": "sess-B", "transcript_path": str(t)}, monkeypatch)
        assert rc == 0 and _fired(out), "B's own first crossing must notify"

        rc, out = _run_hook({"session_id": "sess-A", "transcript_path": str(t)}, monkeypatch)
        assert rc == 0
        assert not _fired(out), (
            "A already notified 20 % — B's hook run in the same cwd must not have "
            "reset A's dedup state (pre-e-6588 double-notification bug)")

    def test_each_session_has_its_own_record(self, project, tmp_path, monkeypatch):
        t = tmp_path / "t.jsonl"
        _write_transcript(t, 250_000)
        _run_hook({"session_id": "sess-A", "transcript_path": str(t)}, monkeypatch)
        _run_hook({"session_id": "sess-B", "transcript_path": str(t)}, monkeypatch)

        a = json.loads(cm._state_path_for("sess-A").read_text())
        b = json.loads(cm._state_path_for("sess-B").read_text())
        assert a["session_id"] == "sess-A" and 20 in a["notified_thresholds"]
        assert b["session_id"] == "sess-B" and 20 in b["notified_thresholds"]
        # identity fields the bridge matches on
        for rec in (a, b):
            assert isinstance(rec["pids"], list) and os.getpid() in rec["pids"]
            assert rec["updated_at"]

    def test_record_carries_parent_pid_from_env(self, project, tmp_path, monkeypatch):
        monkeypatch.setenv("BEACON_PARENT_PID", "4242")
        t = tmp_path / "t.jsonl"
        _write_transcript(t, 100_000)  # 10 %, no crossing — persist path
        _run_hook({"session_id": "sess-A", "transcript_path": str(t)}, monkeypatch)
        rec = json.loads(cm._state_path_for("sess-A").read_text())
        assert rec["parent_pid"] == 4242
        assert rec["context_pct"] == 10

    def test_legacy_file_still_written_but_not_read(self, project, tmp_path, monkeypatch):
        """Back-compat: an old bridge reads the legacy per-cwd file, so we keep
        writing it. But it is NOT the dedup source — a legacy file claiming
        'already notified' must not silence a session whose own record says
        otherwise, and a sibling's legacy write must not reset us."""
        t = tmp_path / "t.jsonl"
        _write_transcript(t, 250_000)
        legacy = Path(cm.STATE_FILE_REL)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"session_id": "sess-A", "notified_thresholds": [20]}))

        rc, out = _run_hook({"session_id": "sess-A", "transcript_path": str(t)}, monkeypatch)
        assert _fired(out), "legacy file must not be consulted for dedup"
        data = json.loads(legacy.read_text())
        assert data["session_id"] == "sess-A" and data["context_pct"] == 25

    def test_state_path_is_sanitised(self, tmp_path):
        p = cm._state_path_for("../../evil/..", tmp_path)
        # a hostile id can never escape the directory: no separators survive
        assert p.parent == tmp_path and p.resolve().parent == tmp_path.resolve()
        assert "/" not in p.name and "\\" not in p.name and p.name.endswith(".json")


class TestPrune:
    def test_dead_sibling_records_are_pruned_live_kept(self, tmp_path, monkeypatch):
        if os.name != "posix":
            pytest.skip("prune is POSIX-only by design (os.kill(pid, 0) kills on Windows)")
        d = tmp_path / "context-usage"
        d.mkdir()
        own = d / "me.json"
        own.write_text(json.dumps({"session_id": "me", "pids": [os.getpid()]}))
        live = d / "live.json"
        live.write_text(json.dumps({"session_id": "live", "pids": [os.getppid()]}))
        # a pid that cannot be alive: max pid + 1 style sentinel
        dead = d / "dead.json"
        dead.write_text(json.dumps({"session_id": "dead", "pids": [2**22 - 7]}))
        unknown = d / "unknown.json"
        unknown.write_text(json.dumps({"session_id": "unknown"}))  # no pids → cannot judge
        garbage = d / "garbage.json"
        garbage.write_text("{not json")

        monkeypatch.setattr(cm, "_pid_alive", lambda pid: pid in (os.getpid(), os.getppid()))
        cm._prune_stale_state_files(d, keep=own)

        assert own.exists() and live.exists()
        assert not dead.exists(), "record whose every pid is dead must be pruned"
        assert unknown.exists(), "record with no pids is left alone"
        assert garbage.exists(), "unparseable record is left alone (never raise)"


# --------------------------------------------------------------------------- #
# Layer 3 — the bridge reader picks ITS OWN record (node)
# --------------------------------------------------------------------------- #

def _have_node() -> bool:
    return shutil.which("node") is not None


jsmark = pytest.mark.skipif(not _have_node(), reason="node not available")


def _read_dir_via_node(dir_path: Path, me: dict):
    script = textwrap.dedent(f"""
        import {{ readContextUsageForSession }} from '{CONTEXT_MJS.as_posix()}'
        const r = readContextUsageForSession({json.dumps(str(dir_path))}, {json.dumps(me)})
        process.stdout.write(JSON.stringify(r))
    """)
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _rec(d: Path, name: str, **fields) -> None:
    (d / f"{name}.json").write_text(json.dumps(fields))


@jsmark
class TestBridgePicksOwnRecord:
    def test_matches_by_ppid_in_pids_not_last_writer(self, tmp_path):
        d = tmp_path / "context-usage"; d.mkdir()
        _rec(d, "A", context_pct=12, pids=[900, 1000, 500], updated_at="2026-09-24T00:00:01Z")
        _rec(d, "B", context_pct=49, pids=[901, 2000, 500], updated_at="2026-09-24T00:00:09Z")
        # bridge under Claude Code pid 1000 → A, even though B was written later
        assert _read_dir_via_node(d, {"ppid": 1000}) == {"contextPct": 12}
        assert _read_dir_via_node(d, {"ppid": 2000}) == {"contextPct": 49}

    def test_no_match_is_null_not_someone_elses(self, tmp_path):
        d = tmp_path / "context-usage"; d.mkdir()
        _rec(d, "B", context_pct=49, pids=[901, 2000], updated_at="x")
        assert _read_dir_via_node(d, {"ppid": 1000}) is None
        assert _read_dir_via_node(d, {}) is None

    def test_falls_back_to_parent_pid_with_latest_wins(self, tmp_path):
        # Windows-style records (no ps → pids has only the hook chain we can't
        # match) — BEACON_PARENT_PID is shared by successive Claude sessions in
        # one terminal, so the freshest record wins the tie.
        d = tmp_path / "context-usage"; d.mkdir()
        _rec(d, "old", context_pct=80, pids=[1], parent_pid=777, updated_at="2026-09-23T10:00:00Z")
        _rec(d, "new", context_pct=5, pids=[2], parent_pid=777, updated_at="2026-09-24T10:00:00Z")
        _rec(d, "other", context_pct=60, pids=[3], parent_pid=778, updated_at="2026-09-24T11:00:00Z")
        assert _read_dir_via_node(d, {"ppid": 1000, "parentPid": 777}) == {"contextPct": 5}

    def test_ppid_match_beats_parent_pid_match(self, tmp_path):
        d = tmp_path / "context-usage"; d.mkdir()
        _rec(d, "weak", context_pct=70, pids=[1], parent_pid=777, updated_at="2026-09-24T11:00:00Z")
        _rec(d, "strong", context_pct=20, pids=[1000], parent_pid=777, updated_at="2026-09-24T10:00:00Z")
        assert _read_dir_via_node(d, {"ppid": 1000, "parentPid": 777}) == {"contextPct": 20}

    def test_absent_dir_and_garbage_are_null(self, tmp_path):
        assert _read_dir_via_node(tmp_path / "nope", {"ppid": 1}) is None
        d = tmp_path / "context-usage"; d.mkdir()
        (d / "x.json").write_text("{not json")
        (d / "y.json").write_text(json.dumps({"pids": [1000], "context_pct": "73"}))  # invalid pct
        (d / "notes.txt").write_text("ignored")
        assert _read_dir_via_node(d, {"ppid": 1000}) is None

    def test_zero_pct_is_returned(self, tmp_path):
        d = tmp_path / "context-usage"; d.mkdir()
        _rec(d, "A", context_pct=0, pids=[1000])
        assert _read_dir_via_node(d, {"ppid": 1000}) == {"contextPct": 0}


# --------------------------------------------------------------------------- #
# Wiring guard — bus.mjs reads the per-session dir with its own identity, and
# no longer reads the legacy shared file (which would re-open the wrong-badge
# path). Symbol-level checks on the call expression, not loose substrings.
# --------------------------------------------------------------------------- #

def test_bus_mjs_wires_per_session_reader_with_own_identity():
    src = BUS_MJS.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))
    assert "import { readContextUsageForSession } from './bus-context-usage.mjs'" in code
    call = code[code.index("readContextUsageForSession(CONTEXT_USAGE_DIR"):]
    call = call[:call.index(")") + 1]
    assert "ppid: process.ppid" in call, call
    assert "parentPid: MY_PARENT_PID" in call, call
    assert "path.join(CWD, '.claude', 'context-usage')" in code
    # the legacy single-file reader must not be wired anywhere in bus.mjs
    assert "readContextUsage(" not in code.replace("readContextUsageForSession(", "")
    assert "context-usage-state.json" not in code
