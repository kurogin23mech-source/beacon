// Option C true-heartbeat helper (ms-54 / e-1318).
//
// The contract: the bridge's poll loop in channel/bus.mjs writes a session
// heartbeat AFTER EVERY poll iteration (success, no-op, or recovered error).
// If the poll loop hangs or crashes, last_poll_at structurally stops
// advancing — server-side `healthy_only` then drops this session.
//
// This module exports the *body builder* and a small runner used by tests.
// The actual HTTP PUT lives in bus.mjs (which already owns API_URL + auth
// token discovery); we don't import the bridge here because importing
// bus.mjs has process-level side effects (MCP stdio connect, poll loop
// kickoff, etc.). The runner takes an injected `putFn` so a test can drive
// it without standing up a real server.
//
// What we pin behavior-wise:
//   1. Every poll iteration produces exactly one heartbeat write.
//   2. Poll errors do NOT skip the heartbeat — the loop's still alive,
//      so last_poll_at advances even when pollOnce throws.
//   3. Stopping the loop triggers one final write with shutdown=true.
//   4. If putFn rejects, the loop keeps running (heartbeat is best-effort,
//      never load-bearing for delivery).

/**
 * Build the heartbeat body the bridge sends on every poll.
 *
 * The ``shutdown`` field is **always** included, even when false. Without
 * this, a graceful exit's ``shutdown=true`` write stays stuck on the
 * Firestore record (= merge=true keeps the prior value), and a *fresh*
 * bclaude that restarts in the same terminal — reusing the same sid via
 * cloud-first tuple lookup — shows ``healthy=False`` in the bus directory
 * because the server-side health filter reads the stale shutdown flag.
 * Observed during ms-62 Phase 2 dogfood (2026-06-12): same-terminal sid
 * continuity worked structurally but the picker dropped the recovered
 * session because of this stale flag.
 *
 * @param {object} opts
 * @param {string} opts.nowIso     ISO8601 of the current poll iteration.
 * @param {number} opts.pollIntervalMs
 * @param {boolean} [opts.shutdown]
 * @returns {object}
 */
// @e-2502-core-candidate — heartbeat body is bus protocol, currently
//   duplicated in lib/codex_receive_loop.py::heartbeat_to_server. Move
//   the shape to lib/bus_protocol.py once that lands; this function
//   becomes a thin Node wrapper.
export function buildHeartbeatBody({ nowIso, pollIntervalMs, shutdown = false, transport }) {
  const body = {
    last_active: nowIso,
    last_poll_at: nowIso,
    poll_interval_ms: pollIntervalMs,
    shutdown: !!shutdown,
  }
  // ms-145 / e-5378 — optional transport observability. Lets the fleet
  // distinguish a WS-healthy bridge (poll dropped to backstop) from one stuck
  // on the 5s poll floor, and *why* (never-opened vs flapping). Shape MUST match
  // lib/bus_protocol.py::heartbeat_body. Omitted when not provided (back-compat).
  if (transport !== undefined && transport !== null) body.transport = transport
  return body
}

/**
 * Run a fixed number of poll iterations and accumulate the heartbeat
 * writes via an injected putFn. Used by tests to assert the poll-loop
 * → heartbeat coupling without hitting the network.
 *
 * The shape mirrors bus.mjs's real loop closely:
 *   while (!stopping) {
 *     try { await pollOnceFn() } catch (e) { /-* swallow *-/ }
 *     await putFn(buildHeartbeatBody(...))
 *     await sleep(pollIntervalMs)
 *   }
 *   await putFn(buildHeartbeatBody(..., shutdown: true))
 *
 * @param {object} opts
 * @param {() => Promise<void>} opts.pollOnceFn  May throw; loop must not die.
 * @param {(body: object) => Promise<void>} opts.putFn  Heartbeat sink.
 * @param {() => string} opts.nowFn  Returns ISO8601 for the iteration.
 * @param {number} opts.pollIntervalMs
 * @param {number} opts.iterations  Number of iterations before shutdown.
 * @returns {Promise<{writes: object[], pollErrors: Error[], putErrors: Error[]}>}
 */
export async function runPollLoopForTest({
  pollOnceFn, putFn, nowFn, pollIntervalMs, iterations,
}) {
  const writes = []
  const pollErrors = []
  const putErrors = []

  const wrappedPut = async (body) => {
    writes.push(body)
    try {
      await putFn(body)
    } catch (e) {
      // Mirror bus.mjs: never let heartbeat failures kill the loop.
      putErrors.push(e)
    }
  }

  for (let i = 0; i < iterations; i++) {
    try {
      await pollOnceFn()
    } catch (e) {
      pollErrors.push(e)
    }
    await wrappedPut(buildHeartbeatBody({
      nowIso: nowFn(), pollIntervalMs, shutdown: false,
    }))
    // Tests don't actually sleep — they want determinism. Skip the real
    // delay so test runtime stays under the timeout.
  }

  // Graceful shutdown stamp.
  await wrappedPut(buildHeartbeatBody({
    nowIso: nowFn(), pollIntervalMs, shutdown: true,
  }))

  return { writes, pollErrors, putErrors }
}
