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

// resolveTTY は pid の制御端末を実際に引く (ps を叩く)。
func resolveTTY(pid int) (string, error) {
	if pid <= 0 {
		return "", fmt.Errorf(
			"プロセス番号が分かりません (このセッションは手元で拾えていない = 別マシンの可能性)")
	}
	out, err := exec.Command("ps", "-o", "tty=", "-p", strconv.Itoa(pid)).Output()
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
// iTerm2 等への拡張は e-6403 で harness.kind 分岐として足す。
func isAppleTerminal(harness string) bool {
	switch strings.ToLower(strings.TrimSpace(harness)) {
	case "", "apple-terminal", "terminal", "apple_terminal":
		return true
	default:
		return false
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
	if !isAppleTerminal(harness) {
		// e-6403 で iTerm2 等を足すまでは、未対応端末は fallback として理由を返す。
		return fmt.Errorf(
			"この端末 (%s) への前面化は未対応です — 作業フォルダを手で開いてください", harness)
	}
	tty, err := resolveTTY(pid)
	if err != nil {
		return err
	}
	out, err := exec.Command("osascript", "-e", appleTerminalJumpScript(tty)).Output()
	if err != nil {
		return fmt.Errorf("端末の前面化に失敗しました (osascript): %v", err)
	}
	if strings.TrimSpace(string(out)) != "true" {
		return fmt.Errorf(
			"その端末 (tty %s) を持つ Terminal.app のタブが見つかりませんでした "+
				"(別の端末アプリで開いている可能性)", tty)
	}
	return nil
}
