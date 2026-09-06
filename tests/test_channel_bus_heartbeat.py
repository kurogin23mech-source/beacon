"""Tests for the bridge's poll-gated heartbeat helper (ms-54 / e-1318).

Option C true-heartbeat moves the heartbeat write inside the poll loop, so
``last_poll_at`` only advances when the bridge is actually pumping events.
The runner + body builder live in ``channel/bus-heartbeat.mjs`` so this
behavior is unit-testable without the side effects of importing the full
``bus.mjs`` (which on import does MCP stdio connect, kicks off the loop,
etc.).

Pinned contract:

  * Every iteration produces exactly one heartbeat write (never zero, never
    two). The whole point of Option C is that the write is *coupled* to the
    loop's liveness.
  * A pollOnce exception does NOT skip the heartbeat — the loop is still
    alive, so last_poll_at must still advance. (If we skipped on error,
    transient network blips would falsely mark the bridge as dead.)
  * On graceful shutdown, the runner posts one final body with
    ``shutdown=true`` so the directory can immediately classify the session
    as deliberately stopped rather than waiting for the timestamp to go
    stale.
  * Heartbeat-write failures (putFn rejects) do NOT halt the loop. The
    next iteration still runs and tries again.
  * The body shape matches what the server's SessionUpsert schema accepts:
    ``last_active``, ``last_poll_at``, ``poll_interval_ms``, optional
    ``shutdown``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_MJS = REPO_ROOT / "channel" / "bus-heartbeat.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — MCP bridge heartbeat is Node-only",
)


def _run_scenario(script: str) -> dict:
    """Execute a Node script that imports bus-heartbeat.mjs and returns
    its JSON result on stdout. Errors surface with the full stderr so
    debugging a broken probe is one click instead of a guessing game."""
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


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------

def test_heartbeat_body_has_all_three_timestamp_fields():
    """The bridge must stamp last_active AND last_poll_at on every write.
    last_active keeps the legacy heartbeat path working (Web UI / rescue);
    last_poll_at is the new poll-gated signal. Both come from the same
    nowIso so a single iteration's write is internally consistent."""
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        const body = buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z',
          pollIntervalMs: 2000,
        }})
        process.stdout.write(JSON.stringify(body))
    """)
    body = _run_scenario(script)
    assert body == {
        "last_active": "2026-06-09T01:00:00.000Z",
        "last_poll_at": "2026-06-09T01:00:00.000Z",
        "poll_interval_ms": 2000,
        "shutdown": False,
    }


def test_heartbeat_body_sets_shutdown_false_explicitly():
    """``shutdown=false`` MUST set the field to ``false`` (not omit it).

    The earlier behaviour omitted the field, intending to "preserve" any
    previously-written shutdown flag. ms-62 Phase 2 dogfood (2026-06-12)
    showed this was the wrong call: a bclaude that exits gracefully writes
    ``shutdown=true``, and a fresh bclaude restarted in the same terminal
    (= cloud-first tuple lookup recovers the same sid) had its heartbeat
    body omit ``shutdown`` — so Firestore's merge kept ``shutdown=true``
    and the bus directory ``--healthy`` filter dropped the recovered
    session even though it was actively heartbeating. The fix is to
    always include the field so the merge overwrites the stale flag.
    """
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        const body = buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z',
          pollIntervalMs: 2000,
          shutdown: false,
        }})
        process.stdout.write(JSON.stringify(body))
    """)
    body = _run_scenario(script)
    assert body.get("shutdown") is False, body
    assert "shutdown" in body, body


def test_heartbeat_body_omits_transport_by_default():
    """ms-145 / e-5378: no transport arg → field absent (back-compat). Must match
    lib/bus_protocol.heartbeat_body so both receive loops serialise identically."""
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        process.stdout.write(JSON.stringify(buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z', pollIntervalMs: 5000,
        }})))
    """)
    body = _run_scenario(script)
    assert "transport" not in body, body


def test_heartbeat_body_includes_transport_when_provided():
    """ms-145 / e-5378: the Node bridge stamps ws_state + effective_poll_ms so the
    fleet can see WS-healthy (backstop) vs stuck-on-5s sessions and why."""
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        process.stdout.write(JSON.stringify(buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z', pollIntervalMs: 5000,
          transport: {{ ws_state: 'open', effective_poll_ms: 120000, ws_opens: 1,
                        ws_closes: 0, ws_last_close_code: null }},
        }})))
    """)
    body = _run_scenario(script)
    assert body["transport"]["ws_state"] == "open", body
    assert body["transport"]["effective_poll_ms"] == 120000, body


