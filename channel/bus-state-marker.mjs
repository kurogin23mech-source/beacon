// ms-159 / e-6244 — read the session-state marker the hooks write.
//
// beacon-state-hook.py writes .beacon/session-state.json on each Claude Code
// lifecycle hook: {declared_state, declared_at, source_event}. The bridge's
// poll loop reads it here and piggybacks declared_state/declared_at onto the
// existing heartbeat PUT (slice SPEC 方針4 — no new send path).
//
// Fail-safe by construction: a missing marker (session that predates the hook,
// or a cwd with no .beacon) and a malformed marker BOTH resolve to null =
// "no declaration this heartbeat". A best-effort observability signal must
// never throw into the poll loop that delivers real events.

import fs from 'node:fs'

// ms-159 review (#735): the one work-unit state the bridge itself declares
// (graceful shutdown → terminated). Held as a named constant here — a copy of
// lib/bus_liveness.STATE_TERMINATED, since JS cannot import the Python module.
// The parity is locked by tests/test_bus_state_marker_parity.py, which fails
// loudly if this literal ever drifts from the Python source of truth. Mirrors
// the bus.mjs UNTRUSTED_FRAME_HEADER byte-identical-copy pattern (ms-169 e-6235).
export const STATE_TERMINATED = 'terminated'

/**
 * Read the state marker at `markerPath`.
 *
 * @param {string} markerPath  Path to .beacon/session-state.json.
 * @returns {{declaredState: string, declaredAt: string, stateSince: string|undefined}|null}
 *   The declared state + timestamps, or null when the marker is absent,
 *   unreadable, malformed, or missing either required field. declared_state and
 *   declared_at are required together — the server's derive_state needs
 *   declared_at to judge staleness, so a marker with only one is treated as no
 *   declaration.
 *
 *   ms-159 review (#735): stateSince is passed through verbatim (undefined when
 *   the marker lacks it, e.g. an e-6244 marker that predates the field). The
 *   reader does NOT fabricate a fallback here — the server owns the single
 *   authoritative state_since fallback chain (server/app.py: state_since ←
 *   declared_at ← last_poll_at ← last_active). A back-compat marker therefore
 *   omits state_since from the heartbeat, and the server's own fallback resolves
 *   it to declared_at — identical result, but only one place decides.
 */
export function readStateMarker(markerPath) {
  let raw
  try {
    raw = fs.readFileSync(markerPath, 'utf-8')
  } catch (e) {
    return null  // absent marker → no declaration (the common back-compat path)
  }
  try {
    const obj = JSON.parse(raw)
    const declaredState = obj && obj.declared_state
    const declaredAt = obj && obj.declared_at
    if (typeof declaredState === 'string' && declaredState &&
        typeof declaredAt === 'string' && declaredAt) {
      const rawSince = obj && obj.state_since
      const stateSince = (typeof rawSince === 'string' && rawSince) ? rawSince : undefined
      return { declaredState, declaredAt, stateSince }
    }
  } catch (e) {
    // malformed JSON → treat as no declaration (never throw into the poll loop)
  }
  return null
}
