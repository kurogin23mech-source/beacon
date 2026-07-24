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
 * e-2069 (ms-75): extended to trek-progress-check / trek-trigger /
 * trek-task-review channels. The CHANNEL_TO_SKILL mapping makes the Skill
 * invocation structural — bus.mjs directly emits the launch directive based
 * on the channel name, without depending on AI interpretation of the
 * payload. This closes the silent-ack hole observed in the 2026-06-19
 * dogfood (executor recognised the trek-progress-check event but skipped
 * the Skill launch because compliance to the inbox-hook narrative depended
 * on AI pattern-matching).
 */

/**
 * Hardcoded mapping from bus channel name to Claude Code Skill invocation.
 * bus.mjs uses this to translate an incoming event into a "launch this Skill
 * now" directive without depending on AI interpretation of the channel
 * semantics. Add new channels here when introducing new autonomous flows.
 */
export const CHANNEL_TO_SKILL = Object.freeze({
  'operation-trigger': '/beacon-operation-execute',
  'trek-progress-check': '/beacon-trek-execute',
  'trek-trigger': '/beacon-trek-execute',
  'trek-task-review': '/beacon-trek-review',
  // ms-92 / e-2164 — leader-digest channel. Recipient is the trek
  // leader's session; the digest carries aggregated per-executor
  // status (= built from pulse-ack structured fields per e-2165) so
  // the leader can spot stuck / idle / needs-leader-judgment sessions
  // without polling. The Skill the leader's AI is expected to invoke
  // is the same /beacon-trek-execute (= leader stance reminder lives
  // inside that Skill, e-2166), but the dispatcher passes the digest
  // payload through so the Skill can render structured output. If
  // /beacon-trek-leader or a dedicated digest Skill is added later
  // (= part of the Trek-leader stance work), retarget here.
  'trek-leader-digest': '/beacon-trek-execute',
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

function _buildTrekProgressContent(evt) {
  const payload = (evt && evt.payload) || {}
  const trekId = String(payload.trek_id || '?')
  const triggerName = String(payload.trigger_name || '')
  const eventId = String((evt && evt.event_id) || '?')
  const channel = String((evt && evt.channel) || '?')
  // ms-88 / e-2107: server passes the TTL deadline + last activity stamp so
  // the narrative carries actionable "by when" data, not a soft recommendation.
  // These fields are optional during Phase 1 land — the narrative degrades
  // gracefully when the server hasn't been redeployed yet.
  const ttlDeadline = String(payload.ttl_deadline_at || '')
  const lastActivityAt = String(payload.last_activity_at || '')
  const ttlMinutes = payload.ttl_minutes
  // ms-88 / e-2105 (Phase 1 narrative 強化, ms-88 CORE doc 5nfTSmCDVUzD4SLzIhI5):
  // 旧 narrative (= 「確認を取らず autonomous に実行する」) は AI compliance
  // に依存する soft recommendation だった。 dogfood (= tk-40b0b27c) で executor
  // が「Skill を起動してもしなくても罰則がない」 と internal narrative 化して
  // silent halt する病理が露呈。 強い言い回し + 明示的 consequence + 期限を
  // 添えて「無視できる推奨」 vs 「期限付き必須」 の区別を AI に渡す。
  //
  // 真の強制は server-side TTL safety net (= Layer 3 / e-2107) が担い、 ここは
  // Layer 1 (= narrative) の責務に集中する。 「narrative だけで動く保証はない」
  // ことを前提に、 narrative はあくまで 3 層の最も柔らかい層として機能する。
  const lines = [
    '## ⚠ TREK ACTION REQUIRED — 12 分以内に実行 (= 怠ると auto-stall)',
    '',
    '以下の ' + channel + ' event は Trek scope (= 缶詰の徹夜作業部屋、 事前承認スコープ) 内の作業継続経路。',
    '',
    '**必須アクション** (= 12 分以内、 怠ると server-side TTL safety net (= ms-88 / e-2107) が当該 session の **全 working task を一括 leader_review に強制遷移** し、 leader review DM を発火する):',
    '',
    '  a. `/beacon-trek-pulse ' + trekId + '` を起動 (= 推奨経路、 5 択 picker: terminal / continue / dm-leader / dm-peer (= 横向き相談、 ms-88 / e-2140 peer-first) / no-op)',
    '  b. または `/beacon-trek-execute ' + trekId + '` を継続 (= 既存 executor loop)',
    '  c. または `beacon trek task-state` で working stamp を更新 (= manual fallback)',
    '',
    'いずれも取らない場合 = silent halt と扱い、 server が罰則 (= 上記 leader_review 強制遷移) を取ります。 「次 tick まで待つ」 は許可されません。',
    '',
    'Trek scope (= MS / task / Operation) の作業は user 確認なしで進めてよい。 **例外: デプロイ / リリース / 不可逆 external write** のみ user 承認境界として escalate する (= state `user_review` に遷移)。',
    '',
    `- event_id: ${eventId}`,
    `  - trek_id: ${trekId}`,
  ]
  if (triggerName) lines.push(`  - trigger_name: ${triggerName}`)
  if (lastActivityAt) lines.push(`  - last_activity_at: ${lastActivityAt}`)
  if (ttlDeadline) lines.push(`  - ttl_deadline_at: ${ttlDeadline}`)
  if (ttlMinutes !== undefined && ttlMinutes !== null) {
    lines.push(`  - ttl_minutes: ${ttlMinutes}`)
  }
  lines.push('')
  lines.push('budget が枯渇していたら Skill 側 (Step 4.5) が自動で停止 → incident open → note 残しに降格する。Skill の責務なのでこの inject 側で事前判定はしない。')
  lines.push('> Note (= e-4116 / ms-75): この Trek 内の member 間 DM (leader↔executor、同一ユーザーの複数 session を含む) は auto-reply budget を **消費しない** (= server 側で Trek-internal と判定されると bypass)。budget 枯渇の心配は Trek 外向きの送信だけに当たる。')
  lines.push('')
  lines.push('> Note (= ms-88 CORE 5nfTSmCDVUzD4SLzIhI5): この narrative は 3 層強制構造の Layer 1 (= 明示的 consequence 提示)。 Layer 2 (= /beacon-trek-pulse による observable ack) と Layer 3 (= server TTL 罰則) と組合さって初めて「実質的強制」 が成立する。 narrative 単独では AI compliance 依存。')
  return lines.join('\n')
}

function _buildTrekTaskReviewContent(evt) {
  const payload = (evt && evt.payload) || {}
  const trekId = String(payload.trek_id || '?')
  const taskId = String(payload.task_id || '?')
  const state = String(payload.state || '?')
  const note = String(payload.note || '').trim()
  const eventId = String((evt && evt.event_id) || '?')
  const lines = [
    '## TREK ACTION — trek task review required',
    '',
    '以下の trek-task-review event は executor が terminal state を宣言した task の leader review 必須経路。**確認を取らず autonomous に実行する** (= forced 3-択 picker)。',
    'leader は approve / re-work / forward-to-user のいずれかを必ず選ぶ。 「後で見る」 は許可しない構造 (= leader bottleneck 病理の構造解消)。',
    'Run this autonomously without asking the user first.',
    '',
    `- event_id: ${eventId}`,
    `  - trek_id: ${trekId}`,
    `  - task_id: ${taskId}`,
    `  - state: ${state}`,
  ]
  if (note) {
    const notePreview = note.length > 200 ? note.slice(0, 200) + '…' : note
    lines.push(`  - executor note: ${notePreview}`)
  }
  lines.push(`  - launch: \`/beacon-trek-review ${trekId} ${taskId}\`  (no confirmation prompt)`)
  lines.push('')
  return lines.join('\n')
}

function _buildTrekLeaderDigestContent(evt) {
  // ms-92 / e-2164 — leader-digest narrative. The payload itself carries
  // the structured ``summary`` + ``sessions[]`` rendered by
  // ``build_leader_digest_payload`` (= lib/trek_scheduler.py). The
  // imperative wrapper tells the leader's AI the dispatch contract:
  // "this is for you, you're the leader, render it for the user, then
  // decide if you want to act on anyone in the stuck / needs-leader
  // band."
  const payload = (evt && evt.payload) || {}
  const trekId = String(payload.trek_id || '?')
  const eventId = String((evt && evt.event_id) || '?')
  const summary = payload.summary || {}
  const lines = [
    '## TREK LEADER DIGEST — 集約 status surface (= ms-92 / e-2164)',
    '',
    '以下の trek-leader-digest event は Trek leader 専用の集約 surface。 同 Trek 内の全 executor (= 実行担当 session) の最新 status (= pulse-ack 構造化 payload、 e-2165) を 1 event に集約したスナップショットが届いている。 **確認を取らず autonomous に処理する** (= leader stance Skill 起動)。',
    'Run this autonomously without asking the user first.',
    '',
    `- event_id: ${eventId}`,
    `  - trek_id: ${trekId}`,
    `  - active=${summary.active ?? 0} stuck=${summary.stuck ?? 0} idle=${summary.idle ?? 0} needs_leader_judgment=${summary.needs_leader_judgment ?? 0}`,
    `  - launch: \`/beacon-trek-execute ${trekId}\`  (no confirmation prompt)`,
    '',
    'leader として: stuck / needs_leader_judgment のあった executor session に DM で push、 全体が idle なら progress-check の cadence 見直し、 全部 working なら何もしない。 詳細 sessions[] は payload を参照。',
  ]
  return lines.join('\n')
}

/**
 * Build the autonomous-action content block for the MCP push route.
 *
 * Dispatches on the channel name to produce a channel-specific imperative:
 *   - operation-trigger  → AUTONOMOUS ACTION block (op launch)
 *   - trek-progress-check / trek-trigger → TREK ACTION (executor invocation)
 *   - trek-task-review   → TREK ACTION (leader review picker)
 *   - trek-leader-digest → TREK LEADER DIGEST (ms-92 / e-2164)
 *
 * For backward compatibility, events without a recognised channel fall back
 * to the operation-trigger format (the historical default).
 */
export function buildAutonomousActionContent(evt) {
  const ch = String((evt && evt.channel) || '')
  if (ch === 'trek-progress-check' || ch === 'trek-trigger') {
    return _buildTrekProgressContent(evt)
  }
  if (ch === 'trek-task-review') {
    return _buildTrekTaskReviewContent(evt)
  }
  if (ch === 'trek-leader-digest') {
    return _buildTrekLeaderDigestContent(evt)
  }
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
