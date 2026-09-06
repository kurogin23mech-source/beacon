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

test('ms-165: an alive bridge on MY canonical sid is NOT a competitor (restart hand-off)', () => {
  // The 2026-09-05 incident shape: a bclaude restart. The dying old bridge
  // (pid 87349, different parent) is briefly still alive and claims MY canonical
  // session_id. Without the mySid exclusion this forced a divergent mint → the
  // new bridge heartbeat under a fresh sid the CLI never resolves → silent
  // receive death. With mySid set, my own session's bridge is excluded so I
  // reuse (take over) the canonical sid.
  const claim = { session_id: 'sv-mine', pid: 87349, parent_pid: 1533 }
  const pidAlive = aliveSet(87349, 1533)  // old bridge briefly still alive
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 851, myPid: 94943, mySid: 'sv-mine', pidAlive }),
    false,
  )
})

test('ms-165: an alive bridge on a DIFFERENT sid still forces a mint (concurrent bclaude)', () => {
  // A genuinely concurrent bclaude in the same cwd (dispatch mode) owns a
  // different sid — a fresh mint is still correct, mySid must not suppress it.
  const claim = { session_id: 'sv-other', pid: 5000, parent_pid: 4000 }
  const pidAlive = aliveSet(5000, 4000)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 851, myPid: 94943, mySid: 'sv-mine', pidAlive }),
    true,
  )
})

test('ms-165: empty mySid preserves pre-ms-165 behaviour (any alive other bridge counts)', () => {
  const claim = { session_id: 'sv-x', pid: 5000, parent_pid: 4000 }
  const pidAlive = aliveSet(5000, 4000)
  assert.equal(
    claimIsOtherAliveBridge(claim, { myPpid: 851, myPid: 94943, mySid: '', pidAlive }),
    true,
  )
})

test('ms-165: detectOtherAliveBridges drops my own-sid bridge, keeps a different-sid one', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bd-'))
  try {
    fs.writeFileSync(path.join(dir, 'mine-restart.json'),
      JSON.stringify({ session_id: 'sv-mine', pid: 87349, parent_pid: 1533 }))
    fs.writeFileSync(path.join(dir, 'concurrent.json'),
      JSON.stringify({ session_id: 'sv-other', pid: 5000, parent_pid: 4000 }))
    const pidAlive = aliveSet(87349, 1533, 5000, 4000)  // ALL alive
    const found = detectOtherAliveBridges({
      bridgesDir: dir, legacyPath: null,
      myPpid: 851, myPid: 94943, mySid: 'sv-mine', pidAlive,
    })
    const sids = found.map(c => c.session_id).sort()
    assert.deepEqual(sids, ['sv-other'])  // my own restart bridge excluded, concurrent kept
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
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
