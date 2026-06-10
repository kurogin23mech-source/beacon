"""Tests for the bridge's local session.json heartbeat (e-1424 / ms-54).

Pinned contract:

  * The bridge writes `last_active` to the LOCAL .beacon/session.json so
    the CLI's freshness check (lib/session.py `_is_fresh`) sees a fresh
    marker and reuses the same session_id instead of minting a new one
    next time a Python entry point runs.
  * Writes are debounced (default 60s) so the 2s poll cadence doesn't
    thrash the disk. 60s << 3600s freshness threshold so the marker
    stays comfortably fresh.
  * Atomic write (tmp + rename) — concurrent CLI readers must never see
    a half-written file.
  * No-op when the value is already what we'd write (saves an inode bump
    + mtime change in the common case).
  * Failures (read error, parse error, write error) are swallowed and
    surfaced via the optional log callback. The bridge must keep polling
    even if the local FS is read-only or session.json was deleted.

These tests pin the helper as a separate module so they don't have to
spin up the full bus.mjs (which on import does MCP stdio connect, kicks
off the poll loop, etc.).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_MJS = REPO_ROOT / "channel" / "bus-local-heartbeat.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — MCP bridge heartbeat is Node-only",
)


def _run(script: str) -> dict:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


def _write_session_json(path: Path, last_active: str = "2026-06-10T00:00:00Z") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "session_id": "test-session-id-123",
            "actor": {"machine": "test", "agent": "test"},
            "created_at": "2026-06-10T00:00:00Z",
            "last_active": last_active,
            "harness": "test",
            "source": "minted",
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_first_call_writes_last_active(tmp_path):
    """A fresh writer should update last_active on its very first call —
    the debounce starts at 0 so there's nothing to wait for."""
    session_json = tmp_path / ".beacon" / "session.json"
    _write_session_json(session_json, last_active="2026-06-10T00:00:00Z")

    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
        }})
        const result = write('2026-06-10T05:00:00.000Z')
        process.stdout.write(JSON.stringify(result))
    """)
    result = _run(script)
    assert result == {
        "written": True,
        "last_active": "2026-06-10T05:00:00.000Z",
    }

    data = json.loads(session_json.read_text(encoding="utf-8"))
    assert data["last_active"] == "2026-06-10T05:00:00.000Z"
    # Other fields preserved.
    assert data["session_id"] == "test-session-id-123"
    assert data["source"] == "minted"


def test_debounced_second_call_is_skipped(tmp_path):
    """Two calls within the debounce window: only the first writes.
    Without this the bridge would write every 2s during the poll loop
    and thrash the disk."""
    session_json = tmp_path / ".beacon" / "session.json"
    _write_session_json(session_json, last_active="2026-06-10T00:00:00Z")

    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
          debounceMs: 60000,
        }})
        const first  = write('2026-06-10T05:00:00.000Z')
        const second = write('2026-06-10T05:00:02.000Z')   // 2s later, inside window
        process.stdout.write(JSON.stringify({{ first, second }}))
    """)
    result = _run(script)
    assert result["first"]["written"] is True
    assert result["second"] == {"skipped": "debounce"}

    # File reflects only the first write.
    data = json.loads(session_json.read_text(encoding="utf-8"))
    assert data["last_active"] == "2026-06-10T05:00:00.000Z"


def test_force_flag_bypasses_debounce(tmp_path):
    """Graceful shutdown must always flush — even if the previous write
    was a poll iteration 100ms ago. Otherwise the bridge could die with
    a stale local marker and the next CLI immediately re-mints."""
    session_json = tmp_path / ".beacon" / "session.json"
    _write_session_json(session_json, last_active="2026-06-10T00:00:00Z")

    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
          debounceMs: 60000,
        }})
        const first  = write('2026-06-10T05:00:00.000Z')
        const second = write('2026-06-10T05:00:01.000Z', {{ force: true }})
        process.stdout.write(JSON.stringify({{ first, second }}))
    """)
    result = _run(script)
    assert result["first"]["written"] is True
    assert result["second"]["written"] is True

    data = json.loads(session_json.read_text(encoding="utf-8"))
    assert data["last_active"] == "2026-06-10T05:00:01.000Z"


def test_unchanged_value_is_noop_after_debounce(tmp_path):
    """If the bridge somehow tries to write the same nowIso twice (rare
    but possible if the system clock has 1s resolution), the second call
    after the debounce should still no-op the disk write to avoid an
    inode bump."""
    session_json = tmp_path / ".beacon" / "session.json"
    _write_session_json(session_json, last_active="2026-06-10T00:00:00Z")

    # debounceMs=0 so the second call is past the window immediately.
    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
          debounceMs: 0,
        }})
        const first  = write('2026-06-10T05:00:00.000Z')
        const second = write('2026-06-10T05:00:00.000Z')   // identical
        process.stdout.write(JSON.stringify({{ first, second }}))
    """)
    result = _run(script)
    assert result["first"]["written"] is True
    assert result["second"] == {"skipped": "unchanged"}


def test_missing_session_json_logs_and_swallows(tmp_path):
    """If session.json is missing (= bridge started before CLI mint
    materialised one), the writer must NOT crash. Log + move on so the
    poll loop keeps running."""
    session_json = tmp_path / ".beacon" / "session.json"  # never created

    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const logs = []
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
          log: (msg) => logs.push(msg),
        }})
        const result = write('2026-06-10T05:00:00.000Z')
        process.stdout.write(JSON.stringify({{ result, logs }}))
    """)
    out = _run(script)
    assert "error" in out["result"]
    assert len(out["logs"]) == 1
    assert "local session.json heartbeat write failed" in out["logs"][0]


def test_atomic_write_via_tmp_rename(tmp_path):
    """Defense in depth: after a successful write, there must be no
    leftover .tmp file. lib/session.py uses the same pattern; a concurrent
    CLI reader must never see a half-written file."""
    session_json = tmp_path / ".beacon" / "session.json"
    _write_session_json(session_json, last_active="2026-06-10T00:00:00Z")

    script = textwrap.dedent(f"""
        import {{ createLocalSessionHeartbeat }} from '{HELPER_MJS.as_posix()}'
        const write = createLocalSessionHeartbeat({{
          sessionJsonPath: '{session_json.as_posix()}',
        }})
        write('2026-06-10T05:00:00.000Z')
        process.stdout.write(JSON.stringify({{ done: true }}))
    """)
    _run(script)

    # Final file present, no stale tmp.
    assert session_json.exists()
    assert not (tmp_path / ".beacon" / "session.json.tmp").exists()


def test_default_debounce_is_60_seconds():
    """Pin the default debounce window. 60s is intentional: << 3600s
    freshness threshold means the CLI always sees a fresh marker, while
    >> 2s poll cadence keeps disk writes infrequent."""
    script = textwrap.dedent(f"""
        import {{ LOCAL_SESSION_DEBOUNCE_MS }} from '{HELPER_MJS.as_posix()}'
        process.stdout.write(JSON.stringify({{ ms: LOCAL_SESSION_DEBOUNCE_MS }}))
    """)
    result = _run(script)
    assert result["ms"] == 60_000
