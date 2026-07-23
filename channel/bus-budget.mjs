// channel/bus-budget.mjs (ms-54 / e-1193)
//
// Budget-gate primitive for the bus MCP reply tool. Extracted from bus.mjs
// so the gate can be unit-tested without standing up the full MCP server
// (which has long-lived polling, MCP handshake, etc.).
//
// Shape: the budget file is .beacon/bus-budget.json under a project root the
// caller passes in. Format matches lib/commands.py's _read_bus_budget /
// _bus_budget_consume_one — see those docstrings for the semantic contract.
// We replicate the contract here rather than RPC-ing into Python because:
//   * Python startup per reply would be noticeable in the MCP hot path.
//   * We want a single, type-checked path in Node for the gate so test
//     coverage stays mechanical.
//
// The caller (bus.mjs) decides WHETHER to invoke the gate (it only fires for
// replies). This module is the HOW of the gate: read, decrement, persist,
// report.

import fs from 'node:fs'
import path from 'node:path'

function budgetPath(projectRoot) {
  return path.join(projectRoot, '.beacon', 'bus-budget.json')
}

export function readBusBudget(projectRoot) {
  // Returns one of:
  //   { state: 'missing' }     — file absent; default OFF, refuse autonomous replies
  //   { state: 'corrupt' }     — read or parse failed; fail-closed
  //   { state: 'present', data: {...} } — successfully parsed budget object
  const p = budgetPath(projectRoot)
  if (!fs.existsSync(p)) {
    return { state: 'missing' }
  }
  try {
    const raw = fs.readFileSync(p, 'utf8')
    const parsed = JSON.parse(raw)
    // Schema is "plain object with total/used". Reject arrays and other
    // non-object JSON (null, number, string, boolean) as corrupt — a naive
    // arr.total === undefined would silently land in the "total<=0 → allow"
    // sentinel and bypass the gate.
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { state: 'corrupt' }
    }
    return { state: 'present', data: parsed }
  } catch (_e) {
    return { state: 'corrupt' }
  }
}

export function isArmed(projectRoot) {
  // ms-120 / e-3901: True iff an auto-reply budget is granted (a present budget
  // file with total > 0) — the durable "autonomous mode active" signal the send
  // gate keys off, so an armed loop can't bypass the gate by omitting
  // --in-reply-to. Mirrors lib/commands.py _bus_is_armed(). Lives here (not
  // inlined in bus.mjs) so the unwrap is unit-tested: reading readBusBudget()'s
  // { state, data } shape wrong once silently disarmed the whole MCP path.
  const read = readBusBudget(projectRoot)
  return read.state === 'present'
    && Number.parseInt(read.data?.total ?? 0, 10) > 0
}

export function consumeBusBudgetOne(projectRoot) {
  // Returns { allowed, reason?, budget? }.
  //
  // Semantics — in lockstep with lib/commands._bus_budget_consume_one on the
  // ARMED cases, but INTENTIONALLY DIVERGENT on the missing-file case:
  //   * missing file        → refuse, reason='not_granted' (default OFF)
  //         ^ The ONE place the MCP gate deliberately differs from the CLI
  //           gate: the CLI ALLOWS on a missing file (a human's manual
  //           `beacon bus send` must not require `grant` first); the MCP gate
  //           REFUSES (the AI's autonomous replies are default-OFF, an explicit
  //           grant is required). human-vs-AI, by design — do NOT "align" them.
  //           Pinned by tests/test_bus_budget_asymmetry.py (ms-100 e-3310);
  //           background in SPEC PVaNf6HYFjucgBS3lkQF ("armed の本質").
  //   * corrupt file        → refuse, reason='corrupt' (fail-closed)
  //   * total<=0            → allow without decrement (legacy "file present
  //                            but not armed"); budget.armed=false
  //   * used >= total       → refuse, reason='exhausted'
  //   * write failure on    → refuse, reason='corrupt' (don't send without
  //     decrement persistence  recording the slot — else gate becomes advisory)
  //   * otherwise           → decrement persistently, allow.
  const read = readBusBudget(projectRoot)
  if (read.state === 'missing') {
    return { allowed: false, reason: 'not_granted' }
  }
  if (read.state === 'corrupt') {
    return { allowed: false, reason: 'corrupt' }
  }
  const data = read.data
  const total = Number.parseInt(data.total ?? 0, 10) || 0
  const used = Number.parseInt(data.used ?? 0, 10) || 0
  if (total <= 0) {
    return { allowed: true, budget: { total, used, armed: false } }
  }
  if (used >= total) {
    return {
      allowed: false,
      reason: 'exhausted',
      budget: { total, used, armed: true },
    }
  }
  const next = { ...data, used: used + 1 }
  try {
    fs.writeFileSync(budgetPath(projectRoot), JSON.stringify(next, null, 2), 'utf8')
  } catch (_e) {
    return { allowed: false, reason: 'corrupt' }
  }
  return {
    allowed: true,
    budget: { total: next.total, used: next.used, armed: true },
  }
}

export function refundBusBudgetOne(projectRoot) {
  // Give back one consumed slot (ms-100 e-2999). consumeBusBudgetOne is a
  // *pessimistic* decrement — it commits the slot BEFORE the send happens so
  // the gate can't be raced. If the send is then held (qual gate) or fails
  // (network / server error), that slot was never actually used, so refund it.
  // Best-effort and idempotent-ish: clamps ``used`` at 0 so a double refund or
  // a refund with no prior consume can't drive the counter negative.
  //
  // Returns { refunded: bool, budget?: {...} }. refunded=false when there is
  // no armed budget file to refund against (nothing was consumed there).
  const read = readBusBudget(projectRoot)
  if (read.state !== 'present') {
    return { refunded: false }
  }
  const data = read.data
  const total = Number.parseInt(data.total ?? 0, 10) || 0
  const used = Number.parseInt(data.used ?? 0, 10) || 0
  if (total <= 0 || used <= 0) {
    // total<=0 → not armed (consume didn't decrement); used<=0 → nothing to
    // give back. Either way leave the file untouched.
    return { refunded: false, budget: { total, used, armed: total > 0 } }
  }
  const next = { ...data, used: used - 1 }
  try {
    fs.writeFileSync(budgetPath(projectRoot), JSON.stringify(next, null, 2), 'utf8')
  } catch (_e) {
    return { refunded: false }
  }
  return {
    refunded: true,
    budget: { total: next.total, used: next.used, armed: true },
  }
}


export function refuseMessage(reason, budget) {
  // Centralised error text so CLI / MCP / docs stay aligned. The strings
  // explicitly reference `beacon bus budget grant` so the user always sees
  // the same recovery path regardless of which gate refused them.
  if (reason === 'not_granted') {
    return (
      'reply refused: bus budget not granted. ' +
      'Run `beacon bus budget grant --turns N` to authorize N auto-replies, ' +
      'then retry.'
    )
  }
  if (reason === 'exhausted') {
    const b = budget || {}
    return (
      `reply refused: bus budget exhausted (${b.used || 0}/${b.total || 0} used). ` +
      'Run `beacon bus budget grant --turns N` to re-grant before sending again.'
    )
  }
  return (
    'reply refused: bus budget file is unreadable. ' +
    'Inspect .beacon/bus-budget.json (use `beacon bus budget show`) and ' +
    'run `beacon bus budget grant --turns N` to reset.'
  )
}
