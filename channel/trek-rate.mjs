// channel/trek-rate.mjs (e-4116 follow-up / PR #491 parent review 2)
//
// Per-trek rate brake for Trek-internal auto-replies.
//
// e-4116 lets Trek-internal DMs bypass the budget gate (a Trek is a
// pre-approved scope with its own TTL / halt controls). But that removed the
// ONLY volume cap on a leader↔executor autonomous DM loop — exactly the
// construction this PR enables — which could then spin unbounded until a manual
// halt or the 24h TTL. The budget gate's whole reason for existing (runaway
// prevention) would be gone for the trek path.
//
// This restores a light brake WITHOUT reintroducing the deadlock e-4116 fixed:
// at most N Trek-internal auto-replies per trek within a ROLLING window. Past
// that, the send is HELD for the human. The window slides, so it self-heals
// (unlike the budget gate, which needed an explicit re-grant) — normal
// coordination never wedges, only a pathological burst is caught.
//
// Pure decision (``decideRate``) is separated from IO so the window math is
// unit-tested without a filesystem / clock.

import fs from 'node:fs'
import path from 'node:path'

export const DEFAULT_CAP = 30
export const DEFAULT_WINDOW_SEC = 600  // 10 min

function statePath(projectRoot) {
  return path.join(projectRoot, '.beacon', 'trek-reply-rate.json')
}

export function readRateState(projectRoot) {
  const p = statePath(projectRoot)
  try {
    if (!fs.existsSync(p)) return {}
    const parsed = JSON.parse(fs.readFileSync(p, 'utf8'))
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return parsed
  } catch (_e) {
    return {}
  }
}

// Pure decision: given the recorded reply timestamps (ms since epoch) for one
// trek, ``nowMs``, the cap and the window, return
//   { allowed, count, recent }
// where ``recent`` is the pruned timestamp list to persist (with ``nowMs``
// appended iff allowed). ``count`` is the post-decision count (what the state
// would hold), so an over-limit hold reports the current burst size.
export function decideRate(timestamps, nowMs, cap = DEFAULT_CAP, windowSec = DEFAULT_WINDOW_SEC) {
  const cutoff = nowMs - windowSec * 1000
  const recent = (Array.isArray(timestamps) ? timestamps : []).filter(
    (t) => typeof t === 'number' && t > cutoff,
  )
  if (recent.length >= cap) {
    return { allowed: false, count: recent.length, recent }
  }
  return { allowed: true, count: recent.length + 1, recent: [...recent, nowMs] }
}

// IO wrapper: check + record one Trek-internal reply for ``trekId``. Returns
// { allowed, count, cap }.
//
// Failure posture: on a read/parse error we treat state as empty (allow) and
// on a write error we still allow. The brake is best-effort — its job is to
// catch a pathological loop, not to be a hard security gate — and the human
// halt / 24h TTL remain as backstops. Failing CLOSED here would let a corrupt
// state file wedge a perfectly healthy trek, which is worse.
export function checkAndRecordTrekReply(projectRoot, trekId, opts = {}) {
  const cap = opts.cap ?? DEFAULT_CAP
  const windowSec = opts.windowSec ?? DEFAULT_WINDOW_SEC
  const nowMs = opts.nowMs ?? Date.now()
  if (!trekId) return { allowed: true, count: 0, cap }
  const state = readRateState(projectRoot)
  const decision = decideRate(state[trekId], nowMs, cap, windowSec)
  if (decision.allowed) {
    state[trekId] = decision.recent
    try {
      fs.writeFileSync(statePath(projectRoot), JSON.stringify(state, null, 2), 'utf8')
    } catch (_e) { /* best-effort — see failure posture above */ }
  }
  return { allowed: decision.allowed, count: decision.count, cap }
}

export function rateHoldMessage(trekId, count, cap, windowSec = DEFAULT_WINDOW_SEC) {
  const mins = Math.round(windowSec / 60)
  return (
    `⚠ Trek ${trekId} 内の自動返信が直近 ${mins} 分で上限 (${cap} 回) に達しました。` +
    `runaway loop の疑いがあるため、この返信は保留します。続行が必要なら、人間が` +
    `状況を確認して手動で送る (--manual) か、レート上限を見直してください` +
    ` (BEACON_TREK_RATE_CAP / BEACON_TREK_RATE_WINDOW_SEC で調整、` +
    `BEACON_TREK_RATE_OFF=1 で無効化)。`
  )
}
