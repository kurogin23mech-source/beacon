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
//   BEACON_API_URL          ← .beacon/cloud.json.api_url   ← https://beacon-ai.dev
//   BEACON_PROJECT_ID       ← .beacon/cloud.json.project_id
//   BEACON_SESSION_ID       ← .beacon/session.json.session_id
//   BEACON_AUTH_TOKEN       ← ~/.beacon/credentials.json.token
//   BEACON_CHANNEL_ALLOWLIST (csv, default "dm")
//   BEACON_BUS_POLL_MS      (default 2000)
//   BEACON_BUS_LOG          (default /tmp/beacon-bus-channel.log)

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { consumeBusBudgetOne, refuseMessage } from './bus-budget.mjs'

// --- Config discovery --------------------------------------------------------

const BEACON_HOME = process.env.BEACON_HOME || path.join(os.homedir(), '.beacon')
const CREDS_JSON = path.join(BEACON_HOME, 'credentials.json')
const CWD = process.cwd()
const CLOUD_JSON = path.join(CWD, '.beacon', 'cloud.json')
const SESSION_JSON = path.join(CWD, '.beacon', 'session.json')

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
  if (process.env.BEACON_AUTH_TOKEN) return process.env.BEACON_AUTH_TOKEN
  const c = safeLoadJSON(CREDS_JSON)
  return c.token || c.id_token || ''
}

const cloud = safeLoadJSON(CLOUD_JSON)

// Cold-start fix: Claude Code does not pass CLAUDE_CODE_SESSION_ID to MCP
// subprocesses (verified empirically 2026-06-07). The single reliable
// refresh path is `beacon session id`, which calls
// lib/session.update_last_active() and prints the materialised id. The
// bash wrapper may walk up to find a parent .beacon/project.json, so use
// the CLI's stdout directly rather than re-reading .beacon/session.json
// from cwd (those can disagree when bus.mjs runs from a sandbox subdir).
function discoverSessionIdViaCLI() {
  // ms-54 e-1191: route through log() (which wraps appendFileSync in
  // try/catch) instead of bare appendFileSync. The bare path crashes the
  // whole MCP server if LOG points at an unwritable location (e.g. a
  // path that didn't exist before e-1159's OS detect, or a read-only
  // mount). log() fails soft.
  try {
    const sid = execSync('beacon session id', {
      cwd: CWD, stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8', timeout: 10000,
    }).trim()
    if (sid) {
      log(`session id resolved via CLI: ${sid}`)
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

const API_URL = (process.env.BEACON_API_URL || cloud.api_url || 'https://beacon-ai.dev').replace(/\/$/, '')
const PROJECT_ID = process.env.BEACON_PROJECT_ID || cloud.project_id || ''
const SESSION_ID = process.env.BEACON_SESSION_ID || cliSessionId || session.session_id || ''
const ALLOWED_CHANNELS = (process.env.BEACON_CHANNEL_ALLOWLIST || 'dm')
  .split(',').map(s => s.trim()).filter(Boolean)
const POLL_INTERVAL = parseInt(process.env.BEACON_BUS_POLL_MS || '2000', 10)

log(`=== beacon-bus channel starting ===`)
log(`  api=${API_URL} project=${PROJECT_ID} session=${SESSION_ID}`)
log(`  allow=[${ALLOWED_CHANNELS.join(',')}] poll=${POLL_INTERVAL}ms cwd=${CWD}`)
log(`  session.json source=[${session.source || ''}] last_active=[${session.last_active || ''}]`)

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
  if (!r.ok) throw new Error(`POST ${p} → ${r.status}: ${(await r.text()).slice(0, 200)}`)
  return r.json()
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

  try {
    const result = await apiPost(`/api/projects/${recipient_project_id}/bus`, {
      channel,
      sender_session_id: SESSION_ID,
      payload,
      delivery: 'propose-to-ai',
    })
    log(`reply sent: event_id=${result.event_id} → ${recipient_project_id}/${recipient_session_id}`)
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
  log(`FATAL: PROJECT_ID and SESSION_ID must be discoverable. Aborting poll loop.`)
} else {
  let stopping = false
  process.on('SIGINT', () => { stopping = true; log('SIGINT received') })
  process.on('SIGTERM', () => { stopping = true; log('SIGTERM received') })

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
        log(`first-run: cursor primed to ${now} (no historical dispatch)`)
      } else {
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
          log(`first-run: cursor created at ${now} (no historical dispatch)`)
        } catch (e2) {
          log(`first-run cursor create failed: ${e2.message}`)
        }
      } else {
        log(`cursor prime check failed: ${msg}`)
      }
    }
  }

  async function pollOnce() {
    const events = await apiGet(
      `/api/projects/${PROJECT_ID}/bus/unread?recipient_id=${encodeURIComponent(SESSION_ID)}`,
    )
    if (!Array.isArray(events) || events.length === 0) return
    let latestSeen = null
    for (const evt of events) {
      const ch = evt.channel || ''
      const sender = String(evt.sender_session_id || '')
      const payload = evt.payload || {}
      const intendedRecipient = String(payload.recipient_session_id || '')

      // Filter 1: never push events we sent ourselves (self-loop guard).
      if (sender === SESSION_ID) {
        log(`drop (self-sent): id=${evt.event_id}`)
      }
      // Filter 2: skip DMs explicitly addressed to someone else.
      // recipient_session_id is the convention for DM events; events without
      // it are treated as broadcast and pass through (still subject to channel
      // allowlist).
      else if (intendedRecipient && intendedRecipient !== SESSION_ID) {
        log(`drop (not addressed to us): id=${evt.event_id} recipient=${intendedRecipient}`)
      }
      // Filter 3: channel allowlist.
      else if (!ALLOWED_CHANNELS.includes(ch)) {
        log(`drop (channel not in allowlist): id=${evt.event_id} ch=${ch}`)
      } else {
        const content = typeof payload.text === 'string' && payload.text.length > 0
          ? payload.text
          : JSON.stringify(payload)
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
      }
      latestSeen = evt.created_at || latestSeen
    }
    if (latestSeen) {
      try {
        await apiPost(
          `/api/projects/${PROJECT_ID}/bus/cursors/${encodeURIComponent(SESSION_ID)}`,
          { last_seen_at: latestSeen },
        )
        log(`cursor advanced to ${latestSeen}`)
      } catch (e) {
        log(`cursor advance failed: ${e.message}`)
      }
    }
  }

  async function loop() {
    await ensureCursorPrimed()
    while (!stopping) {
      try {
        await pollOnce()
      } catch (e) {
        log(`poll error: ${e.message}`)
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL))
    }
    log('poll loop exiting')
  }
  setTimeout(loop, 500)
}
