package main

import (
	"strings"
	"testing"
)

// ps の表記揺れ (短縮 / フルパス / 制御端末なし) を吸収して tty 名に揃えること。
// ここがズレると、正しいプロセスでも端末を辿れず「飛べない」になる。
func TestTTYFromPS(t *testing.T) {
	cases := []struct{ in, want, why string }{
		{"ttys000\n", "ttys000", "そのままの表記"},
		{"s001", "ttys001", "ps が tty を省いた短縮形"},
		{"/dev/ttys002\n", "ttys002", "フルパス表記"},
		{"  ttys003  ", "ttys003", "前後の空白"},
		{"?", "", "制御端末なし (? 表記)"},
		{"??", "", "制御端末なし (?? 表記)"},
		{"-", "", "制御端末なし (- 表記)"},
		{"", "", "空"},
	}
	for _, c := range cases {
		if got := ttyFromPS(c.in); got != c.want {
			t.Errorf("ttyFromPS(%q) = %q, want %q (%s)", c.in, got, c.want, c.why)
		}
	}
}

// 前面化の AppleScript が、対象 tty をデバイスパスで含み、Terminal を狙うこと。
// 見つかったかを true/false で返す形になっていること (見つからない時に落とさないため)。
func TestAppleTerminalJumpScript(t *testing.T) {
	script := appleTerminalJumpScript("ttys004")
	if !strings.Contains(script, "/dev/ttys004") {
		t.Errorf("対象 tty のデバイスパスが含まれていない: %q", script)
	}
	if !strings.Contains(script, `application "Terminal"`) {
		t.Errorf("Terminal.app を狙っていない: %q", script)
	}
	if !strings.Contains(script, "return matched") {
		t.Errorf("見つかったかを返していない (見つからない時に落とさないため): %q", script)
	}
}

// 端末の種類の判定: 空 (不明) と Terminal 系は Terminal.app、他は未対応 (fallback)。
// iTerm2 等を Terminal.app として誤って前面化しないための境界 (e-6403 で拡張)。
func TestIsAppleTerminal(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"", true},               // 不明は macOS 既定端末とみなす
		{"apple-terminal", true}, // サーバの harness.kind 表記
		{"Terminal", true},
		{"Apple_Terminal", true},
		{"iterm2", false}, // e-6403 で対応するまでは未対応
		{"kitty", false},
		{"vscode", false},
	}
	for _, c := range cases {
		if got := isAppleTerminal(c.in); got != c.want {
			t.Errorf("isAppleTerminal(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

// pid が無い (= 別マシンのセッション等) は、落とさず理由を返すこと。
func TestResolveTTYRejectsZeroPID(t *testing.T) {
	if _, err := resolveTTY(0); err == nil {
		t.Error("pid 0 でエラーを返していない (別マシンのセッションを黙って飛ばそうとしてはいけない)")
	}
}
