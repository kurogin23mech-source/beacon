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

/**
 * Read the state marker at `markerPath`.
 *
 * @param {string} markerPath  Path to .beacon/session-state.json.
 * @returns {{declaredState: string, declaredAt: string, stateSince: string}|null}
 *   The declared state + timestamps, or null when the marker is absent,
 *   unreadable, malformed, or missing either required field. declared_state and
 *   declared_at are required together — the server's derive_state needs
 *   declared_at to judge staleness, so a marker with only one is treated as no
 *   declaration. stateSince falls back to declaredAt when the marker predates
 *   the field (ms-159 e-6245 back-compat with e-6244 markers).
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
      const stateSince = (typeof rawSince === 'string' && rawSince) ? rawSince : declaredAt
      return { declaredState, declaredAt, stateSince }
    }
  } catch (e) {
    // malformed JSON → treat as no declaration (never throw into the poll loop)
  }
  return null
}
