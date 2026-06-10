// Local .beacon/session.json heartbeat writer for the bridge (e-1424 / ms-54).
//
// Background
// ----------
// Pre e-1424 the bridge wrote heartbeat data (last_active, last_poll_at) only
// to the cloud sessions/{sid} document via PUT. The local session.json on
// disk was written once at first mint by lib/session.py and then never
// touched. Once _DEFAULT_FRESHNESS_SECONDS (3600s) passed without a re-mint
// the file looked stale, so the next CLI call inside the same cwd would
// mint a fresh session_id (overwriting session.json) while bus.mjs and
// .beacon/bridge.json kept using the original — two concurrent session_ids
// for the same logical session.
//
// The contract test test_session_heartbeat_responsibility.py pins
// "CLI is pure getter, bridge owns last_active". This module makes the
// bridge actually do the second half of that ownership — write last_active
// to local session.json (not just cloud) — so the CLI's freshness check
// sees a recently-bumped marker and reuses the id.
//
// Design notes
// ------------
//  * Debounced (default 60s) so a 2s poll cadence doesn't thrash the disk
//    or generate inotify-style mtime noise. 60s << 3600s freshness, so
//    the CLI always sees a fresh marker.
//  * Atomic (tmp + rename) like lib/session.py's write_session — so a
//    concurrent CLI reader can't see a half-written file.
//  * Best-effort — failures are logged and swallowed. The bridge keeps
//    polling, the cloud heartbeat still wins, and the worst case is the
//    next CLI re-mints (= the pre-fix behavior, no regression).
//  * No-op when last_active already matches (race-window between two
//    bridges in the same cwd; harmless either way).

import fs from 'node:fs'

export const LOCAL_SESSION_DEBOUNCE_MS = 60_000

/**
 * Create a stateful writer bound to one session.json path.
 *
 * @param {object} opts
 * @param {string} opts.sessionJsonPath   Absolute path to .beacon/session.json.
 * @param {(msg: string) => void} [opts.log]  Optional logger for failures.
 * @param {number} [opts.debounceMs]      Override the default debounce window.
 * @returns {(nowIso: string, options?: {force?: boolean}) => object}
 *   The returned function returns an object describing the outcome:
 *     { written: true, last_active }   — wrote a new value
 *     { skipped: 'debounce' }          — within the debounce window
 *     { skipped: 'unchanged' }         — file already had this nowIso
 *     { error: string }                — read/write/parse failure (logged)
 *   This shape is consumed by tests; the bridge itself ignores it.
 */
export function createLocalSessionHeartbeat({
  sessionJsonPath,
  log,
  debounceMs = LOCAL_SESSION_DEBOUNCE_MS,
} = {}) {
  let lastWriteAtMs = 0

  return function updateLocalSessionLastActive(nowIso, { force = false } = {}) {
    try {
      const nowMs = Date.now()
      if (!force && nowMs - lastWriteAtMs < debounceMs) {
        return { skipped: 'debounce' }
      }
      const raw = fs.readFileSync(sessionJsonPath, 'utf-8')
      const data = JSON.parse(raw)
      if (data.last_active === nowIso) {
        lastWriteAtMs = nowMs
        return { skipped: 'unchanged' }
      }
      data.last_active = nowIso
      const tmp = sessionJsonPath + '.tmp'
      fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n')
      fs.renameSync(tmp, sessionJsonPath)
      lastWriteAtMs = nowMs
      return { written: true, last_active: nowIso }
    } catch (e) {
      if (log) log(`local session.json heartbeat write failed (non-fatal): ${e.message}`)
      return { error: e.message }
    }
  }
}
