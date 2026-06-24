#!/usr/bin/env node
// Beacon bus channel MCP server (ms-54 e-1152).
//
// Bridges Beacon Cloud /bus events into the local Claude Code session via the
// Channels MCP protocol, so an idle session wakes up on incoming DMs without
// requiring the user to type anything.
//
// Flow:
//   [Beacon Cloud /bus]
//     ↑ POST  (reply tool — supports cross-project)
//     ↓ poll  /api/projects/{id}/bus/unread?recipient_id=<my session_id>
//   [channel/bus.mjs] ─stdio─ [Claude Code session]
//
// Discovery (env wins, falls back to local files):
//   BEACON_PROFILE          ← .beacon/cloud.json.profile  ← "default"
//                             (profile name; see profile-resolver.mjs)
//   BEACON_API_URL          ← .beacon/cloud.json.api_url
//                             ← ~/.beacon/profiles/<name>/profile.json.api_url
//                             ← https://beacon-ai.dev
//   BEACON_PROJECT_ID       ← .beacon/cloud.json.project_id
//   BEACON_SESSION_ID       ← .beacon/session.json.session_id
//   BEACON_AUTH_TOKEN       ← ~/.beacon/profiles/<name>/credentials.json.token
//                             (legacy fallback: ~/.beacon/credentials.json
//                              only when profile == "default" and the new
//                              path is not yet created by silent migration)
//   BEACON_CHANNEL_ALLOWLIST (csv, default "dm")
//   BEACON_BUS_POLL_MS      (default 5000、ms-84 e-2366)
//   BEACON_BUS_LOG          (default /tmp/beacon-bus-channel.log)

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { consumeBusBudgetOne, refuseMessage } from './bus-budget.mjs'
import { selectTierForBridge } from './bus-envelope.mjs'
import { buildHeartbeatBody } from './bus-heartbeat.mjs'
import { createLocalSessionHeartbeat } from './bus-local-heartbeat.mjs'
import {
  buildAutonomousActionContent,
  shouldEmitAutonomousImperative,
} from './bus-autonomous-content.mjs'
import {
  resolveActiveProfile,
  loadToken as loadTokenFromProfile,
} from './profile-resolver.mjs'

// --- Config discovery --------------------------------------------------------

const CWD = process.cwd()
const CLOUD_JSON = path.join(CWD, '.beacon', 'cloud.json')
const SESSION_JSON = path.join(CWD, '.beacon', 'session.json')

// ms-64 / e-1459: profile-aware credentials + api_url resolution.
// MUST stay behavior-equivalent to lib/profile.py — pinned by
// tests/test_profile_cross_lang.py as the merge gate. BEACON_HOME env
// (when set) is honored inside the resolver.
const PROFILE = resolveActiveProfile({ cwd: CWD })

// Tag process by cwd so `pkill -f beacon-bus:<cwd>` targets safely without
// blasting bus.mjs instances of other projects sharing this script path.
process.title = `beacon-bus:${CWD}`

