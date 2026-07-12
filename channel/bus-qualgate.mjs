// channel/bus-qualgate.mjs (ms-100 e-3308)
//
// Qualitative gate for the AI's *outbound autonomous reply* (armed mode).
//
// The budget gate (bus-budget.mjs) is the QUANTITATIVE half of armed safety:
// "you may auto-send at most N times". This module is the QUALITATIVE half:
// even within budget, some categories of autonomous send should never go out
// without a human seeing them. It classifies a reply and decides whether armed
// mode may send it autonomously or must HOLD it for the human (= structural
// pending), instead of relying on the armed Skill's prompt to "remember" to
// ask (ms-100 SPEC PVaNf6HYFjucgBS3lkQF: the qualitative safety was on the AI's
// disposition; this moves the dangerous categories into a hard code gate).
//
// Hybrid decision (SPEC): 通常会話 = structurally allowed (B芯); 外部宛 / 機密 /
// action付き = structurally held for the human (A gate).
//
// Category signals that are STRUCTURALLY determinable at the reply tool:
//   * external   — a plain `dm` to a DIFFERENT project (= autonomously messaging
//                  a stranger's project). Trek-scoped channels (trek-* /
//                  operation-trigger) are a pre-approved shared scope (the human
//                  joined the Trek = blanket consent, ms-70 shared_trek), so they
//                  are NOT external even across projects.
//   * action     — the envelope authorises actions. The MCP reply tool sends
//                  text-only pings (empty actions), so this rarely fires here,
//                  but the category is kept for completeness / future callers.
//   * confidential — content-sensitive. Deferred to disclosure_policy (ms-63);
//                  this module only holds when a caller passes an explicit
//                  ``confidential`` signal. Rule-based content classification is
//                  intentionally out of scope (that is ms-63's job).
// Everything else is ``normal_chat`` and flows autonomously (B芯).

// Trek-scoped / operation channels: a shared pre-approved working scope. Sends
// on these are covered by the Trek's blanket consent, so they are never treated
// as "external" even cross-project.
export const TREK_SCOPED_CHANNELS = new Set([
  'operation-trigger',
  'trek-progress-check',
  'trek-trigger',
  'trek-task-review',
  'trek-leader-digest',
])

export function isTrekScopedChannel(channel) {
  const c = String(channel || '')
  return c.startsWith('trek-') || TREK_SCOPED_CHANNELS.has(c)
}

export const CAT_NORMAL = 'normal_chat'
export const CAT_EXTERNAL = 'external'
export const CAT_ACTION = 'action'
export const CAT_CONFIDENTIAL = 'confidential'

export function classifyOutboundReply({
  channel = '',
  senderProjectId = '',
  recipientProjectId = '',
  actionsAuthorized = null,
  confidential = false,
} = {}) {
  // Confidential wins (most restrictive) — but only when an explicit signal is
  // provided (ms-63 disclosure integration point; no content sniffing here).
  if (confidential) return CAT_CONFIDENTIAL
  if (Array.isArray(actionsAuthorized) && actionsAuthorized.length > 0) {
    return CAT_ACTION
  }
  // Trek-scoped channels are a pre-approved shared scope → never "external".
  if (isTrekScopedChannel(channel)) return CAT_NORMAL
  // Plain DM to a different project = autonomously messaging outside our project.
  if (
    recipientProjectId && senderProjectId &&
    recipientProjectId !== senderProjectId
  ) {
    return CAT_EXTERNAL
  }
  return CAT_NORMAL
}

export function evaluateOutboundQualGate({ classification, armed }) {
  // Returns { hold, category, reason }.
  //   * not armed         → never hold (the qual gate only constrains the AI's
  //                          autonomous sends; a human-driven send is its own
  //                          approval, same posture as the budget gate).
  //   * normal_chat       → autonomous send allowed (B芯).
  //   * external/action/confidential → HOLD for the human (A gate).
  if (!armed) {
    return { hold: false, category: classification, reason: 'not_armed' }
  }
  if (classification === CAT_NORMAL) {
    return { hold: false, category: classification, reason: CAT_NORMAL }
  }
  return { hold: true, category: classification, reason: `${classification}_needs_human` }
}

export function qualHoldMessage(category) {
  // Human-facing refusal text; mirrors bus-budget.mjs's refuseMessage style.
  if (category === CAT_EXTERNAL) {
    return (
      'reply held: this is an autonomous cross-project message to another ' +
      "project's bus. armed mode holds外部宛 (external) sends for you to review. " +
      'Send it yourself (e.g. via /beacon-dm-send) if intended, or share a Trek ' +
      'so the scope is pre-approved.'
    )
  }
  if (category === CAT_CONFIDENTIAL) {
    return (
      'reply held: flagged confidential. armed mode holds 機密 (confidential) ' +
      'sends for you to review before it leaves this session.'
    )
  }
  if (category === CAT_ACTION) {
    return (
      'reply held: this reply carries an authorised action. armed mode holds ' +
      'action付き sends for your approval (a text ping would not be held).'
    )
  }
  return 'reply held for human review.'
}