def test_heartbeat_body_clears_stale_shutdown_on_default_call():
    """The default ``shutdown`` value (= not passed) must also produce
    ``shutdown: false`` so cold-start heartbeats clear any stale flag.
    """
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        const body = buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z',
          pollIntervalMs: 2000,
        }})
        process.stdout.write(JSON.stringify(body))
    """)
    body = _run_scenario(script)
    assert body.get("shutdown") is False, body


def test_heartbeat_body_sets_shutdown_when_true():
    script = textwrap.dedent(f"""
        import {{ buildHeartbeatBody }} from '{HEARTBEAT_MJS.as_posix()}'
        const body = buildHeartbeatBody({{
          nowIso: '2026-06-09T01:00:00.000Z',
          pollIntervalMs: 2000,
          shutdown: true,
        }})
        process.stdout.write(JSON.stringify(body))
    """)
    body = _run_scenario(script)
    assert body.get("shutdown") is True


# ---------------------------------------------------------------------------
# Poll-loop coupling
# ---------------------------------------------------------------------------

def test_poll_loop_produces_one_heartbeat_per_iteration():
    script = textwrap.dedent(f"""
        import {{ runPollLoopForTest }} from '{HEARTBEAT_MJS.as_posix()}'
        let counter = 0
        const nowFn = () => `2026-06-09T01:00:0${{counter++}}.000Z`
        const result = await runPollLoopForTest({{
          pollOnceFn: async () => {{ /* no-op success */ }},
          putFn: async () => {{ /* no-op success */ }},
          nowFn,
          pollIntervalMs: 2000,
          iterations: 5,
        }})
        process.stdout.write(JSON.stringify({{
          writes: result.writes.length,
          pollErrors: result.pollErrors.length,
          putErrors: result.putErrors.length,
          shutdownStamped: result.writes[result.writes.length - 1].shutdown === true,
        }}))
    """)
    result = _run_scenario(script)
    # 5 iteration writes + 1 final shutdown write = 6
    assert result["writes"] == 6
    assert result["pollErrors"] == 0
    assert result["putErrors"] == 0
    assert result["shutdownStamped"] is True


def test_poll_loop_still_heartbeats_when_poll_throws():
    """The smoking-gun case: pollOnce is hanging or erroring. The loop
    is still alive (each iteration's `await pollOnce()` rejects, but the
    outer try/catch catches it). last_poll_at MUST keep advancing —
    that's how a consumer can tell "bridge is up, just network-blip"
    apart from "bridge process dead"."""
    script = textwrap.dedent(f"""
        import {{ runPollLoopForTest }} from '{HEARTBEAT_MJS.as_posix()}'
        let i = 0
        const nowFn = () => `2026-06-09T01:00:0${{i++}}.000Z`
        const result = await runPollLoopForTest({{
          pollOnceFn: async () => {{ throw new Error('simulated poll failure') }},
          putFn: async () => {{ /* heartbeat sink is healthy */ }},
          nowFn,
          pollIntervalMs: 2000,
          iterations: 3,
        }})
        process.stdout.write(JSON.stringify({{
          writes: result.writes.length,
          pollErrors: result.pollErrors.length,
          // Heartbeat advanced even though every poll failed.
          allWritesHaveLastPollAt: result.writes.every(w => w.last_poll_at),
        }}))
    """)
    result = _run_scenario(script)
    assert result["writes"] == 4  # 3 iterations + shutdown
    assert result["pollErrors"] == 3  # every iteration's poll threw
    assert result["allWritesHaveLastPollAt"] is True


def test_poll_loop_survives_putfn_rejection():
    """If the heartbeat endpoint is temporarily unreachable, the loop
    keeps polling. Heartbeat is best-effort — a cloud blip MUST NOT
    silently halt event delivery."""
    script = textwrap.dedent(f"""
        import {{ runPollLoopForTest }} from '{HEARTBEAT_MJS.as_posix()}'
        let i = 0
        const nowFn = () => `2026-06-09T01:00:0${{i++}}.000Z`
        const result = await runPollLoopForTest({{
          pollOnceFn: async () => {{ /* succeeded */ }},
          putFn: async () => {{ throw new Error('cloud unreachable') }},
          nowFn,
          pollIntervalMs: 2000,
          iterations: 4,
        }})
        process.stdout.write(JSON.stringify({{
          writes: result.writes.length,
          putErrors: result.putErrors.length,
        }}))
    """)
    result = _run_scenario(script)
    # All 4 iterations still attempted to write + 1 shutdown attempt = 5.
    assert result["writes"] == 5
    assert result["putErrors"] == 5


def test_poll_loop_shutdown_write_carries_shutdown_true():
    """Graceful shutdown — the final write must have shutdown=true so
    the directory's --healthy filter can drop this session immediately
    instead of waiting for last_poll_at to age out."""
    script = textwrap.dedent(f"""
        import {{ runPollLoopForTest }} from '{HEARTBEAT_MJS.as_posix()}'
        const result = await runPollLoopForTest({{
          pollOnceFn: async () => {{}},
          putFn: async () => {{}},
          nowFn: () => '2026-06-09T01:00:00.000Z',
          pollIntervalMs: 2000,
          iterations: 2,
        }})
        const writes = result.writes
        const lastWrite = writes[writes.length - 1]
        process.stdout.write(JSON.stringify({{
          totalWrites: writes.length,
          // Iteration writes must NOT have shutdown=true.
          iterationsHaveShutdown: writes.slice(0, -1).some(w => w.shutdown === true),
          finalIsShutdown: lastWrite.shutdown === true,
          finalHasInterval: lastWrite.poll_interval_ms === 2000,
        }}))
    """)
    result = _run_scenario(script)
    assert result["totalWrites"] == 3  # 2 iters + 1 shutdown
    assert result["iterationsHaveShutdown"] is False
    assert result["finalIsShutdown"] is True
    assert result["finalHasInterval"] is True