const LOG = process.env.BEACON_BUS_LOG || '/tmp/beacon-bus-channel.log'
const log = (msg) => {
  try { fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`) } catch {}
}

function loadJSON(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'))
}

function safeLoadJSON(p) {
  try { return loadJSON(p) } catch { return {} }
}

function loadToken() {
  // Read credentials via the resolver (env > profile dir > legacy fallback
  // for default profile). The profile is resolved once at startup, but the
  // token is re-read on every call so `beacon auth login` takes effect
  // without restarting bus.mjs.
  return loadTokenFromProfile(PROFILE)
}

const cloud = safeLoadJSON(CLOUD_JSON)

// e-1460: per-sid bridge claims live in .beacon/bridges/<sid>.json so
// multiple bclaude in the same cwd can each own a distinct claim.
const BRIDGES_DIR = path.join(CWD, '.beacon', 'bridges')

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (e) {
    // EPERM = process exists but belongs to another user → still alive.
    if (e && e.code === 'EPERM') return true
    return false
  }
}

// e-1460: at cold-start, look at every existing bridge claim in this cwd
// and decide whether *another* bclaude already owns one. If yes, this
// bus.mjs belongs to a 2nd+ bclaude and must NOT inherit the existing
// session_id via session.json freshness reuse — it must mint fresh.
//
// "Belongs to another bclaude" = the claim's parent_pid is alive AND
// is not my own ppid (= my bclaude pid). Legacy single-claim
// .beacon/bridge.json has no parent_pid; fall back to pid (= bus.mjs
// pid) liveness to decide whether the legacy owner is still around.
function detectOtherAliveBridges() {
  const myPpid = process.ppid
  const found = []
  try {
    if (fs.existsSync(BRIDGES_DIR)) {
      for (const f of fs.readdirSync(BRIDGES_DIR)) {
        if (!f.endsWith('.json')) continue
        let data
        try {
          data = JSON.parse(fs.readFileSync(path.join(BRIDGES_DIR, f), 'utf8'))
        } catch {
          continue
        }
        const pp = data && Number.isInteger(data.parent_pid) ? data.parent_pid : 0
        const bp = data && Number.isInteger(data.pid) ? data.pid : 0
        if (pp && pp !== myPpid && isPidAlive(pp)) {
          found.push(data)
        } else if (!pp && bp && bp !== process.pid && isPidAlive(bp)) {
          // Legacy-shaped per-sid file with no parent_pid — fall back to
          // bridge pid liveness.
          found.push(data)
        }
      }
    }
  } catch (e) {
    log(`bridges scan failed (non-fatal): ${e.message}`)
  }
  // Legacy single-claim .beacon/bridge.json — if an alive bus.mjs other
  // than us still owns it, treat it as another live bridge.
  try {
    const legacyPath = path.join(CWD, '.beacon', 'bridge.json')
    if (fs.existsSync(legacyPath)) {
      const data = JSON.parse(fs.readFileSync(legacyPath, 'utf8'))
      const bp = Number.isInteger(data?.pid) ? data.pid : 0
      if (bp && bp !== process.pid && isPidAlive(bp)) {
        // Don't double-count if same sid already collected from bridges/.
        if (!found.some(c => c.session_id === data.session_id)) {
          found.push(data)
        }
      }
    }
  } catch {}
  return found
}

const otherAliveBridges = detectOtherAliveBridges()
const FORCE_MINT = otherAliveBridges.length > 0
if (FORCE_MINT) {
  const labels = otherAliveBridges.map(c => `sid=${c.session_id} ppid=${c.parent_pid ?? '?'} pid=${c.pid ?? '?'}`).join(', ')
  log(`force-mint: ${otherAliveBridges.length} other alive bridge(s) detected [${labels}] — this bus.mjs will request a fresh session_id`)
}

// Cold-start fix: Claude Code does not pass CLAUDE_CODE_SESSION_ID to MCP
// subprocesses (verified empirically 2026-06-07). The single reliable
// refresh path is `beacon session id`, which calls
// lib/session.update_last_active() and prints the materialised id. The
// bash wrapper may walk up to find a parent .beacon/project.json, so use
// the CLI's stdout directly rather than re-reading .beacon/session.json
// from cwd (those can disagree when bus.mjs runs from a sandbox subdir).
//
// e-1460: when FORCE_MINT is on, propagate BEACON_FORCE_MINT=1 so the
// CLI's get_or_mint_session skips freshness reuse and mints a fresh sid.
function discoverSessionIdViaCLI() {
  // ms-54 e-1191: route through log() (which wraps appendFileSync in
  // try/catch) instead of bare appendFileSync. The bare path crashes the
  // whole MCP server if LOG points at an unwritable location (e.g. a
  // path that didn't exist before e-1159's OS detect, or a read-only
  // mount). log() fails soft.
  try {
    const childEnv = { ...process.env }
    if (FORCE_MINT) childEnv.BEACON_FORCE_MINT = '1'
    const sid = execSync('beacon session id', {
      cwd: CWD, stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8', timeout: 10000,
      env: childEnv,
    }).trim()
    if (sid) {
      log(`session id resolved via CLI: ${sid}${FORCE_MINT ? ' (force-minted)' : ''}`)
      return sid
    }
  } catch (e) {
    const tail = String(e?.message || e).slice(0, 200)
    log(`beacon session id failed: ${tail}`)
  }
  return ''
}
const cliSessionId = discoverSessionIdViaCLI()
const session = safeLoadJSON(SESSION_JSON)

// ms-64 / e-1459: api_url precedence chain (env > cwd cloud.json > profile.json > default)
// is owned by the resolver; PROFILE.apiUrl already encodes it.
const API_URL = PROFILE.apiUrl
const PROJECT_ID = process.env.BEACON_PROJECT_ID || cloud.project_id || ''
const SESSION_ID = process.env.BEACON_SESSION_ID || cliSessionId || session.session_id || ''
// ms-60 / e-1390: the bridge-integrated Operation scheduler fires
// `beacon trigger check` on a slower cadence (default 60s) so opted-in
// operation-trigger events surface without waiting for the user to type
// a CLI command. Also auto-include `operation-trigger` in the allowlist
// when the project has opted in via `bus_auto_execute_channels`, so the
// channel push reaches the AI via MCP notification (otherwise the inbox
// hook would still inject on the next UserPromptSubmit, but the AI
// wouldn't wake on its own — defeating the autonomy goal).
const PROJECT_JSON = path.join(CWD, '.beacon', 'project.json')
function autoExecuteChannelsFromProject() {
  try {
    const raw = safeLoadJSON(PROJECT_JSON).bus_auto_execute_channels
    if (!Array.isArray(raw)) return []
    return raw.map(s => String(s).trim()).filter(Boolean)
  } catch { return [] }
}
const _envAllow = (process.env.BEACON_CHANNEL_ALLOWLIST || 'dm')
  .split(',').map(s => s.trim()).filter(Boolean)
const ALLOWED_CHANNELS = Array.from(new Set([
  ..._envAllow,
  ...autoExecuteChannelsFromProject(),
]))
// ms-84 / e-2366 — bridge poll interval default 2s → 5s.
// 2s は Cloud Armor の IP 単位レート枠 100 req/60s (= infra/security/setup_cloud_armor.sh)
// と相まって、1 session 60 req/分 (= 2 API × 30 周/分) が同 IP に 2 session 載るだけで
// 即 429 Rate exceeded を踏む構造になっていた (= dolphin.orca の解析 / 2026-06-24)。
// 5s に上げて 1 session 24 req/分まで落とし、合わせ技で予定されている dolphin の
// heartbeat 緩和 PR (= heartbeat だけ 15s に separate、429 時 exponential backoff)
// と組み合わせれば 1 session ~16 req/分 → 同 IP に 6 session 載るマージンになる。
// 副作用は DM 着信遅延が最大 5s に伸びる (= 体感分かるがギリ許容)。
// healthy 判定窓 max(30s, 2× poll) は 30s 不変、bus directory の live 判定に regression なし。
// 即時性が必要な経路は BEACON_BUS_POLL_MS=2000 で従来挙動に opt-back 可能。
const POLL_INTERVAL = parseInt(process.env.BEACON_BUS_POLL_MS || '5000', 10)
const SCHEDULER_INTERVAL_MS = parseInt(
  process.env.BEACON_BRIDGE_SCHEDULE_INTERVAL_MS || '60000', 10)
const SCHEDULER_DISABLED = process.env.BEACON_BRIDGE_SCHEDULE_DISABLE === '1'

log(`=== beacon-bus channel starting ===`)
log(`  api=${API_URL} project=${PROJECT_ID} session=${SESSION_ID}`)
log(`  allow=[${ALLOWED_CHANNELS.join(',')}] poll=${POLL_INTERVAL}ms cwd=${CWD}`)
log(`  scheduler=${SCHEDULER_DISABLED ? 'OFF' : SCHEDULER_INTERVAL_MS + 'ms'}`)
log(`  session.json source=[${session.source || ''}] last_active=[${session.last_active || ''}]`)

// --- Layer 0-3 transparency metadata (ms-54 / e-1369) -----------------------
//
// The bridge stamps a structured snapshot of "what this session is" at
// cold-start, so the directory query can answer "which session is which?"
// with more than just session_id + actor.{machine,agent,email} +
// last_active. Four layers, all bridge-observable (Layer 4 INTENT is the
// AI's self-report and goes through a separate /intent endpoint, not via
// the bridge):
//
//   Layer 0 — Identity : agent.{kind, version}, runtime.harness.{kind, version}
//   Layer 1 — Where    : cwd, git.{branch, head_short, head_subject}
//   Layer 2 — What     : focus.{milestone, recent_task}  (from .beacon/project.json)
//   Layer 3 — Reach    : channels, budget                (from local config)
//
// Stamped ONCE on cold-start as part of the first heartbeat. Re-stamping on
// every heartbeat would be wasteful — cwd never changes mid-session, branch
// rarely does, and the cost of re-reading project.json + spawning git on
// every 2s poll is not worth the staleness window.

function safeExec(cmd, args, opts = {}) {
  try {
    return execSync([cmd, ...args].join(' '), {
      cwd: CWD,
      stdio: ['ignore', 'pipe', 'pipe'],
      encoding: 'utf8',
      timeout: 3000,
      ...opts,
    }).trim()
  } catch {
    return ''
  }
}

function detectAgentKind() {
  // Self-report. The bridge runs inside an AI harness; today that's
  // Claude Code, but the channel is designed to host Codex / Cursor /
  // Aider / Devin instances too. We surface whichever is the closest
  // signal in env. Defaults to 'claude-code' since this binary ships
  // bundled with Beacon's Claude-Code-targeted bclaude wrapper.
  if (process.env.CODEX_CLI_VERSION) return 'codex'
  if (process.env.CURSOR_TRACE_ID) return 'cursor'
  if (process.env.AIDER_VERSION) return 'aider'
  return 'claude-code'
}

function detectHarnessKind() {
  // What's wrapping the AI runtime? Terminal sessions are the most common
  // (Claude Code default). IDE extensions and the desktop app set their
  // own env signals.
  if (process.env.VSCODE_PID || process.env.VSCODE_IPC_HOOK) return 'vscode-ext'
  if (process.env.JETBRAINS_IDE) return 'jetbrains-ext'
  if (process.env.TAURI_PLATFORM) return 'desktop-app'
  if (process.env.TERM_PROGRAM === 'Apple_Terminal') return 'apple-terminal'
  if (process.env.TERM_PROGRAM) return `terminal:${process.env.TERM_PROGRAM}`
  return 'terminal'
}

function collectLayer0Identity() {
  // agent.version: read beacon CLI's _version.py via a lightweight exec.
  // Falls back to '' rather than guessing — unknown is more honest than
  // wrong.
  const agentVersion = safeExec('beacon', ['--version'])
    .replace(/^beacon\s+/, '') || ''
  return {
    agent: {
      kind: detectAgentKind(),
      version: agentVersion,
    },
    runtime: {
      harness: {
        kind: detectHarnessKind(),
        version: process.env.CLAUDE_CODE_VERSION || '',
      },
    },
  }
}

function collectLayer1Where() {
  const branch = safeExec('git', ['symbolic-ref', '--short', '-q', 'HEAD'])
  const headShort = safeExec('git', ['rev-parse', '--short', 'HEAD'])
  const headSubject = safeExec('git', ['log', '-1', '--pretty=%s'])
  return {
    cwd: CWD,
    git: {
      branch: branch || '',
      head_short: headShort || '',
      head_subject: headSubject || '',
    },
  }
}

function collectLayer2What() {
  // .beacon/project.json holds the active milestone authoritatively. We
  // read it directly rather than spawning `beacon status --json` because
  // the cost of forking a Python interpreter per cold-start adds up
  // (multiple bridges per dev machine in dispatch mode) and the JSON
  // shape we want is a tiny subset of the full status.
  try {
    const projectPath = path.join(CWD, '.beacon', 'project.json')
    const proj = safeLoadJSON(projectPath)
    const milestones = Array.isArray(proj.milestones) ? proj.milestones : []
    const active = milestones.find((m) => m.status === 'in_progress')
    if (!active) return { focus: { milestone: null, recent_task: null } }
    return {
      focus: {
        milestone: {
          id: active.id || '',
          title: active.title || '',
        },
        // recent_task is left null at cold-start; a future task may
        // wire it up by inspecting the most recent in-progress entry
        // under the active milestone. Keeping the shape stable now.
        recent_task: null,
      },
    }
  } catch {
    return { focus: { milestone: null, recent_task: null } }
  }
}

function collectLayer3Reach() {
  // channels: what the bridge subscribes to (ALLOWED_CHANNELS env →
  // ALLOWED_CHANNELS const above).
  // budget.remaining: read .beacon/bus-budget.json (the same file the
  // CLI's budget gate consults). Best-effort; missing file = no budget
  // grant = unknown (signaled as null rather than 0 so the picker can
  // distinguish "no grant" from "grant exhausted").
  let budgetRemaining = null
  let budgetTotal = null
  try {
    const budgetPath = path.join(CWD, '.beacon', 'bus-budget.json')
    const b = safeLoadJSON(budgetPath)
    if (typeof b.total === 'number' && typeof b.used === 'number') {
      budgetTotal = b.total
      budgetRemaining = Math.max(0, b.total - b.used)
    }
  } catch {
    // leave budget as null
  }
  return {
    channels: [...ALLOWED_CHANNELS],
    budget: budgetTotal !== null ? {
      total: budgetTotal,
      remaining: budgetRemaining,
    } : null,
  }
}

function collectSessionMetadata() {
  return {
    ...collectLayer0Identity(),
    ...collectLayer1Where(),
    ...collectLayer2What(),
    ...collectLayer3Reach(),
  }
}

// --- HTTPS helpers -----------------------------------------------------------

async function apiGet(p) {
  const r = await fetch(`${API_URL}${p}`, {
    headers: { Authorization: `Bearer ${loadToken()}` },
  })
  if (!r.ok) throw new Error(`GET ${p} → ${r.status}: ${(await r.text()).slice(0, 200)}`)
  return r.json()
}

async function apiPost(p, body) {
  const r = await fetch(`${API_URL}${p}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${loadToken()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    // Stamp status on the Error so callers (envelope issuance in particular)
    // can branch on 404 / 400 vs. transport-level failures. Existing call
    // sites that just want a message keep working via .message.
    const text = (await r.text()).slice(0, 200)
    const err = new Error(`POST ${p} → ${r.status}: ${text}`)
    err.status = r.status
    throw err
  }
  return r.json()
}

async function apiPut(p, body) {
  const r = await fetch(`${API_URL}${p}`, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${loadToken()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    const text = (await r.text()).slice(0, 200)
    const err = new Error(`PUT ${p} → ${r.status}: ${text}`)
    err.status = r.status
    throw err
  }
  return r.json()
}

/**
 * Mint an envelope via the server's /bus/envelope/issue endpoint, returning
 * { envelope, requested_action } to stamp on a subsequent /bus POST. On any
 * server-side failure (404 = older server without envelope support, 400 =
 * tier/payload rejection, transport error) returns null so the caller falls
 * back to the legacy no-envelope path. The server treats no-envelope events
 * as T5-equivalent today (e-1136 backward compat), so the fallback is
 * intentionally invisible to callers — they always get a delivered event,
 * just sometimes through the legacy lane.
 *
 * Tier selection: see channel/bus-envelope.mjs:selectTierForBridge. In
 * practice this MCP reply tool always carries a `text` field, which puts the
 * payload outside the T5 short-ping schema, so the chosen tier is T1 with
 * empty actions_authorized. That still earns the message a signed-T1
 * envelope, which is what unlocks `propose-to-ai` delivery on the receive
 * side (vs. the legacy T5-equivalent cap at `notify-user-only`).
 */
async function tryIssueEnvelope(projectId, payload, requestedAction) {
  const { tier, actionsAuthorized, dataClass } = selectTierForBridge(
    payload, requestedAction,
  )
  try {
    const env = await apiPost(`/api/projects/${projectId}/bus/envelope/issue`, {
      tier,
      actions_authorized: actionsAuthorized,
      data_class: dataClass,
    })
    log(`envelope minted tier=${tier} actions=${actionsAuthorized.length}`)
    return { envelope: env, requestedAction: requestedAction || null, tier }
  } catch (e) {
    const status = e?.status
    if (status === 404 || status === 400) {
      log(`envelope issuance ${status}, falling back to legacy path: ${e.message}`)
      return null
    }
    // Transport / 5xx: still fall back rather than surface a hard error to
    // the user. The bus must not break just because the envelope path
    // misbehaves — that's the deployment-safety contract from the e-1290
    // task brief.
    log(`envelope issuance error (non-404/400), falling back: ${e.message}`)
    return null
  }
}

// --- MCP server --------------------------------------------------------------

const mcp = new Server(
  { name: 'beacon-bus', version: '0.0.1' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions: [
      `You are connected to the Beacon bus.`,
      `Your session_id is ${SESSION_ID || '(not set)'} in project ${PROJECT_ID || '(not set)'}.`,
      `Incoming events arrive as <channel source="beacon-bus" event_id="..." channel="..." from_session="..." from_project="..." created_at="...">payload</channel>.`,
      `Only DMs addressed to this session in channels [${ALLOWED_CHANNELS.join(',')}] reach you.`,
      `To reply: call the reply tool with recipient_project_id=from_project, recipient_session_id=from_session, channel=channel, in_reply_to=event_id, and your text. Cross-project replies are supported — the tool posts directly to the target project's bus.`,
    ].join('\n'),
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description: 'Post a message to the Beacon bus, optionally as a reply to an inbound event.',
      inputSchema: {
        type: 'object',
        properties: {
          recipient_project_id: { type: 'string', description: 'Target Beacon project_id (use from_project of the inbound event for replies)' },
          recipient_session_id: { type: 'string', description: 'Target session_id (use from_session of the inbound event for replies)' },
          channel: { type: 'string', description: 'Bus channel (e.g. "dm")' },
          text: { type: 'string', description: 'Message body shown to the recipient' },
          in_reply_to: { type: 'string', description: 'event_id of the inbound event this is a reply to (engages budget gate)' },
        },
        required: ['recipient_project_id', 'recipient_session_id', 'channel', 'text'],
      },
    },
  ],
}))

