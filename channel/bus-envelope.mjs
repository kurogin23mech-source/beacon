// Bus envelope client helper (ms-54 / e-1290).
//
// Mirrors a slim subset of server/envelope.py so channel/bus.mjs can:
//   * decide whether an outgoing payload fits the T5 short-ping schema,
//   * pick a tier (T5 for ping-shape, T1 for everything else from this bridge),
// before it asks the server to mint an envelope.
//
// The server is still the source of truth — it re-validates the payload
// against the same rules at /bus/envelope/issue and again at /bus. The client
// runs the check up-front so it can avoid a round-trip when the verdict is
// obvious, and so the unit tests below pin the contract.
//
// Keep these constants in lockstep with server/envelope.py:
//   T5_PING_KEYS, T5_PING_VALUE_MAX_LEN, TIER_T1, TIER_T5.

export const TIER_T1 = 'T1'
export const TIER_T5 = 'T5'

export const T5_PING_KEYS = new Set(['ping', 'ack', 'status', 'kind', 'ts'])
export const T5_PING_VALUE_MAX_LEN = 32

/**
 * Returns null if `payload` fits the T5 short-ping schema, else a string
 * explaining the violation. Mirrors server/envelope.py:validate_t5_payload.
 *
 * Short-ping schema:
 *   * payload is a plain object
 *   * all keys are in T5_PING_KEYS
 *   * all values are primitives (string ≤32 chars, number, bool, null)
 *
 * @param {unknown} payload
 * @returns {string | null}
 */
export function validateT5Payload(payload) {
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    return 'T5 payload must be a dict'
  }
  const keys = Object.keys(payload)
  const extra = keys.filter((k) => !T5_PING_KEYS.has(k))
  if (extra.length > 0) {
    return `T5 payload keys not in allowlist: ${JSON.stringify(extra.sort())}`
  }
  for (const key of keys) {
    const value = payload[key]
    if (typeof value === 'string') {
      if (value.length > T5_PING_VALUE_MAX_LEN) {
        return `T5 payload field "${key}" exceeds ${T5_PING_VALUE_MAX_LEN} chars`
      }
      continue
    }
    if (
      value === null ||
      typeof value === 'number' ||
      typeof value === 'boolean'
    ) {
      continue
    }
    return `T5 payload field "${key}" must be primitive`
  }
  return null
}

/**
 * Pick the envelope tier for an outgoing MCP-bridge message.
 *
 * Rule (from e-1290 task brief):
 *   * No requested action AND payload conforms to T5 short-ping → T5
 *   * Otherwise → T1, actions_authorized=[]
 *
 * T3 reply chains are intentionally out of scope for this client; the
 * receiving session can still treat in_reply_to as a thread anchor without
 * needing T3 to be claimed on the wire.
 *
 * @param {object} payload - the bus payload that will be POSTed
 * @param {string | null | undefined} requestedAction - action the receiver
 *   should be authorized to auto-execute, if any
 * @returns {{tier: string, actionsAuthorized: string[], dataClass: string}}
 */
export function selectTierForBridge(payload, requestedAction) {
  if (
    !requestedAction &&
    validateT5Payload(payload) === null
  ) {
    return { tier: TIER_T5, actionsAuthorized: [], dataClass: 'free' }
  }
  return { tier: TIER_T1, actionsAuthorized: [], dataClass: 'free' }
}
