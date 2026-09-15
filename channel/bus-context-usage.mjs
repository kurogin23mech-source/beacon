// ms-159 / e-6499 — read the context-usage state file the monitor writes.
//
// bin/context-usage-monitor (beacon_cli/hooks/context_monitor.py) writes
// .claude/context-usage-state.json on each Claude Code lifecycle hook:
// {session_id, notified_thresholds, context_pct, context_used, context_limit}.
// The bridge's poll loop reads context_pct here and piggybacks it onto the
// existing heartbeat PUT (mirrors how bus-state-marker.mjs surfaces the
// session-state marker — no new send path), so the roster (bus directory /
// attention) can show which session is context-pressured.
//
// Fail-safe by construction: a missing file (a cwd where the monitor never
// ran), a malformed file, and a file with no context_pct all resolve to null =
// "no context_pct this heartbeat". A best-effort observability signal must never
// throw into the poll loop that delivers real events. Back-compat: an old state
// file (session_id + notified_thresholds only, no context_pct) reads as null and
// the heartbeat simply omits the field.
//
// NOTE on identity: the state file's `session_id` is the Claude Code session id
// (from the hook stdin), NOT the beacon-bus SESSION_ID (sv-…). They live in
// different namespaces, so we do NOT cross-check them. The file is per-cwd and
// the monitor resets it on Claude-session change, so "the context_pct in this
// cwd's file" is exactly "this terminal's current context usage" — which is what
// the bridge (also per-cwd) wants to report.

import fs from 'node:fs'

/**
 * Read the context-usage state at `statePath`.
 *
 * @param {string} statePath  Path to .claude/context-usage-state.json.
 * @returns {{contextPct: number}|null}
 *   The context-window usage percent, or null when the file is absent,
 *   unreadable, malformed, or carries no numeric `context_pct`. A percent of 0
 *   is a legitimate value (a fresh session) and is returned, not treated as
 *   absent — hence the explicit numeric/range check rather than a falsy test.
 */
export function readContextUsage(statePath) {
  let raw
  try {
    raw = fs.readFileSync(statePath, 'utf-8')
  } catch (e) {
    return null  // absent file → no context_pct (the common back-compat path)
  }
  try {
    const obj = JSON.parse(raw)
    const pct = obj && obj.context_pct
    // Accept only a finite number in [0, 100]. Anything else (missing, string,
    // NaN, out-of-range from a corrupt write) → treat as no declaration.
    if (typeof pct === 'number' && Number.isFinite(pct) && pct >= 0 && pct <= 100) {
      return { contextPct: pct }
    }
  } catch (e) {
    // malformed JSON → treat as no context_pct (never throw into the poll loop)
  }
  return null
}