// --- Budget gate (e-1193) ---------------------------------------------------
//
// The CLI's `beacon bus send --in-reply-to <event_id>` is gated by
// .beacon/bus-budget.json (lib/commands.py / e-1000), but the MCP reply tool
// was bypassing the same gate. Every reply through this tool gets an
// in_reply_to (the inbound event_id), so without a gate here the budget
// becomes advisory — autonomous loops would happily run forever.
//
// To stay a single source of truth we read/write the same budget file the
// CLI uses. The actual decrement logic lives in bus-budget.mjs so it can be
// unit-tested without standing up the full MCP server. See that module's
// docstring for the semantic contract (fail-closed on corruption, default
// OFF when no file, pessimistic decrement, etc.).

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'reply') {
    throw new Error(`unknown tool: ${req.params.name}`)
  }
  const args = req.params.arguments || {}
  const { recipient_project_id, recipient_session_id, channel, text, in_reply_to } = args
  const payload = { recipient_session_id, text, source_project: PROJECT_ID }
  if (in_reply_to) payload.in_reply_to = in_reply_to

  // Budget gate fires only when this is a reply (in_reply_to set). A bare
  // post (no in_reply_to) is a manual send — the human composing the tool
  // call IS the approval, same as the CLI's manual-send path. Without this
  // distinction the CLI and MCP semantics diverge again.
  if (in_reply_to) {
    const gate = consumeBusBudgetOne(CWD)
    if (!gate.allowed) {
      const msg = refuseMessage(gate.reason, gate.budget)
      log(`reply REFUSED by budget gate: reason=${gate.reason}`)
      return { content: [{ type: 'text', text: msg }], isError: true }
    }
    log(`reply budget consumed: ${gate.budget.used}/${gate.budget.total}`)
  }

  // Envelope-by-default (e-1290). Mint a server-signed envelope before
  // posting; on any failure fall through to the legacy no-envelope POST so
  // the bus never breaks for the user. tryIssueEnvelope() does the tier
  // selection (T5 for short-ping payloads, T1 otherwise) and swallows the
  // 404/400/5xx failure modes for us.
  const envInfo = await tryIssueEnvelope(recipient_project_id, payload, null)

  try {
    const body = {
      channel,
      sender_session_id: SESSION_ID,
      payload,
      delivery: 'propose-to-ai',
    }
    if (envInfo) {
      body.envelope = envInfo.envelope
      if (envInfo.requestedAction) {
        body.requested_action = envInfo.requestedAction
      }
    }
    const result = await apiPost(`/api/projects/${recipient_project_id}/bus`, body)
    const tag = envInfo ? `tier=${envInfo.tier}` : 'tier=legacy'
    log(`reply sent: event_id=${result.event_id} ${tag} → ${recipient_project_id}/${recipient_session_id}`)
    return { content: [{ type: 'text', text: `sent ${result.event_id}` }] }
  } catch (e) {
    log(`reply ERROR: ${e.message}`)
    return { content: [{ type: 'text', text: `error: ${e.message}` }], isError: true }
  }
})

