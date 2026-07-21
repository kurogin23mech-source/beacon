// e-3858 — unit tests for the cold-start "another alive bridge?" predicate.
// Run: node --test channel/bridge_detect.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { claimIsOtherAliveBridge, detectOtherAliveBridges } from './bridge_detect.mjs'

// Injected liveness: pids in this set are "alive", everything else "dead".
const aliveSet = (...pids) => {
  const s = new Set(pids)
  return (pid) => s.has(pid)
}

test('dead bridge pid is NOT another alive bridge, even if parent bclaude is alive (the e-3858 bug)', () => {
  // The exact incident shape: bridge process killed (39892 dead) but its parent
  // bclaude (31280) still alive. Old logic keyed on parent → force-minted →
  // split. New logic keys on the bridge pid → must be false.
  const claim = { session_id: 's1', pid: 39892, parent_pid: 31280 }
  const pidAlive = aliveSet(31280)  // parent alive, bridge dead
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 30152, myPid: 15972, pidAlive }),
    false,
  )
})

test('alive bridge owned by a different bclaude IS another alive bridge', () => {
  const claim = { session_id: 's2', pid: 5000, parent_pid: 4000 }
  const pidAlive = aliveSet(5000, 4000)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 9999, myPid: 8888, pidAlive }),
    true,
  )
})

test('my own bridge (same pid) is not counted', () => {
  const claim = { session_id: 's3', pid: 8888, parent_pid: 4000 }
  const pidAlive = aliveSet(8888, 4000)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 9999, myPid: 8888, pidAlive }),
    false,
  )
})

test('my own bclaude (same parent_pid) prior claim is not counted', () => {
  const claim = { session_id: 's4', pid: 5000, parent_pid: 9999 }
  const pidAlive = aliveSet(5000, 9999)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 9999, myPid: 8888, pidAlive }),
    false,
  )
})

test('claim without a usable bridge pid does not block reuse', () => {
  const claim = { session_id: 's5', parent_pid: 4000 }  // no pid
  const pidAlive = aliveSet(4000)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 9999, myPid: 8888, pidAlive }),
    false,
  )
})

test('detectOtherAliveBridges returns only claims with a live bridge process', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bd-'))
  try {
    fs.writeFileSync(path.join(dir, 'dead.json'),
      JSON.stringify({ session_id: 'dead', pid: 39892, parent_pid: 31280 }))
    fs.writeFileSync(path.join(dir, 'alive.json'),
      JSON.stringify({ session_id: 'alive', pid: 5000, parent_pid: 4000 }))
    fs.writeFileSync(path.join(dir, 'mine.json'),
      JSON.stringify({ session_id: 'mine', pid: 8888, parent_pid: 9999 }))
    const pidAlive = aliveSet(31280, 5000, 4000, 8888, 9999)  // parents+all alive EXCEPT bridge 39892
    const found = detectOtherAliveBridges({
      bridgesDir: dir, legacyPath: null,
      myPpid: 9999, myPid: 8888, pidAlive,
    })
    const sids = found.map(c => c.session_id).sort()
    assert.deepEqual(sids, ['alive'])  // dead bridge excluded, mine excluded
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('empty / missing bridges dir yields no detections', () => {
  const found = detectOtherAliveBridges({
    bridgesDir: path.join(os.tmpdir(), 'does-not-exist-bd'), legacyPath: null,
    myPpid: 1, myPid: 2, pidAlive: () => true,
  })
  assert.deepEqual(found, [])
})
