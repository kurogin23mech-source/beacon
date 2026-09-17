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
// 端末種別テーブルが表示名を正しく引き、大文字/前後空白も吸収すること。
func TestTerminalAppName(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", "Terminal.app"},               // 不明は macOS 既定端末
		{"apple-terminal", "Terminal.app"}, // サーバの harness.kind 表記
		{" Apple_Terminal ", "Terminal.app"},
		{"iterm2", "iTerm2"},
		{"ITERM.APP", "iTerm2"}, // 大文字でも引ける
		{"kitty", "Terminal.app"}, // 未対応は既定表示名 (メッセージ用のみ)
	}
	for _, c := range cases {
		if got := terminalAppName(c.in); got != c.want {
			t.Errorf("terminalAppName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// 端末種別ごとに正しい AppleScript が選ばれ、未対応は fallback (理由付きエラー) になること。
func TestJumpScriptBuilder(t *testing.T) {
	// apple-terminal → Terminal.app を狙う。
	b, err := jumpScriptBuilder("apple-terminal")
	if err != nil || !strings.Contains(b("ttys000"), `application "Terminal"`) {
		t.Errorf("apple-terminal が Terminal.app を狙っていない (err=%v)", err)
	}
	// iterm2 → iTerm2 を狙う。
	b, err = jumpScriptBuilder("iterm2")
	if err != nil || !strings.Contains(b("ttys000"), `application "iTerm2"`) {
		t.Errorf("iterm2 が iTerm2 を狙っていない (err=%v)", err)
	}
	// 空 (種類不明) → 既定の Terminal.app。
	b, err = jumpScriptBuilder("")
	if err != nil || !strings.Contains(b("ttys000"), `application "Terminal"`) {
		t.Errorf("空 harness が Terminal.app 既定になっていない (err=%v)", err)
	}
	// 未対応 (kitty 等) → エラー (落とさず fallback の理由)。builder は nil。
	if b, err := jumpScriptBuilder("kitty"); err == nil || b != nil {
		t.Error("未対応端末で fallback エラーを返していない (silent に飛ばそうとしてはいけない)")
	}
}

// iTerm2 の前面化スクリプトが、対象 tty を含み iTerm2 を狙い、見つかったかを返すこと。
func TestITerm2JumpScript(t *testing.T) {
	s := iTerm2JumpScript("ttys007")
	if !strings.Contains(s, "/dev/ttys007") {
		t.Errorf("対象 tty が含まれていない: %q", s)
	}
	if !strings.Contains(s, `application "iTerm2"`) {
		t.Errorf("iTerm2 を狙っていない: %q", s)
	}
	if !strings.Contains(s, "return matched") {
		t.Errorf("見つかったかを返していない: %q", s)
	}
}

// 対応端末はテーブル (terminalDrivers) が唯一の正典。テストはそこから網羅を導出し、
// 別名を足しても (apple_terminal / iterm.app 含め) テスト漏れが起きないようにする。
func TestJumpableHarness(t *testing.T) {
	// テーブルに載る全別名は必ず jumpable。
	for _, h := range jumpableHarnessKinds() {
		if !jumpableHarness(h) {
			t.Errorf("jumpableHarness(%q) = false, want true (テーブルに在る別名)", h)
		}
	}
	// 大文字/前後空白も吸収する。
	if !jumpableHarness(" ITerm2 ") {
		t.Error("大文字/空白付きの別名が jumpable と判定されない")
	}
	// テーブルに無い端末は未対応 (fallback へ)。
	for _, h := range []string{"kitty", "vscode", "tmux", "wezterm"} {
		if jumpableHarness(h) {
			t.Errorf("jumpableHarness(%q) = true, want false", h)
		}
	}
}

// pid が無い (= 別マシンのセッション等) は、落とさず理由を返すこと。
func TestResolveTTYRejectsZeroPID(t *testing.T) {
	if _, err := resolveTTY(0); err == nil {
		t.Error("pid 0 でエラーを返していない (別マシンのセッションを黙って飛ばそうとしてはいけない)")
	}
}

// 「端末へ飛ぶ」が使える条件が 1 か所で正しく判定されること。
// macOS かつ loopback かつ 非公開 のときだけ true。他は全て false。
func TestJumpSupported(t *testing.T) {
	cases := []struct {
		goos, host string
		expose     bool
		want       bool
		why        string
	}{
		{"darwin", "127.0.0.1", false, true, "macOS + loopback + 非公開"},
		{"darwin", "localhost", false, true, "localhost も loopback"},
		{"linux", "127.0.0.1", false, false, "非 macOS は osascript が無い"},
		{"darwin", "0.0.0.0", false, false, "外向き host は loopback でない"},
		{"darwin", "127.0.0.1", true, false, "外部公開時は無効"},
	}
	for _, c := range cases {
		if got := jumpSupported(c.goos, c.host, c.expose); got != c.want {
			t.Errorf("jumpSupported(%q,%q,%v) = %v, want %v (%s)",
				c.goos, c.host, c.expose, got, c.want, c.why)
		}
	}
}

// resolveTTY のエラー処理を、実プロセスなしで (実行器を差し替えて) 確かめられること。
func TestResolveTTYWith(t *testing.T) {
	// 正常: ps が tty を返す。
	ok := func(name string, args ...string) ([]byte, error) {
		return []byte("ttys009\n"), nil
	}
	if got, err := resolveTTYWith(ok, 123); err != nil || got != "ttys009" {
		t.Errorf("正常系: got %q err %v, want ttys009", got, err)
	}
	// ps 自体が失敗 (プロセスがもう無い等)。
	boom := func(name string, args ...string) ([]byte, error) {
		return nil, errForTest("no such process")
	}
	if _, err := resolveTTYWith(boom, 123); err == nil {
		t.Error("ps 失敗時にエラーを返していない")
	}
	// 制御端末なし (?) は「端末の外」で落とさず理由を返す。
	noTTY := func(name string, args ...string) ([]byte, error) {
		return []byte("??\n"), nil
	}
	if _, err := resolveTTYWith(noTTY, 123); err == nil {
		t.Error("制御端末なしでエラーを返していない")
	}
	// pid<=0 は実行器を呼ばずに弾く。
	called := false
	spy := func(name string, args ...string) ([]byte, error) {
		called = true
		return nil, nil
	}
	if _, err := resolveTTYWith(spy, 0); err == nil || called {
		t.Errorf("pid 0 は実行器を呼ばずに弾くべき (called=%v err=%v)", called, err)
	}
}

// errForTest はテスト内で使う軽いエラー。
type errForTest string

func (e errForTest) Error() string { return string(e) }

// --- 最終判定 jumpVerdict (e-6427) ----------------------------------------

// 「飛べるか」の最終 1 値と、飛べない理由の閉じた enum が正しく出ること。
// 特に **別マシンと未対応端末が同じ false に潰れない** こと (#744 独立レビューの核心)。
func TestJumpVerdict(t *testing.T) {
	cases := []struct {
		envOK   bool
		remote  bool
		pid     int
		harness string
		wantOK  bool
		wantWhy string
		why     string
	}{
		{true, false, 100, "apple-terminal", true, "", "このマシン・対応端末は飛べる"},
		{true, false, 100, "", true, "", "harness 不明は Terminal.app 既定で飛べる"},
		{false, false, 100, "apple-terminal", false, JumpBlockedExposed,
			"環境 (公開中/非loopback/非macOS) が最優先で塞ぐ"},
		{false, true, 0, "kitty", false, JumpBlockedExposed,
			"環境起因は他のどの理由よりも優先"},
		{true, true, 0, "apple-terminal", false, JumpBlockedRemoteMachine,
			"別マシンは pid 無しでも remote-machine と名乗る (no-pid に潰れない)"},
		{true, false, 0, "apple-terminal", false, JumpBlockedNoPID,
			"このマシンで pid 不明"},
		{true, false, 100, "kitty", false, JumpBlockedUnsupportedTerminal,
			"未対応端末は fallback (パスのコピー) に回す理由として区別"},
	}
	for _, c := range cases {
		ok, why := jumpVerdict(c.envOK, c.remote, c.pid, c.harness)
		if ok != c.wantOK || why != c.wantWhy {
			t.Errorf("jumpVerdict(%v,%v,%d,%q) = (%v,%q), want (%v,%q) — %s",
				c.envOK, c.remote, c.pid, c.harness, ok, why, c.wantOK, c.wantWhy, c.why)
		}
	}
}

// 飛べない理由が閉じた enum に収まっていること。値の綴りは画面 (page.html) が
// 文字列比較で読むため、黙って変えると出し分けが silent に壊れる。変えるときは
// このテストと page.html の両方を意図的に更新すること。
func TestJumpBlockedEnumClosed(t *testing.T) {
	want := map[string]bool{
		"exposed": true, "remote-machine": true,
		"no-pid": true, "unsupported-terminal": true,
	}
	got := []string{JumpBlockedExposed, JumpBlockedRemoteMachine,
		JumpBlockedNoPID, JumpBlockedUnsupportedTerminal}
	if len(got) != len(want) {
		t.Fatalf("enum の数が合わない: %d / %d", len(got), len(want))
	}
	for _, v := range got {
		if !want[v] {
			t.Errorf("未知の理由値 %q (閉じた enum の外)", v)
		}
	}
}

// applyJumpVerdicts が全行に最終判定を書き込むこと (受け口はこれを呼ぶだけ)。
func TestApplyJumpVerdicts(t *testing.T) {
	rows := []SessionOverview{
		{PID: 100, Harness: "apple-terminal"},
		{Remote: true},
		{PID: 100, Harness: "kitty"},
	}
	applyJumpVerdicts(rows, true)
	if !rows[0].Jumpable || rows[0].JumpBlocked != "" {
		t.Errorf("飛べる行が飛べる印になっていない: %+v", rows[0])
	}
	if rows[1].Jumpable || rows[1].JumpBlocked != JumpBlockedRemoteMachine {
		t.Errorf("別マシン行の理由が違う: %+v", rows[1])
	}
	if rows[2].Jumpable || rows[2].JumpBlocked != JumpBlockedUnsupportedTerminal {
		t.Errorf("未対応端末行の理由が違う: %+v", rows[2])
	}
	// 環境が塞がっていれば、飛べたはずの行も exposed で塞がる。
	applyJumpVerdicts(rows, false)
	if rows[0].Jumpable || rows[0].JumpBlocked != JumpBlockedExposed {
		t.Errorf("環境起因の塞ぎが効いていない: %+v", rows[0])
	}
}

// --- 前面化失敗の status 対応 (e-6429) --------------------------------------

// 失敗理由ごとに HTTP status が「入力を直しても無駄か」を伝えること。
// 501 = 環境 (OS / 端末種別)、400 = 入力 (pid)、404 = 実体不在、500 = 実行失敗。
func TestJumpToTerminalWithStatuses(t *testing.T) {
	// 実行器 stub: ps は tty を返し、osascript は指定の結果を返す。
	runner := func(psOut string, psErr error, osaOut string, osaErr error) commandRunner {
		return func(name string, args ...string) ([]byte, error) {
			if name == "ps" {
				return []byte(psOut), psErr
			}
			return []byte(osaOut), osaErr
		}
	}
	status := func(err error) int {
		jf, ok := err.(*jumpFailure)
		if !ok {
			t.Fatalf("jumpFailure でないエラー: %v", err)
		}
		return jf.Status
	}
	// 非 macOS は 501 (環境起因)。
	if err := jumpToTerminalWith(runner("ttys000", nil, "true", nil),
		"linux", 100, "apple-terminal"); status(err) != 501 {
		t.Errorf("非 macOS が 501 でない: %v", err)
	}
	// 未対応端末は 501 (環境起因)。
	if err := jumpToTerminalWith(runner("ttys000", nil, "true", nil),
		"darwin", 100, "kitty"); status(err) != 501 {
		t.Errorf("未対応端末が 501 でない: %v", err)
	}
	// pid 無効は 400 (入力起因)。
	if err := jumpToTerminalWith(runner("ttys000", nil, "true", nil),
		"darwin", 0, "apple-terminal"); status(err) != 400 {
		t.Errorf("pid 無効が 400 でない: %v", err)
	}
	// プロセスが消えている (ps 失敗) は 404 (実体不在)。
	if err := jumpToTerminalWith(runner("", errForTest("gone"), "true", nil),
		"darwin", 100, "apple-terminal"); status(err) != 404 {
		t.Errorf("プロセス不在が 404 でない: %v", err)
	}
	// タブが見つからない (osascript が false) も 404。
	if err := jumpToTerminalWith(runner("ttys000", nil, "false", nil),
		"darwin", 100, "apple-terminal"); status(err) != 404 {
		t.Errorf("タブ不在が 404 でない: %v", err)
	}
	// osascript 実行失敗は 500。
	if err := jumpToTerminalWith(runner("ttys000", nil, "", errForTest("boom")),
		"darwin", 100, "apple-terminal"); status(err) != 500 {
		t.Errorf("osascript 失敗が 500 でない: %v", err)
	}
	// 正常系はエラー無し。
	if err := jumpToTerminalWith(runner("ttys000", nil, "true\n", nil),
		"darwin", 100, "apple-terminal"); err != nil {
		t.Errorf("正常系でエラー: %v", err)
	}
}
