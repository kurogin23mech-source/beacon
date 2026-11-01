/**
 * AUTONOMOUS ACTION imperative content builder + gating for the MCP push path.
 *
 * e-1417 (ms-60): historically the MCP `notifications/claude/channel` route
 * only carried situational awareness ("a thing arrived") and the actual
 * "Run this autonomously without asking the user first" imperative was added
 * exclusively by the UserPromptSubmit hook side
 * (`bin/beacon-bus-inbox-hook.py:_format_autonomous_action_block`). The
 * consequence was a structural UX gap: even after a user `opt-in`'d an
 * Operation via `beacon bus auto-execute add`, the Skill would not launch
 * until the user typed at least one prompt to wake the hook. This broke the
 * "set it and forget it" expectation surfaced in the PE cross-project
 * dogfood (e-1413, 2026-06-10).
 *
 * This helper lets the MCP push route emit the same imperative block format
 * so the AI harness sees the launch instruction regardless of which route
 * delivered the event. The Python format must stay in lockstep — the
 * accompanying test (`tests/test_channel_bus_autonomous_content.py`) pins
 * the contract so a future divergence is loud, not silent.
 *
 * Opt-out: `BEACON_BRIDGE_MCP_AUTONOMOUS_DISABLE=1` returns false from the
 * gate, restoring the slim-ping-only push behaviour for callers that hit a
 * regression during the dogfood and need to roll back without redeploying.
 *
 * e-2069 (ms-75): the CHANNEL_TO_SKILL mapping makes the Skill invocation
 * structural — bus.mjs emits the launch directive from the channel name
 * alone, without depending on AI interpretation of the payload. This closes
 * the silent-ack hole where compliance to the inbox-hook narrative depended
 * on AI pattern-matching.
 */

/**
 * Hardcoded mapping from bus channel name to Claude Code Skill invocation.
 * bus.mjs uses this to translate an incoming event into a "launch this Skill
 * now" directive without depending on AI interpretation of the channel
 * semantics. Add new channels here when introducing new autonomous flows.
 */
export const CHANNEL_TO_SKILL = Object.freeze({
  'operation-trigger': '/beacon-operation-execute',
})

function _buildOperationActionContent(evt) {
  const payload = (evt && evt.payload) || {}
  const opId = String(payload.op_id || '?')
  const specDocId = String(payload.spec_doc_id || '')
  const triggerName = String(payload.trigger_name || '')
  const eventId = String((evt && evt.event_id) || '?')
  const lines = [
    '## AUTONOMOUS ACTION — operation autonomy active',
    '',
    '以下の operation-trigger event は `bus_auto_execute_channels` allowlist に登録済 (= 人間 opt-in 済) のため、**確認を取らず autonomous に実行する**。',
    'Run this autonomously without asking the user first.',
    '',
    `- event_id: ${eventId}`,
    `  - op_id: ${opId}`,
  ]
  if (specDocId) lines.push(`  - spec_doc_id: ${specDocId}`)
  if (triggerName) lines.push(`  - trigger_name: ${triggerName}`)
  lines.push(`  - launch: \`/beacon-operation-execute ${opId}\`  (no confirmation prompt)`)
  lines.push('')
  lines.push('budget が枯渇していたら Skill 側 (Step 4.5) が自動で停止 → incident open → note 残しに降格する。')
  return lines.join('\n')
}

/**
 * Build the autonomous-action content block for the MCP push route.
 *
 * Every mapped channel currently produces the AUTONOMOUS ACTION block
 * (op launch). Events without a recognised channel fall back to the same
 * format (the historical default). Add a dispatch branch here when a new
 * channel needs a different imperative.
 */
export function buildAutonomousActionContent(evt) {
  return _buildOperationActionContent(evt)
}

/**
 * Gate: only emit the autonomous-action imperative when the channel × delivery
 * combination is one we have a hardcoded Skill mapping for, and the user has
 * not opted out via `BEACON_BRIDGE_MCP_AUTONOMOUS_DISABLE`.
 */
export function shouldEmitAutonomousImperative({
  channel,
  delivery,
  autonomousImperativeDisabled,
}) {
  if (autonomousImperativeDisabled) return false
  if (delivery !== 'auto-execute') return false
  if (!Object.prototype.hasOwnProperty.call(CHANNEL_TO_SKILL, channel)) return false
  return true
}
