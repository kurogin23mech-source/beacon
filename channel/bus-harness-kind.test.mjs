// e-7199 — 端末の種類の名乗り (harnessKindFromEnv) の分岐を全部通す。
// Run: node --test channel/bus-harness-kind.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  harnessKindFromEnv,
  TERM_PROGRAM_KINDS,
  RAW_TERMINAL_PREFIX,
} from './bus-harness-kind.mjs'

test('iTerm2 は正典名 iterm2 で名乗る (生の terminal:iTerm.app ではない — e-7199 の実害)', () => {
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: 'iTerm.app' }), 'iterm2')
})

test('Terminal.app は apple-terminal で名乗る (従来どおり)', () => {
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: 'Apple_Terminal' }), 'apple-terminal')
})

test('知らない端末は捨てずに接頭辞付きの生の値で名乗る', () => {
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: 'WezTerm' }), `${RAW_TERMINAL_PREFIX}WezTerm`)
})

test('TERM_PROGRAM が無ければ種類不明の端末', () => {
  assert.equal(harnessKindFromEnv({}), 'terminal')
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: '   ' }), 'terminal')
  assert.equal(harnessKindFromEnv(undefined), 'terminal')
})

test('IDE 拡張 / デスクトップアプリは端末より優先する', () => {
  assert.equal(harnessKindFromEnv({ VSCODE_PID: '1', TERM_PROGRAM: 'iTerm.app' }), 'vscode-ext')
  assert.equal(harnessKindFromEnv({ VSCODE_IPC_HOOK: 'x' }), 'vscode-ext')
  assert.equal(harnessKindFromEnv({ JETBRAINS_IDE: '1', TERM_PROGRAM: 'iTerm.app' }), 'jetbrains-ext')
  assert.equal(harnessKindFromEnv({ TAURI_PLATFORM: 'macos', TERM_PROGRAM: 'iTerm.app' }), 'desktop-app')
})

test('正規化表の全キーが表の値そのもので返る (表を足したら自動で網羅)', () => {
  for (const [termProgram, kind] of Object.entries(TERM_PROGRAM_KINDS)) {
    assert.equal(harnessKindFromEnv({ TERM_PROGRAM: termProgram }), kind)
    assert.ok(!kind.startsWith(RAW_TERMINAL_PREFIX), `正典名が生の接頭辞を持っている: ${kind}`)
  }
})

test('表の照合は綴りそのまま (prototype 経由のキーには反応しない)', () => {
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: 'toString' }), `${RAW_TERMINAL_PREFIX}toString`)
  assert.equal(harnessKindFromEnv({ TERM_PROGRAM: 'iterm.app' }), `${RAW_TERMINAL_PREFIX}iterm.app`)
})
