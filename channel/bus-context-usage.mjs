// ms-159 / e-6499 + e-6588 — read the context-usage state the monitor writes.
//
// bin/context-usage-monitor (beacon_cli/hooks/context_monitor.py) writes, on
// each Claude Code Stop hook, ONE record per Claude session under
// .claude/context-usage/<session_id>.json:
//   {session_id, notified_thresholds, context_pct, context_used, context_limit,
//    pids: [hook pid + ancestor pids], parent_pid?: BEACON_PARENT_PID,
//    updated_at}
// The bridge's poll loop picks THIS terminal's record here and piggybacks
// context_pct onto the existing heartbeat PUT (mirrors how bus-state-marker.mjs
// surfaces the session-state marker — no new send path), so the roster (bus
// directory / attention) can show which session is context-pressured.
//
// Why a directory and an identity match (e-6588): the pre-e-6588 shape was one
// per-cwd file, last-writer-wins. With two Claude sessions in the same folder
// the badge showed whichever session's hook ran last — wrong half the time — and
// the monitor's dedup state got reset on every alternation. The monitor now
// keys records by session; the bridge must therefore answer "which record is
// mine?". It cannot see the Claude Code session id (not propagated to MCP
// subprocess env — the same gap discoverSessionIdViaCLI works around), but it
// DOES know process.ppid = the Claude Code process, which is also an ancestor
// of the hook that wrote the record. So: strong match = record.pids includes my
// ppid; weak match = record.parent_pid equals my BEACON_PARENT_PID (the terminal
// pid bin/bclaude exports to every child; shared by successive Claude sessions
// in the same terminal, hence weaker — tie-break on updated_at). No match →
// null: better no badge than another session's badge. There is deliberately
// NO fallback to the legacy per-cwd file for the same reason.
//
// Fail-safe by construction: a missing directory (a cwd where the monitor never
// ran), unreadable / malformed records, and records with no context_pct all
// resolve to null = "no context_pct this heartbeat". A best-effort
// observability signal must never throw into the poll loop that delivers real
// events.

import fs from 'node:fs'
import path from 'node:path'

/**
 * Pick THIS terminal's record from the per-session directory (e-6588).
 *
 * @param {string} dirPath  Path to .claude/context-usage/.
 * @param {{ppid?: number, parentPid?: number}} me
 *   ppid = process.ppid (the Claude Code process); parentPid = numeric
 *   BEACON_PARENT_PID from the bridge's own env (undefined when unset).
 * @returns {{contextPct: number}|null}
 *   The matched record's percent, or null when the directory is absent, no
 *   record can be attributed to this terminal, or the matched record has no
 *   usable context_pct.
 */
export function readContextUsageForSession(dirPath, me) {
  let names
  try {
    names = fs.readdirSync(dirPath)
  } catch (e) {
    return null  // absent dir → monitor never ran here (common back-compat path)
  }
  const ppid = me && Number.isInteger(me.ppid) && me.ppid > 0 ? me.ppid : null
  const parentPid = me && Number.isInteger(me.parentPid) && me.parentPid > 0 ? me.parentPid : null
  let best = null
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    const rec = readRecord(path.join(dirPath, name))
    if (!rec) continue
    let score = 0
    if (ppid !== null && rec.pids.includes(ppid)) score = 2
    else if (parentPid !== null && rec.parentPid === parentPid) score = 1
    if (score === 0) continue
    if (!best || score > best.score ||
        (score === best.score && rec.updatedAt > best.rec.updatedAt)) {
      best = { score, rec }
    }
  }
  return best ? { contextPct: best.rec.contextPct } : null
}

// Parse one record; null unless it carries a valid context_pct. Identity fields
// are normalised (non-numeric entries dropped) so the matcher above never sees
// a malformed value. Deliberately NOT exported (#760 review F1/M1): the
// pre-e-6588 single-file reader `readContextUsage(path)` was removed rather
// than kept as a "shared parser" — an exported path-based reader is exactly
// the last-writer-wins surface this module exists to retire, and a future
// caller would reproduce the wrong-session badge without any error.
// `readContextUsageForSession` is the only public entry point.
function readRecord(statePath) {
  let raw
  try {
    raw = fs.readFileSync(statePath, 'utf-8')
  } catch (e) {
    return null
  }
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return null
    const pct = obj.context_pct
    // Accept only a finite number in [0, 100]. Anything else (missing, string,
    // NaN, out-of-range from a corrupt write) → treat as no declaration.
    if (!(typeof pct === 'number' && Number.isFinite(pct) && pct >= 0 && pct <= 100)) return null
    const pids = Array.isArray(obj.pids)
      ? obj.pids.filter((p) => Number.isInteger(p) && p > 0)
      : []
    const parentPid = Number.isInteger(obj.parent_pid) && obj.parent_pid > 0 ? obj.parent_pid : null
    const updatedAt = typeof obj.updated_at === 'string' ? obj.updated_at : ''
    return { contextPct: pct, pids, parentPid, updatedAt }
  } catch (e) {
    return null  // malformed JSON → no context_pct (never throw into the poll loop)
  }
}
