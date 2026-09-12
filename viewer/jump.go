// 端末へ飛ぶ (jump-to-terminal, ms-173 e-6402)。
//
// 運用室の一覧から「実物の端末」へ辿り着けるようにする。並列に走るセッションを
// 見ているとき、「これを直接見たい」となったら手で端末を探すのではなく、一覧の操作で
// その端末ウィンドウ/タブを前面化する。
//
// 辿り方: セッションのプロセス番号 (pid) → 制御端末 (tty) → その tty を持つ端末
// ウィンドウ/タブを AppleScript で前面化する。
//
// **これは「橋渡し」であって主役ではない** (SPEC 方針5)。運用室は対象・状態・
// activity で作業が完結するのが目標で、飛ぶのはどうしても実物を見たい時の最後の手段。
//
// **loopback (= 自分の機械の中) 限定** (SPEC 方針4)。外部公開時 (--expose) は無効。
// 他人のブラウザから他所の端末は前面化できず、無意味かつ危険なため。
package main

import (
	"fmt"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
)

// commandRunner はコマンド実行を差し替え可能にする継ぎ目 (テストで stub するため)。
// これが無いと ps / osascript を実際に叩くしか検証手段がなく、エラー処理の変更を
// 局所フィードバックで確かめられない。
type commandRunner func(name string, args ...string) ([]byte, error)

// defaultRunner は実プロセスを起動する既定の実行器。
func defaultRunner(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).Output()
}

// jumpSupported は「端末へ飛ぶ」が使える条件を **1 か所** で判定する (純関数)。
//
// macOS (osascript がある) かつ 自分の機械の中 (loopback) かつ 非公開 のときだけ。
// 対応 OS / 公開ポリシーを変えるときはこの関数 1 つを直せば、ボタンの表示可否
// (jumpAvailable 経由) も揃って変わる (= 判定の真実源を 1 つに)。
func jumpSupported(goos, host string, expose bool) bool {
	return goos == "darwin" && isLoopback(host) && !expose
}

// ttyFromPS は `ps -o tty= -p <pid>` の出力から制御端末名を取り出す (純関数)。
//
// ps は環境により "s000" / "ttys000" / "/dev/ttys000" と表記が揺れる。制御端末が
// 無いプロセスは "?" / "??" / 空を返す — その場合は空文字 (= 前面化できない) を返す。
func ttyFromPS(out string) string {
	t := strings.TrimSpace(out)
	if t == "" || t == "?" || t == "??" || t == "-" {
		return ""
	}
	t = strings.TrimPrefix(t, "/dev/")
	// ps は "ttys000" を "s000" と短縮することがあるので "tty" を補う。
	if !strings.HasPrefix(t, "tty") {
		t = "tty" + t
	}
	return t
}

// resolveTTY は pid の制御端末を引く (既定の実行器で ps を叩く)。
func resolveTTY(pid int) (string, error) {
	return resolveTTYWith(defaultRunner, pid)
}

// resolveTTYWith は実行器を差し替え可能にした本体 (テスト可能)。
func resolveTTYWith(run commandRunner, pid int) (string, error) {
	if pid <= 0 {
		// pid が無効 / 欠落 (0 は Go の int ゼロ値)。「別マシン」とは別の失敗。
		return "", fmt.Errorf(
			"プロセス番号が指定されていないか無効です (pid=%d)", pid)
	}
	out, err := run("ps", "-o", "tty=", "-p", strconv.Itoa(pid))
	if err != nil {
		return "", fmt.Errorf(
			"プロセス %d の端末を辿れません (すでに終了している可能性)", pid)
	}
	tty := ttyFromPS(string(out))
	if tty == "" {
		return "", fmt.Errorf(
			"プロセス %d に制御端末がありません (端末の外で動いている可能性)", pid)
	}
	return tty, nil
}

