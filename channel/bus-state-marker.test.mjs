// ms-159 / e-6244 — unit tests for the session-state marker read + heartbeat
// piggyback. Run: node --test channel/bus-state-marker.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { readStateMarker } from './bus-state-marker.mjs'
import { buildHeartbeatBody } from './bus-heartbeat.mjs'

function tmpFile(contents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'beacon-state-'))
  const p = path.join(dir, 'session-state.json')
  if (contents !== undefined) fs.writeFileSync(p, contents)
  return p
}

// --- readStateMarker -------------------------------------------------------

test('reads a well-formed marker', () => {
  const p = tmpFile(JSON.stringify({
    declared_state: 'awaiting_human',
    declared_at: '2026-09-07T00:05:00.000Z',
    state_since: '2026-09-07T00:00:00.000Z',
    source_event: 'Notification',
  }))
  assert.deepEqual(readStateMarker(p), {
    declaredState: 'awaiting_human',
    declaredAt: '2026-09-07T00:05:00.000Z',
    stateSince: '2026-09-07T00:00:00.000Z',
  })
})

test('marker without state_since falls back to declared_at (e-6244 back-compat)', () => {
  const p = tmpFile(JSON.stringify({
    declared_state: 'idle',
    declared_at: '2026-09-07T00:00:00.000Z',
  }))
  assert.deepEqual(readStateMarker(p), {
    declaredState: 'idle',
    declaredAt: '2026-09-07T00:00:00.000Z',
    stateSince: '2026-09-07T00:00:00.000Z',
  })
})

test('absent marker → null (back-compat common path)', () => {
  assert.equal(readStateMarker('/no/such/path/session-state.json'), null)
})

test('malformed JSON → null (never throws into the poll loop)', () => {
  const p = tmpFile('{not valid json')
  assert.equal(readStateMarker(p), null)
})

test('marker missing declared_at → null (both fields required together)', () => {
  const p = tmpFile(JSON.stringify({ declared_state: 'running' }))
  assert.equal(readStateMarker(p), null)
})

test('marker missing declared_state → null', () => {
  const p = tmpFile(JSON.stringify({ declared_at: '2026-09-07T00:00:00Z' }))
  assert.equal(readStateMarker(p), null)
})

// --- buildHeartbeatBody piggyback ------------------------------------------

test('heartbeat carries declared_state/declared_at/state_since when present', () => {
  const body = buildHeartbeatBody({
    nowIso: '2026-09-07T00:05:00Z', pollIntervalMs: 5000,
    declaredState: 'awaiting_human', declaredAt: '2026-09-07T00:05:00Z',
    stateSince: '2026-09-07T00:00:00Z',
  })
  assert.equal(body.declared_state, 'awaiting_human')
  assert.equal(body.declared_at, '2026-09-07T00:05:00Z')
  assert.equal(body.state_since, '2026-09-07T00:00:00Z')
})

test('heartbeat state_since falls back to declared_at when not provided', () => {
  const body = buildHeartbeatBody({
    nowIso: '2026-09-07T00:00:00Z', pollIntervalMs: 5000,
    declaredState: 'idle', declaredAt: '2026-09-07T00:00:00Z',
  })
  assert.equal(body.state_since, '2026-09-07T00:00:00Z')
})

test('heartbeat omits declared_* when no declaration (negative regression)', () => {
  const body = buildHeartbeatBody({ nowIso: '2026-09-07T00:00:00Z', pollIntervalMs: 5000 })
  assert.ok(!('declared_state' in body))
  assert.ok(!('declared_at' in body))
  // The healthy common path keeps its original shape.
  assert.deepEqual(Object.keys(body).sort(), ['last_active', 'last_poll_at', 'poll_interval_ms', 'shutdown'])
})

test('heartbeat omits declared_* when only one field is provided', () => {
  const body = buildHeartbeatBody({
    nowIso: '2026-09-07T00:00:00Z', pollIntervalMs: 5000, declaredState: 'running',
  })
  assert.ok(!('declared_state' in body))
})