await mcp.connect(new StdioServerTransport())
log('mcp connected via stdio')

// --- Polling loop ------------------------------------------------------------

if (!PROJECT_ID || !SESSION_ID) {
  // ms-54 e-1330: friendly explanation when the project is local-mode
  // (no .beacon/cloud.json) and therefore cannot participate in the
  // cloud bus. This is structurally correct fail-safe (no project_id ⇒
  // no destination to write events to), but the cryptic message used
  // to confuse users who didn't know the bus needed cloud-mode.
  log(`[FATAL] Bus channel cannot start in this directory.`)
  if (!PROJECT_ID) {
    log(`[FATAL]   reason: project_id is not set — this is a local-mode Beacon project (no .beacon/cloud.json).`)
    log(`[FATAL]   The bus (DM / event push) requires cloud sync. To enable:`)
    log(`[FATAL]     1. beacon auth login            # authenticate against beacon-ai.dev`)
    log(`[FATAL]     2. beacon cloud setup           # create / link a cloud project_id`)
    log(`[FATAL]   Local-mode projects remain fully usable for local CLI / hooks; only the bus needs cloud.`)
  }
  if (!SESSION_ID) {
    log(`[FATAL]   reason: session_id could not be resolved (.beacon/session.json missing or invalid).`)
    log(`[FATAL]   Try: beacon session id   # this mints a session_id and writes session.json`)
  }
  log(`[FATAL] Aborting poll loop. Other beacon commands still work; only the bus channel is disabled.`)
} else {
  let stopping = false
  process.on('SIGINT', () => { stopping = true; log('SIGINT received') })
  process.on('SIGTERM', () => { stopping = true; log('SIGTERM received') })

  // ms-60 follow-up: per-bridge in-memory high-water mark. The bridge owns
  // the MCP-notification delivery channel; the inbox-hook owns the
  // AUTONOMOUS-ACTION-block delivery channel. They were sharing the server
  // cursor — bus.mjs advanced it after every push and the hook then saw an
  // empty unread set, so AUTONOMOUS blocks never reached the AI. Fix:
  // bus.mjs no longer touches the server cursor. It tracks its own watermark
  // here and passes it as ``?since=`` so the next poll skips events it
  // already handled. The server cursor is now the inbox-hook's single
  // source of truth.
  //
  // On restart the bridge catches up via ensureCursorPrimed (which reads the
  // server cursor once) so we don't replay history the hook has already
  // surfaced.
  let bridgeLastSeen = ''

  // First-run cursor catchup: if this session has never read the bus before
  // (no cursor or cursor at epoch zero), advance to "now" without dispatching
  // any events. Prevents flooding the AI with historical DMs that predate this
  // session.
  async function ensureCursorPrimed() {
    try {
      const cur = await apiGet(
        `/api/projects/${PROJECT_ID}/bus/cursors/${encodeURIComponent(SESSION_ID)}`,
      )
      const last = cur && typeof cur === 'object' ? cur.last_seen_at || '' : ''
      const isUnset = !last || last.startsWith('1970-') || last === '0'
      if (isUnset) {
        const now = new Date().toISOString()
        await apiPost(
          `/api/projects/${PROJECT_ID}/bus/cursors/${encodeURIComponent(SESSION_ID)}`,
          { last_seen_at: now },
        )
        bridgeLastSeen = now
        log(`first-run: cursor primed to ${now} (no historical dispatch)`)
      } else {
        bridgeLastSeen = last
        log(`cursor already primed (last_seen_at=${last})`)
      }
    } catch (e) {
      // Cursor endpoint may 404 when no cursor exists yet; treat as first-run.
      const msg = String(e?.message || e)
      if (/\b404\b/.test(msg)) {
        const now = new Date().toISOString()
        try {
          await apiPost(
            `/api/projects/${PROJECT_ID}/bus/cursors/${encodeURIComponent(SESSION_ID)}`,
            { last_seen_at: now },
          )
          bridgeLastSeen = now
          log(`first-run: cursor created at ${now} (no historical dispatch)`)
        } catch (e2) {
          log(`first-run cursor create failed: ${e2.message}`)
        }
      } else {
        log(`cursor prime check failed: ${msg}`)
      }
    }
  }

  // Option C (e-1318): poll-gated heartbeat. The previous heartbeat path
  // (PostToolUse hook → `beacon session id` → upsert last_active) only
  // proved the *heartbeat code path* was running, not that this bridge's
  // poll loop was actually pumping events into the AI inbox. Today's
  // dogfood case caught a bridge with healthy heartbeat but a dead poll
  // loop: DMs accumulated server-side but never reached the AI. By
  // stamping `last_poll_at` *inside* pollOnce — after each poll completes,
  // success or no-op — we make stale `last_poll_at` definitively imply
  // "bridge cannot receive". Consumers (bus directory --healthy) read
  // this as the canonical liveness signal.
  //
  // The legacy `beacon session id` heartbeat keeps working — it is now
  // the *secondary* signal: useful when the bridge isn't running at all
  // (e.g. plain Claude Code without the channels feature) but no longer
  // load-bearing for "can this session receive a DM?".
  //
  // e-1424 (ms-54, PE Bug 5 reproduced in Beacon main session): the
  // contract test test_session_heartbeat_responsibility.py pins
  // "CLI is pure getter, bridge owns last_active". But before this fix
  // the bridge only wrote last_active to the CLOUD sessions/{sid} doc —
  // local .beacon/session.json kept whatever value lib/session.py wrote
  // at first mint. After _DEFAULT_FRESHNESS_SECONDS (3600s) the next CLI
  // call would see a stale marker and mint a fresh id, while bus.mjs and
  // .beacon/bridge.json kept using the original. Two session_ids,
  // silent. Cause: bridge owned the contract but didn't write the file
  // it owned. Fix: bridge writes last_active to local session.json too,
  // debounced so we don't thrash the disk every 2s poll. Details + unit
  // tests live in channel/bus-local-heartbeat.mjs.
  const updateLocalSessionLastActive = createLocalSessionHeartbeat({
    sessionJsonPath: SESSION_JSON,
    log,
  })

  async function writePollHeartbeat({ shutdown = false } = {}) {
    const nowIso = new Date().toISOString()
    try {
      const body = buildHeartbeatBody({
        nowIso,
        pollIntervalMs: POLL_INTERVAL,
        shutdown,
      })
      await apiPut(
        `/api/projects/${PROJECT_ID}/sessions/${encodeURIComponent(SESSION_ID)}`,
        body,
      )
    } catch (e) {
      // Heartbeat write failures must NEVER kill the poll loop — the
      // bridge has to keep trying to deliver events even if the cloud
      // is temporarily unreachable. Log and move on.
      log(`heartbeat write failed (non-fatal): ${e.message}`)
    }
    // e-1424: keep local session.json fresh so CLI's freshness check
    // (lib/session.py _is_fresh) reuses this id instead of re-minting.
    // Done after the cloud write — local is the secondary truth source
    // here, just enough to keep CLI happy.
    updateLocalSessionLastActive(nowIso, { force: shutdown })
  }

  // ms-54 / e-1348: per-event read receipt. Two stages:
  //   delivered = this bridge's poll fetched the event (set for every event
  //               returned by /unread, regardless of whether it survives the
  //               filter chain below). The sender uses this to distinguish
  //               "bridge is alive, message reached it" from "still in queue".
  //   opened    = this bridge dispatched the event into the MCP client (i.e.
  //               the AI/harness has seen it). Only set after a successful
  //               mcp.notification call.
  //
  // Receipt POSTs are fire-and-forget: failures are logged but never kill
  // the poll loop. The cursor advance below remains the source-of-truth
  // for "won't re-deliver" semantics — receipts are an observational
  // signal that sits on top, not a delivery guarantee.
  async function ackReceipt(eventId, stage) {
    if (!eventId) return
    try {
      await apiPost(
        `/api/projects/${PROJECT_ID}/bus/${encodeURIComponent(eventId)}/ack`,
        { stage, recipient_session_id: SESSION_ID },
      )
    } catch (e) {
      // 404 = event GC'd between poll and ack (rare, harmless). Other
      // errors (transport, 5xx) likewise mustn't break the loop — the
      // receipt is observational.
      log(`receipt ack ${stage} failed (non-fatal): id=${eventId} ${e.message}`)
    }
  }

  async function pollOnce() {
    // Pass in-memory bridgeLastSeen as ?since so the bridge does not depend on
    // (and does not perturb) the server cursor. Empty string ⇒ server falls
    // back to its cursor, which is the legacy path used by inbox-hook.
    const url = `/api/projects/${PROJECT_ID}/bus/unread`
      + `?recipient_id=${encodeURIComponent(SESSION_ID)}`
      + `&since=${encodeURIComponent(bridgeLastSeen)}`
    const events = await apiGet(url)
    if (!Array.isArray(events) || events.length === 0) return
    let latestSeen = null
    for (const evt of events) {
      // ms-60 follow-up: defense for the deploy gap. If the server is on an
      // older build that ignores the ?since query (which FastAPI does
      // silently for unknown params, so the call still succeeds but resolves
      // since from the server cursor), the bridge can re-receive the same
      // events on every poll until the inbox-hook advances the cursor.
      // Skip anything older than the in-memory watermark to keep MCP pushes
      // exactly-once during this process's lifetime.
      if (bridgeLastSeen && (evt.created_at || '') <= bridgeLastSeen) {
        continue
      }
      // e-1348: stamp `delivered` BEFORE the filter chain. The bridge
      // physically received the event; the sender deserves to know that
      // even if the event ends up filtered out (channel not in allowlist,
      // self-sent, mis-addressed). Without this we'd silently drop
      // receipts on filtered events and the sender's "where did it go?"
      // question would have a gap.
      await ackReceipt(evt.event_id, 'delivered')
      const ch = evt.channel || ''
      const sender = String(evt.sender_session_id || '')
      const payload = evt.payload || {}
      const intendedRecipient = String(payload.recipient_session_id || '')

      // DM routing context (e-1209). Pre-computed so the if/else chain stays
      // a single declarative ladder — sticking a `const` between two else
      // branches would break the chain.
      const isDmChannel = ch === 'dm'

      // Filter 1: never push events we sent ourselves (self-loop guard).
      if (sender === SESSION_ID) {
        log(`drop (self-sent): id=${evt.event_id}`)
      }
      // Filter 2a: DM addressed to someone else (or non-DM with explicit
      // recipient that isn't us). The server's /bus/unread filter already
      // drops mis-addressed events before we see them, but we double-check
      // here because:
      //   * an older Beacon server that hasn't been deployed yet still
      //     fans out every dm event to all subscribers
      //   * a third-party sender posting raw JSON to /bus could omit the
      //     stamp; only the receiver knows its own SESSION_ID
      else if (intendedRecipient && intendedRecipient !== SESSION_ID) {
        log(`drop (not addressed to us): id=${evt.event_id} recipient=${intendedRecipient}`)
      }
      // Filter 2b: DM channel without recipient stamp. e-1209 made dm a
      // 1:1-unicast-only channel; an unaddressed dm event is treated as
      // malformed-drop here instead of the legacy broadcast pass-through.
      //
      // Rules (must stay in lockstep with server/app.py:_bus_event_addressed_to):
      //   * dm channel + recipient empty       → drop (was: pass as broadcast)
      //   * dm channel + recipient != self     → drop (Filter 2a)
      //   * dm channel + recipient === self    → pass
      //   * non-dm + recipient empty           → pass (broadcast)
      //   * non-dm + recipient !== self        → drop (Filter 2a)
      //   * non-dm + recipient === self        → pass
      else if (isDmChannel && !intendedRecipient) {
        log(`drop (dm without recipient_session_id, e-1209): id=${evt.event_id} from=${sender}`)
      }
      // Filter 3: channel allowlist.
      else if (!ALLOWED_CHANNELS.includes(ch)) {
        log(`drop (channel not in allowlist): id=${evt.event_id} ch=${ch}`)
      } else {
        // e-1403: slim ping by default. The MCP <channel> notification
        // exists to wake the AI ("a thing arrived") — the full body +
        // handling guide is delivered separately by the UserPromptSubmit
        // inbox-hook (bin/beacon-bus-inbox-hook.py) on the next prompt.
        // Two routes carrying the same full payload was observed during
        // 2026-06-10 Plan A dogfood as a redundant "えっ" UX moment
        // ("why am I reading the same DM twice in two different
        // formats?"). Splitting the roles — channel = ping, hook =
        // detail — preserves the redundancy of the dual-route design
        // (= one route can fail without losing the message) without
        // making the AI re-read the same content in two shapes.
        //
        // BEACON_BUS_CHANNEL_FULLBODY=1 restores legacy behaviour for
        // callers that don't run the inbox-hook (= the channel must
        // carry the whole payload itself) or for debugging.
        const fullBody = typeof payload.text === 'string' && payload.text.length > 0
          ? payload.text
          : JSON.stringify(payload)
        const senderRaw = String(sender || '')
        const senderShort = senderRaw.length > 24
          ? senderRaw.slice(0, 24) + '…'
          : (senderRaw || '(empty)')
        const previewText = typeof payload.text === 'string' && payload.text.length > 0
          ? (payload.text.length > 80 ? payload.text.slice(0, 80) + '…' : payload.text)
          : Object.entries(payload || {})
              .filter(([k]) => !['recipient_session_id', 'source_project'].includes(k))
              .slice(0, 3)
              .map(([k, v]) => `${k}=${String(v).slice(0, 30)}`)
              .join(' ')
        const slimPing = `[${ch}] from ${senderShort}: ${previewText}\n(full body は inbox-hook の additionalContext を参照、event_id=${evt.event_id})`
        // e-1417 (ms-60): operation-trigger + auto-execute event を MCP push
        // 経路でも自律起動指示として届ける検証ルート。現状 (Phase 1) は
        // AUTONOMOUS ACTION の明示 imperative が UserPromptSubmit hook 経由
        // (bin/beacon-bus-inbox-hook.py _format_autonomous_action_block)
        // でしか付与されず、「session 起動 + 最低 1 prompt」が事実上の前提
        // として残っていた。クライアント側 (bus.mjs) からは DM event と
        // operation-trigger event を区別なく push できるはずなので、
        // operation-trigger + auto-execute の場合は MCP notification 自体に
        // AUTONOMOUS ACTION imperative を含めて AI ハーネスが prompt なし
        // でも起動できるかを実機で確認する。
        //
        // 安全側の opt-out: BEACON_BRIDGE_MCP_AUTONOMOUS_DISABLE=1 で旧挙動
        // (= slim ping のみ) に戻す。実装由来の副作用 (= 想定外の起動 / AI
        // 出力崩れ) が観察された場合の即時 rollback 経路。
        const useAutonomousImperative = shouldEmitAutonomousImperative({
          channel: ch,
          delivery: evt.delivery,
          autonomousImperativeDisabled:
            process.env.BEACON_BRIDGE_MCP_AUTONOMOUS_DISABLE === '1',
        })
        const content = useAutonomousImperative
          ? buildAutonomousActionContent(evt)
          : (process.env.BEACON_BUS_CHANNEL_FULLBODY === '1' ? fullBody : slimPing)
        await mcp.notification({
          method: 'notifications/claude/channel',
          params: {
            content,
            meta: {
              event_id: String(evt.event_id || ''),
              channel: ch,
              from_session: sender,
              from_project: String(payload.source_project || PROJECT_ID),
              created_at: String(evt.created_at || ''),
            },
          },
        })
        log(`pushed event_id=${evt.event_id} ch=${ch} from=${sender}`)
        // e-1348: `opened` is stamped only AFTER mcp.notification resolves
        // — i.e. the harness accepted the channel push. A dropped/filtered
        // event will have delivered_at but no opened_at, which is the
        // signal the sender uses to localize where their DM stalled.
        await ackReceipt(evt.event_id, 'opened')
      }
      latestSeen = evt.created_at || latestSeen
    }
    if (latestSeen) {
      // ms-60 follow-up: in-memory only. The server cursor is the
      // inbox-hook's; the bridge advancing it caused AUTONOMOUS ACTION
      // blocks to never reach the AI. See bridgeLastSeen comment at top.
      bridgeLastSeen = latestSeen
      log(`bridge watermark advanced to ${latestSeen} (in-memory)`)
    }
  }

  // ms-54 / e-1369: cold-start session metadata stamp. Layer 0-3 (Identity /
  // Where / What / Reach) are collected once and POSTed before the poll
  // loop starts. Subsequent heartbeats keep the minimal body (last_active /
  // last_poll_at / etc) so we don't repeat the cost of git spawns and
  // project.json reads on every 2s tick. cwd never changes mid-session and
  // branch / focus changes are rare enough that "restart bridge to refresh"
  // is acceptable for v0.
  //
  // Also stamps actor.{machine, agent} that was previously missing on the
  // heartbeat-only path — the directory was showing `mach=-` for any
  // session whose first write came from the bridge rather than a CLI mint.
  // ms-54 / e-1331 quick fix: write a local `.beacon/bridge.json` claim
  // so CLI commands (beacon session focus / attention) can target the SAME
  // session_id the bridge uses, instead of the CLI minting its own from
  // CLAUDE_CODE_SESSION_ID and writing it back into .beacon/session.json
  // (which would clobber what the bridge already wrote).
  //
  // The split exists because CLAUDE_CODE_SESSION_ID is NOT propagated to MCP
  // subprocess env (verified 2026-06-07). The bridge resolves session_id via
  // `beacon session id` (which mints when no env is visible), but the user's
  // terminal CLI sees the env var and would otherwise prefer it. Writing the
  // bridge's authoritative view to bridge.json lets the CLI see it and skip
  // the env-override path.
  //
  // bridge claim schema (e-1460):
  //   { session_id: str, pid: number, parent_pid: number, cwd: str,
  //     started_at: ISO8601 }
  //
  // pid          = this bus.mjs process (used by readers to drop stale
  //                claims after a crash).
  // parent_pid   = the bclaude process that spawned this bus.mjs. This is
  //                the load-bearing field for per-bclaude isolation: a CLI
  //                running inside a given bclaude walks up its parent
  //                chain and matches the bclaude pid against parent_pid
  //                to find its own bridge.
  //
  // Files live under .beacon/bridges/<session_id>.json so multiple
  // bclaude in the same cwd can each own a distinct claim. The legacy
  // single-claim .beacon/bridge.json is no longer written here; readers
  // still treat it as a fallback for back-compat. If this cold-start
  // inherited the legacy file's session_id (single-bclaude case, no
  // force-mint), clean it up so we don't leave duplicate claims behind.
  function writeBridgeClaim() {
    try {
      fs.mkdirSync(BRIDGES_DIR, { recursive: true })
      const claim = {
        session_id: SESSION_ID,
        pid: process.pid,
        parent_pid: process.ppid,
        cwd: CWD,
        started_at: new Date().toISOString(),
      }
      const claimPath = path.join(BRIDGES_DIR, `${SESSION_ID}.json`)
      fs.writeFileSync(claimPath, JSON.stringify(claim, null, 2))
      log(`bridge claim written: pid=${process.pid} ppid=${process.ppid} session=${SESSION_ID} → ${claimPath}`)
      // Best-effort legacy cleanup: if .beacon/bridge.json carries our
      // sid or a dead pid, remove it so future readers don't see two
      // owners for the same session.
      try {
        const legacyPath = path.join(CWD, '.beacon', 'bridge.json')
        if (fs.existsSync(legacyPath)) {
          const data = JSON.parse(fs.readFileSync(legacyPath, 'utf8'))
          const sameSid = data && data.session_id === SESSION_ID
          const deadOwner = data && Number.isInteger(data.pid) && !isPidAlive(data.pid)
          if (sameSid || deadOwner) {
            fs.unlinkSync(legacyPath)
            log(`legacy bridge.json cleared (sameSid=${sameSid} deadOwner=${deadOwner})`)
          }
        }
      } catch {}
    } catch (e) {
      // Best-effort. CLI degrades to mint path if claim isn't writable.
      log(`bridge claim write failed (non-fatal): ${e.message}`)
    }
  }

  function clearBridgeClaim() {
    try {
      const claimPath = path.join(BRIDGES_DIR, `${SESSION_ID}.json`)
      if (fs.existsSync(claimPath)) {
        fs.unlinkSync(claimPath)
        log(`bridge claim cleared at shutdown`)
      }
    } catch (e) {
      log(`bridge claim clear failed (non-fatal): ${e.message}`)
    }
  }

  async function stampColdStartMetadata() {
    writeBridgeClaim()
    try {
      const metadata = collectSessionMetadata()
      // Compose actor from os.hostname() so directory queries get a real
      // machine label even when no CLI ever upserted actor for this
      // session. Email is filled in server-side from the bearer token,
      // so we leave it off here on purpose.
      const actor = {
        machine: os.hostname(),
        agent: os.hostname(),
      }
      const body = { actor, ...metadata }
      await apiPut(
        `/api/projects/${PROJECT_ID}/sessions/${encodeURIComponent(SESSION_ID)}`,
        body,
      )
      const m = metadata.focus?.milestone
      log(`cold-start metadata stamped: agent=${metadata.agent.kind}@${metadata.agent.version || '?'} cwd=${metadata.cwd} branch=${metadata.git.branch || '?'} focus=${m ? `${m.id}/${(m.title || '').slice(0, 30)}` : 'none'}`)
    } catch (e) {
      // Cold-start metadata is observational, not load-bearing for
      // delivery. Logging the failure is enough — the poll loop must
      // still come up and start servicing the AI.
      log(`cold-start metadata stamp failed (non-fatal): ${e.message}`)
    }
  }

  // ms-60 / e-1390 Phase 1: bridge-integrated Operation scheduler.
  // Rate-limited so we don't spam the autofire (idempotent but writes
  // Firestore once per first-fire-of-the-day). Runs `beacon trigger
  // check`, which invokes `_auto_fire_operation_triggers()` server-side
  // and posts a T2-envelope-protected bus event (e-1393). The next
  // pollOnce() iteration picks the event up via /bus/unread and forwards
  // to the AI via MCP notification.
  let lastSchedulerRunAt = 0
  async function runSchedulerTick() {
    if (SCHEDULER_DISABLED) return
    const now = Date.now()
    if (now - lastSchedulerRunAt < SCHEDULER_INTERVAL_MS) return
    lastSchedulerRunAt = now
    try {
      execSync('beacon trigger check', {
        cwd: CWD,
        stdio: ['ignore', 'ignore', 'pipe'],
        encoding: 'utf8',
        timeout: 15000,
      })
      log('scheduler: trigger check completed')
    } catch (e) {
      // Non-zero exit / timeout / spawn fail — log but don't kill the loop.
      // The autofire path is idempotent so a missed tick recovers next round.
      log(`scheduler: trigger check failed (non-fatal): ${e.message}`)
    }
  }

  async function loop() {
    await ensureCursorPrimed()
    await stampColdStartMetadata()
    while (!stopping) {
      try {
        await pollOnce()
      } catch (e) {
        log(`poll error: ${e.message}`)
      }
      // ms-60 / e-1390: rate-limited Operation schedule check.
      // Sits between pollOnce and heartbeat so a failure here can't
      // prevent the heartbeat from advancing.
      try {
        await runSchedulerTick()
      } catch (e) {
        log(`scheduler error (non-fatal): ${e.message}`)
      }
      // e-1318: heartbeat is a *byproduct* of the poll loop. Whether
      // pollOnce succeeded, no-op'd, or threw (caught above), we got
      // here, so the loop is alive. Writing here means: if the loop
      // hangs or crashes, last_poll_at structurally stops advancing
      // and consumers can detect a dead bridge from cloud state alone.
      await writePollHeartbeat()
      await new Promise((r) => setTimeout(r, POLL_INTERVAL))
    }
    log('poll loop exiting')
    // Graceful shutdown signal (e-1318): post one last heartbeat with
    // shutdown=true so the directory query can immediately classify
    // this session as "deliberately stopped" vs. "crashed/zombie".
    // Without this, a clean Ctrl-C would look identical to a hang
    // until last_poll_at went stale.
    await writePollHeartbeat({ shutdown: true })
    // e-1331 quick fix: clear the local bridge claim on graceful exit so
    // a stale claim doesn't keep pointing CLI commands at a dead
    // session_id. Crashes / SIGKILL skip this — the CLI's pid-liveness
    // check in read_bridge_session() handles that case.
    clearBridgeClaim()
  }
  setTimeout(loop, 500)
}