// appleTerminalJumpScript は tty を持つ Terminal.app のタブを前面化する
// AppleScript を組む (純関数、テスト可能)。見つかれば "true"、無ければ "false" を返す。
func appleTerminalJumpScript(tty string) string {
	dev := "/dev/" + tty
	return fmt.Sprintf(`tell application "Terminal"
  set matched to false
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %q then
        set matched to true
        set selected of t to true
        set frontmost of w to true
      end if
    end repeat
  end repeat
  if matched then activate
  return matched
end tell`, dev)
}

// isAppleTerminal は harness の種類が Terminal.app かどうか。
// 空 (= 種類不明) は Terminal.app とみなす (macOS の既定端末)。
func isAppleTerminal(harness string) bool {
	switch strings.ToLower(strings.TrimSpace(harness)) {
	case "", "apple-terminal", "terminal", "apple_terminal":
		return true
	default:
		return false
	}
}

// isITerm2 は harness の種類が iTerm2 かどうか。
func isITerm2(harness string) bool {
	switch strings.ToLower(strings.TrimSpace(harness)) {
	case "iterm2", "iterm", "iterm.app":
		return true
	default:
		return false
	}
}

// jumpableHarness は「端末へ飛ぶ」に対応した端末かどうか (前面化できるか)。
// 対応外 (kitty / VS Code / tmux 等) は fallback (パスのコピー) に回す。
func jumpableHarness(harness string) bool {
	return isAppleTerminal(harness) || isITerm2(harness)
}

// terminalAppName は前面化対象の端末アプリ名 (見つからない時のメッセージ用)。
func terminalAppName(harness string) string {
	if isITerm2(harness) {
		return "iTerm2"
	}
	return "Terminal.app"
}

// iTerm2JumpScript は tty を持つ iTerm2 のタブを前面化する AppleScript を組む
// (純関数)。iTerm2 は window → tab → session の 3 段で、session が tty を持つ。
func iTerm2JumpScript(tty string) string {
	dev := "/dev/" + tty
	return fmt.Sprintf(`tell application "iTerm2"
  set matched to false
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is %q then
          set matched to true
          select w
          select t
          select s
        end if
      end repeat
    end repeat
  end repeat
  if matched then activate
  return matched
end tell`, dev)
}

// jumpScriptBuilder は端末種別に応じた AppleScript 組み立て関数を返す。
// 未対応の種別は fallback として理由付きエラーを返す (SPEC 方針4: 落とさない)。
func jumpScriptBuilder(harness string) (func(tty string) string, error) {
	switch {
	case isAppleTerminal(harness):
		return appleTerminalJumpScript, nil
	case isITerm2(harness):
		return iTerm2JumpScript, nil
	default:
		return nil, fmt.Errorf(
			"この端末 (%s) への前面化は未対応です — 作業フォルダを手で開いてください", harness)
	}
}

// jumpToTerminal は pid のセッションが動いている端末ウィンドウを前面化する。
//
// harness は端末の種類。未対応の種類は **エラーで落とさず理由を返す** (SPEC 方針4)。
func jumpToTerminal(pid int, harness string) error {
	if runtime.GOOS != "darwin" {
		return fmt.Errorf(
			"端末の前面化はこの OS では未対応です (現状 macOS のみ)")
	}
	// 端末種別で前面化スクリプトを選ぶ。未対応端末は fallback として理由を返す。
	build, err := jumpScriptBuilder(harness)
	if err != nil {
		return err
	}
	tty, err := resolveTTY(pid)
	if err != nil {
		return err
	}
	out, err := exec.Command("osascript", "-e", build(tty)).Output()
	if err != nil {
		return fmt.Errorf("端末の前面化に失敗しました (osascript): %v", err)
	}
	if strings.TrimSpace(string(out)) != "true" {
		return fmt.Errorf(
			"その端末 (tty %s) を持つ %s のタブが見つかりませんでした "+
				"(別の端末アプリで開いている可能性)", tty, terminalAppName(harness))
	}
	return nil
}
