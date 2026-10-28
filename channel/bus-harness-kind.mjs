// ms-173 / e-7199 — 端末の種類 (runtime.harness.kind) の名乗り方を 1 箇所に置く。
//
// bridge は名乗り (session register) で「自分がどの端末アプリの中で動いているか」を
// 送り、運用室 (viewer/jump.go) はその値で「端末へ飛ぶ」の前面化スクリプトを選ぶ。
// 以前は名乗り側が Apple_Terminal だけを正規化し、それ以外 (iTerm.app 等) は
// `terminal:<TERM_PROGRAM>` という生の値を送っていた。受け付け表は iterm2 しか
// 知らないので、iTerm2 対応は宣言されていたのに実機では一度も成立していなかった
// (語彙を決める場所が JS と Go の 2 箇所にあり、突き合わせる試験が無かった)。
//
// ここが名乗り側の **唯一の正規化表**。値は viewer/jump.go の terminalDrivers の
// kinds に必ず含まれること — tests/test_harness_kind_parity_e7199.py が両方の
// 表を構造的に読んで突き合わせ、片方だけ変えると赤くなる。
//
// 純粋関数 (env を引数で受ける) にしてあるのは、実際の端末を起動せずに
// node --test で全分岐を試せるようにするため。

// TERM_PROGRAM の値 → 正典の端末種別。キーは端末アプリが環境変数に立てる綴り
// そのまま (大文字小文字を含む)。値は viewer 側の別名集合に在るものだけ。
export const TERM_PROGRAM_KINDS = Object.freeze({
  'Apple_Terminal': 'apple-terminal',
  'iTerm.app': 'iterm2',
})

// 正規化表に無い端末は、この接頭辞 + TERM_PROGRAM の生の値で送る (捨てない =
// 新しい端末が現れたときに何が来ているかを運用室側で読めるようにする)。
// viewer/jump.go はこの接頭辞を剥がしてから表を引く (rawTerminalPrefix と同じ値)。
export const RAW_TERMINAL_PREFIX = 'terminal:'

/**
 * 環境変数から端末の種類を決める (純粋関数)。
 *
 * 優先順位は IDE 拡張 → デスクトップアプリ → 端末アプリ。端末アプリは
 * TERM_PROGRAM_KINDS で正典名に寄せ、知らないものは生の値を接頭辞付きで返す。
 * TERM_PROGRAM が無ければ 'terminal' (種類不明の端末)。
 *
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function harnessKindFromEnv(env) {
  const e = env || {}
  if (e.VSCODE_PID || e.VSCODE_IPC_HOOK) return 'vscode-ext'
  if (e.JETBRAINS_IDE) return 'jetbrains-ext'
  if (e.TAURI_PLATFORM) return 'desktop-app'
  const term = String(e.TERM_PROGRAM || '').trim()
  if (!term) return 'terminal'
  if (Object.prototype.hasOwnProperty.call(TERM_PROGRAM_KINDS, term)) {
    return TERM_PROGRAM_KINDS[term]
  }
  return `${RAW_TERMINAL_PREFIX}${term}`
}
