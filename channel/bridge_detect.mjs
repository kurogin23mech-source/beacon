// e-3858 — cold-start bridge-liveness detection for the force-mint decision.
//
// Extracted from bus.mjs so the "another alive bridge?" predicate is pure and
// unit-testable, and so the rule keys on the BRIDGE process liveness — not
// merely its parent bclaude.
//
// The bug this fixes: when a bridge is killed / crashes / is OOM-reaped, its
// `.beacon/bridges/<sid>.json` claim file is left behind (graceful shutdown is
// the only path that clears it). The old rule counted such a claim as "another
// alive bridge" whenever its recorded `parent_pid` (= the owning bclaude) was
// still alive — even though the bridge process itself was already dead. That
// made the next bridge in the same bclaude force-mint a fresh session_id, which
// diverged from the session_id the CLI resolves (from CLAUDE_CODE_SESSION_ID /
// session.json). The split broke DM round-trips: sends were stamped with one
// session as sender and replies routed back to a session with no live bridge.
//
// Keying on the bridge pid instead: a claim only blocks reuse (forces a mint)
// when the bridge process it names is actually running.
import fs from 'node:fs'
import path from 'node:path'

// True iff `pid` is a live process on this host. `process.kill(pid, 0)` sends
// no signal but throws ESRCH when the pid is dead. EPERM = the process exists
// but we may not signal it (another user) → still alive.
export function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (e) {
    return !!(e && e.code === 'EPERM')
  }
}

// Decide whether one claim dict belongs to a DIFFERENT, currently-alive bridge
// (i.e. one that should force THIS bus.mjs to mint a fresh session_id).
//
// A claim is an "other alive bridge" iff ALL hold:
//   - it has a usable bridge `pid` and that process is alive (`pidAlive`). A dead
//     bridge's stale claim no longer counts (e-3858 — the original fix).
//   - it is not mine: not my own bridge pid, not my own bclaude's parent pid.
//   - it does not claim MY canonical session_id (`canonicalSid`) — the ms-165 rule.
// A claim without a usable `pid` cannot be confirmed running, so it does not block.
//
// ms-165 (restart hand-off race) — why the canonicalSid exclusion:
//   During a bclaude restart the dying old bridge briefly overlaps my new bus.mjs
//   startup. Its claim names my CANONICAL session_id, because the sid is minted on
//   (project, machine, BEACON_PARENT_PID) and BEACON_PARENT_PID = the TERMINAL pid
//   — a restart in the same terminal resolves the SAME sid. I must TAKE OVER that
//   sid, NOT force-mint a divergent one; otherwise the bridge heartbeats under a
//   fresh sid the CLI never resolves, so the session's DMs route to a bridge that
//   isn't there and receive dies silently (the 2026-09-05 incident). A genuinely
//   CONCURRENT bclaude runs in a DIFFERENT terminal → different BEACON_PARENT_PID →
//   DIFFERENT sid, so it is NOT excluded and still forces a mint (dispatch mode).
//   (Two LIVE processes sharing one canonical sid would require terminal-pid
//   RECYCLING within a session's lifetime — negligible, and the server expires
//   stale (project,machine,ppid) tuples by created_at/heartbeat. So "same
//   canonical sid ⟹ my own restart" holds in practice; we deliberately do NOT try
//   to disambiguate via parent_pid, since a legitimate restart ALSO has a
//   different parent_pid and such a check would false-fire on every restart.)
//
// When `canonicalSid` is empty (the CLI could not resolve a sid) the exclusion is
// a no-op, preserving the pre-ms-165 behaviour. The caller is expected to surface
// that degraded state (the exclusion — and thus the restart protection — is off).
export function claimIsOtherAliveBridge(data, { myPpid, myPid, canonicalSid = '', pidAlive = isPidAlive }) {
  if (!data || typeof data !== 'object') return false
  const pp = Number.isInteger(data.parent_pid) ? data.parent_pid : 0
  const bp = Number.isInteger(data.pid) ? data.pid : 0
  if (!bp || bp === myPid) return false      // no bridge pid, or it's my own bridge
  if (pp && pp === myPpid) return false       // my own bclaude's prior claim
  if (canonicalSid && data.session_id === canonicalSid) return false  // my own session restarting → take over
  return pidAlive(bp)                          // the bridge process must be live
}

// Scan `bridgesDir` (per-sid claims) and the legacy single-claim `legacyPath`,
// returning the claims that belong to other, currently-alive bridges. Pure
// except for the fs reads it is given; `pidAlive` is injectable for tests.
export function detectOtherAliveBridges({ bridgesDir, legacyPath, myPpid, myPid, canonicalSid = '', pidAlive = isPidAlive, log = () => {} }) {
  const found = []
  const seen = new Set()
  try {
    if (bridgesDir && fs.existsSync(bridgesDir)) {
      for (const f of fs.readdirSync(bridgesDir)) {
        if (!f.endsWith('.json')) continue
        let data
        try {
          data = JSON.parse(fs.readFileSync(path.join(bridgesDir, f), 'utf8'))
        } catch {
          continue
        }
        if (claimIsOtherAliveBridge(data, { myPpid, myPid, canonicalSid, pidAlive })) {
          found.push(data)
          if (data.session_id) seen.add(data.session_id)
        }
      }
    }
  } catch (e) {
    log(`bridges scan failed (non-fatal): ${e.message}`)
  }
  // Legacy single-claim .beacon/bridge.json (no parent_pid) — same bridge-pid
  // liveness rule; skip if the sid was already collected from bridges/.
  try {
    if (legacyPath && fs.existsSync(legacyPath)) {
      const data = JSON.parse(fs.readFileSync(legacyPath, 'utf8'))
      if (claimIsOtherAliveBridge(data, { myPpid, myPid, canonicalSid, pidAlive }) &&
          !(data && data.session_id && seen.has(data.session_id))) {
        found.push(data)
      }
    }
  } catch {}
  return found
}
