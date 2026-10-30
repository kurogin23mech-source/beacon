package main

import (
	"errors"
	"os"
	"os/exec"
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
	if !strings.Contains(script, "return false") || !strings.Contains(script, "return true") {
		t.Errorf("見つかったかを true/false で返していない (見つからない時に落とさないため): %q", script)
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
		{"ITERM.APP", "iTerm2"},   // 大文字でも引ける
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
	if !strings.Contains(s, "return false") || !strings.Contains(s, "return true") {
		t.Errorf("見つかったかを true/false で返していない: %q", s)
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
	// 名乗り側の生の形 (直す前の bridge が送る `terminal:<TERM_PROGRAM>`) も、
	// 接頭辞を剥がして表に当てる (e-7199: iTerm2 がこの形で来て一度も飛べなかった)。
	for _, h := range []string{"terminal:iTerm.app", "terminal:Apple_Terminal", " Terminal:ITERM.APP "} {
		if !jumpableHarness(h) {
			t.Errorf("jumpableHarness(%q) = false, want true (生の形は接頭辞を剥がして表引き)", h)
		}
	}
	// テーブルに無い端末は未対応 (fallback へ)。生の形でも同じ。
	for _, h := range []string{"kitty", "vscode", "tmux", "wezterm", "terminal:kitty", "terminal:WezTerm"} {
		if jumpableHarness(h) {
			t.Errorf("jumpableHarness(%q) = true, want false", h)
		}
	}
	// 生の形は rawNames との完全一致でだけ受ける。kinds の汎用別名 ("" / "terminal") や
	// 短縮形 ("iterm")、正典名 ("iterm2") に生の形から落ちてはならない — 未知の端末が
	// 偶然その名で名乗ると既知端末に化ける (PR#820 独立 AX レビュー)。
	for _, h := range []string{"terminal:", "terminal:terminal", "terminal:Terminal", "terminal:iterm", "terminal:iterm2", "terminal:apple-terminal"} {
		if jumpableHarness(h) {
			t.Errorf("jumpableHarness(%q) = true, want false (生の形は rawNames 完全一致のみ)", h)
		}
	}
	// 逆に、正典名の面 (kinds) には rawNames の値だけを頼りに当てない — rawNames の
	// 値は kinds にも載せてあるので今は同じ結果だが、照合面が分かれていることを
	// driverForHarness の raw フラグ経由で確かめる (下の TestSplitHarnessKind)。
}

// 生の形の分解: 接頭辞 `terminal:` の有無で照合面 (rawNames / kinds) を切り替え、
// 鍵は小文字化・空白除去する。分解した結果がどの端末アプリを狙うかまで確かめる。
func TestSplitHarnessKind(t *testing.T) {
	cases := []struct {
		in   string
		raw  bool
		want string
	}{
		{"terminal:iTerm.app", true, "iterm.app"},
		{"terminal:Apple_Terminal", true, "apple_terminal"},
		{" Terminal:ITERM2 ", true, "iterm2"},
		{"terminal:", true, ""},
		{"iterm2", false, "iterm2"},
		{"terminal", false, "terminal"}, // 接頭辞ではない素の値は触らない
		{"terminal:kitty", true, "kitty"},
		{"", false, ""},
	}
	for _, c := range cases {
		raw, got := splitHarnessKind(c.in)
		if raw != c.raw || got != c.want {
			t.Errorf("splitHarnessKind(%q) = (%v, %q), want (%v, %q)", c.in, raw, got, c.raw, c.want)
		}
	}
	// 全 driver の rawNames は小文字で、その driver の kinds にも載っている
	// (生の形でも正典名でも同じ driver に着く)。
	for _, d := range terminalDrivers {
		for _, rn := range d.rawNames {
			if rn != strings.ToLower(rn) || rn == "" {
				t.Errorf("%s の rawName %q は小文字・非空でなければならない", d.appName, rn)
			}
			if driverForHarness(rawTerminalPrefix+rn) != driverForHarness(rn) {
				t.Errorf("%s: 生の形 %q と素の %q が別の driver に着く", d.appName, rawTerminalPrefix+rn, rn)
			}
		}
	}
	// 生の iTerm.app は iTerm2 を狙う (Terminal.app に誤って落ちない)。
	if got := terminalAppName("terminal:iTerm.app"); got != "iTerm2" {
		t.Errorf("terminalAppName(terminal:iTerm.app) = %q, want iTerm2", got)
	}
	b, err := jumpScriptBuilder("terminal:iTerm.app")
	if err != nil || !strings.Contains(b("ttys000"), `application "iTerm2"`) {
		t.Errorf("terminal:iTerm.app が iTerm2 を狙っていない (err=%v)", err)
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
		envBlocked string
		remote     bool
		pid        int
		harness    string
		wantOK     bool
		wantWhy    string
		why        string
	}{
		{"", false, 100, "apple-terminal", true, "", "このマシン・対応端末は飛べる"},
		{"", false, 100, "", true, "", "harness 不明は Terminal.app 既定で飛べる"},
		{JumpBlockedExposed, false, 100, "apple-terminal", false, JumpBlockedExposed,
			"環境 (公開中/非loopback) が最優先で塞ぐ"},
		{JumpBlockedUnsupportedOS, true, 0, "kitty", false, JumpBlockedUnsupportedOS,
			"環境起因は他のどの理由よりも優先。OS 起因は exposed に潰れない (#756 AX)"},
		{"", true, 0, "apple-terminal", false, JumpBlockedRemoteMachine,
			"別マシンは pid 無しでも remote-machine と名乗る (no-pid に潰れない)"},
		{"", false, 0, "apple-terminal", false, JumpBlockedNoPID,
			"このマシンで pid 不明"},
		{"", false, 100, "kitty", false, JumpBlockedUnsupportedTerminal,
			"未対応端末は fallback (パスのコピー) に回す理由として区別"},
	}
	for _, c := range cases {
		ok, why := jumpVerdict(c.envBlocked, c.remote, c.pid, c.harness)
		if ok != c.wantOK || why != c.wantWhy {
			t.Errorf("jumpVerdict(%q,%v,%d,%q) = (%v,%q), want (%v,%q) — %s",
				c.envBlocked, c.remote, c.pid, c.harness, ok, why, c.wantOK, c.wantWhy, c.why)
		}
	}
}

// 環境ブロック理由の判定 (jumpEnvBlocked)。OS 起因と公開起因は直し方が違うので
// 別の値で返ること (#756 独立 AX レビュー)。
func TestJumpEnvBlocked(t *testing.T) {
	cases := []struct {
		goos, host string
		expose     bool
		want       string
	}{
		{"darwin", "127.0.0.1", false, ""},
		{"linux", "127.0.0.1", false, JumpBlockedUnsupportedOS},
		{"linux", "0.0.0.0", true, JumpBlockedUnsupportedOS}, // OS 起因が公開起因より先
		{"darwin", "0.0.0.0", true, JumpBlockedExposed},
		{"darwin", "192.168.1.10", false, JumpBlockedExposed},
	}
	for _, c := range cases {
		if got := jumpEnvBlocked(c.goos, c.host, c.expose); got != c.want {
			t.Errorf("jumpEnvBlocked(%q,%q,%v) = %q, want %q",
				c.goos, c.host, c.expose, got, c.want)
		}
	}
}

// 飛べない理由が閉じた enum に収まっていること。値のうち画面 (page.html) が
// 文字列比較で読むのは "unsupported-terminal" だけ (それ以外はボタン非表示のみで
// 描画分岐を持たない — #756 独立 AX レビューで過大宣言を修正)。その 1 値は下の
// TestJumpBlockedLiteralMatchesPage が Go 定数と page.html の一致を機械検証するので、
// 定数の綴りを変えると即座に赤くなる。
func TestJumpBlockedEnumClosed(t *testing.T) {
	want := map[string]bool{
		"exposed": true, "unsupported-os": true, "remote-machine": true,
		"no-pid": true, "unsupported-terminal": true,
	}
	got := []string{JumpBlockedExposed, JumpBlockedUnsupportedOS,
		JumpBlockedRemoteMachine, JumpBlockedNoPID, JumpBlockedUnsupportedTerminal}
	if len(got) != len(want) {
		t.Fatalf("enum の数が合わない: %d / %d", len(got), len(want))
	}
	for _, v := range got {
		if !want[v] {
			t.Errorf("未知の理由値 %q (閉じた enum の外)", v)
		}
	}
}

// page.html が文字列で消費する enum 値が、Go 定数と一致していること (#756
// 独立レビュー consensus: コメントで「両方更新せよ」と頼むのは構造ガードではない。
// 定数の綴りを変えたら page.html の対応表 (JUMP_BLOCKED_REASON = 飛べない理由を
// 人の言葉にする map、e-6549) が silent に引けなくなる前に、ここが赤くなる)。
func TestJumpBlockedLiteralMatchesPage(t *testing.T) {
	page, err := os.ReadFile("page.html")
	if err != nil {
		t.Fatalf("page.html が読めない: %v", err)
	}
	for _, v := range []string{JumpBlockedExposed, JumpBlockedUnsupportedOS,
		JumpBlockedRemoteMachine, JumpBlockedNoPID, JumpBlockedUnsupportedTerminal} {
		needle := `"` + v + `"`
		if !strings.Contains(string(page), needle) {
			t.Errorf("page.html に %s が無い — Go 定数と画面の理由対応表"+
				" (JUMP_BLOCKED_REASON) がずれている (飛べない理由の表示が silent に壊れる)", needle)
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
	applyJumpVerdicts(rows, "")
	if !rows[0].Jumpable || rows[0].JumpBlocked != "" {
		t.Errorf("飛べる行が飛べる印になっていない: %+v", rows[0])
	}
	if rows[1].Jumpable || rows[1].JumpBlocked != JumpBlockedRemoteMachine {
		t.Errorf("別マシン行の理由が違う: %+v", rows[1])
	}
	if rows[2].Jumpable || rows[2].JumpBlocked != JumpBlockedUnsupportedTerminal {
		t.Errorf("未対応端末行の理由が違う: %+v", rows[2])
	}
	// 環境が塞がっていれば、飛べたはずの行もその環境理由で塞がる。
	applyJumpVerdicts(rows, JumpBlockedExposed)
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

// --- 並び替えとの衝突を構造で禁じる (e-7219) ---------------------------------

// windowLoopBody は AppleScript から「windows をなめているブロック」の中身を抜き出す
// (純関数)。`repeat with ... in windows` の行から、対応する `end repeat` までを返す。
//
// 入れ子の数え方: 行頭が `repeat` で始まる行が開き、`end repeat` だけの行が閉じる。
// `exit repeat` は "repeat" を含むが開きではないので数えない。
func windowLoopBody(script string) (string, bool) {
	lines := strings.Split(script, "\n")
	start := -1
	for i, ln := range lines {
		t := strings.TrimSpace(ln)
		if strings.HasPrefix(t, "repeat with ") && strings.HasSuffix(t, " in windows") {
			start = i
			break
		}
	}
	if start < 0 {
		return "", false
	}
	depth := 0
	for i := start; i < len(lines); i++ {
		t := strings.TrimSpace(lines[i])
		switch {
		case t == "end repeat":
			depth--
			if depth == 0 {
				return strings.Join(lines[start+1:i], "\n"), true
			}
		case strings.HasPrefix(t, "repeat"):
			depth++
		}
	}
	return "", false
}

// scanPhaseViolation は、窓をなめているブロックの中に**副作用を持つ文**が
// 在れば、その行を返す (無ければ空文字列)。
//
// **なぜ blocklist をやめたか** (独立レビュー 2 体の指摘、e-7219): 以前は
// 「select / set frontmost / set selected / activate が在れば赤」という固定語彙の
// blocklist だった。judge が走査ループに `set index of w to 1` (= 窓の重なり順を
// 変える別の書き方、-1719 と同じ故障クラス) を注入したところ、**ガードは緑のまま
// 通った**。語彙に無い書き方で同じバグを再導入できる = 「緑だから安全」という
// 確信を裏切る偽の安全で、ガード自身が守るはずの原則を破っていた。
//
// そこで判定を反転した。**許される形だけを並べ、それ以外は全部赤**にする
// (fail-closed)。走査段に要るのは「読む」「分岐する」「ローカル変数に控える」
// だけなので、許可リストは閉じられる。未知の動詞も、将来 AppleScript に増える
// 書き方も、列挙しないまま捕まる。
//
// 許す形:
//   - 空行 / コメント (`--`)
//   - 制御構文: `repeat …` / `end repeat` / `end if` / `exit repeat` /
//     `if … then` (末尾に `exit repeat` が付く 1 行形も可)
//   - ローカル変数への代入: `set <裸の識別子> to …`
//     (代入先に ` of ` を含むものは**端末オブジェクトのプロパティ書き換え**なので禁止)
func scanPhaseViolation(body string) string {
	for _, raw := range strings.Split(body, "\n") {
		t := strings.TrimSpace(raw)
		if t == "" || strings.HasPrefix(t, "--") {
			continue
		}
		if t == "end repeat" || t == "end if" || t == "exit repeat" ||
			strings.HasPrefix(t, "repeat ") || strings.HasPrefix(t, "repeat\t") {
			continue
		}
		// `if … then` 単独、または `if … then exit repeat` の 1 行形。
		// `then` の後に続けて書けるのは制御の離脱だけに限る (`then select w` を通さない)。
		if strings.HasPrefix(t, "if ") {
			if idx := strings.LastIndex(t, " then"); idx >= 0 {
				tail := strings.TrimSpace(t[idx+len(" then"):])
				if tail == "" || tail == "exit repeat" {
					continue
				}
			}
			return t
		}
		// ローカル変数への代入だけ許す。代入先が ` of ` を含む = 端末オブジェクトの
		// プロパティを書き換えているので禁止 (`set frontmost of w to true` /
		// `set index of w to 1` / `set bounds of w to …` が全部ここで落ちる)。
		if strings.HasPrefix(t, "set ") {
			if idx := strings.Index(t, " to "); idx >= 0 {
				target := strings.TrimSpace(strings.TrimPrefix(t[:idx], "set "))
				if !strings.Contains(target, " of ") && target != "" {
					continue
				}
			}
			return t
		}
		// それ以外は全部赤 (= 命令動詞 select / activate / close / reveal …、
		// および将来増える未知の書き方)。
		return t
	}
	return ""
}

// 前面化スクリプトは、窓をなめている最中に副作用のある文を実行してはならない。
//
// これが e-7219 の真因そのもの: 窓の並びを変える操作は `windows` の並びそのものを
// 変えるので、3 タブの窓の最後でないタブに一致して並びが変わると、
// `item N of every window` が 1 タブの窓を指すようになり、次の `item 2 of every tab`
// が範囲外になって落ちる。実機で旧スクリプトは 48 試行中 15 件失敗、新スクリプトは
// 48/48 成功 (手順: scripts/verify-jump-terminal-reorder.sh)。
//
// **テーブル (terminalDrivers) から導出して回す**ので、新しい端末 driver を足しても
// この禁止が自動で効く (AC「片方だけ直して終わりにしない」)。
func TestJumpScriptsDoNotMutateWhileScanning(t *testing.T) {
	for _, d := range terminalDrivers {
		script := d.script("ttys005")
		body, ok := windowLoopBody(script)
		if !ok {
			t.Errorf("%s: windows をなめるブロックが見つからない (形が変わった?): %q",
				d.appName, script)
			continue
		}
		if bad := scanPhaseViolation(body); bad != "" {
			t.Errorf("%s: 窓をなめている最中に副作用のある文がある: %q\n"+
				"並びが変わって相対参照が壊れる (-1719) 原因そのもの。第 1 段は"+
				"読むだけにして、選択は id の絶対参照で解決し直した第 2 段で行うこと (e-7219)",
				d.appName, bad)
		}
		// 第 2 段が id の絶対参照で解決し直していること。相対参照 (window 3 等) を
		// 持ち回ると、解決し直す時点で同じ問題を踏む (e-7219 で実測済み)。
		if !strings.Contains(script, "first window whose id is") {
			t.Errorf("%s: 選択対象を id の絶対参照で解決し直していない: %q",
				d.appName, script)
		}
	}
}

// legacyMutatingJumpScript は e-7219 で**実際に壊れていた**前面化スクリプトを返す
// (走査中に選択する形)。**この形の正典はここ 1 箇所**。
//
// ガードの自己検査 (下) と、実機検証スクリプトの `--with-old` (修正前が本当に落ちる
// ことを同条件で示す基準線) の両方がこれを使う。以前は 2 箇所に手で書かれていて、
// 片方だけ直すと「単体テストが検出する旧形式」と「実機で測る旧形式」が別物を指して
// 食い違う状態だった (独立保守性レビュー指摘)。実機側は
// TestDumpJumpScriptsForManualVerification 経由でこの関数から書き出す。
func legacyMutatingJumpScript(harness, tty string) string {
	dev := "/dev/" + tty
	if harness == "iterm2" {
		return `tell application "iTerm2"
  set matched to false
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "` + dev + `" then
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
end tell`
	}
	return `tell application "Terminal"
  set matched to false
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "` + dev + `" then
        set matched to true
        set selected of t to true
        set frontmost of w to true
      end if
    end repeat
  end repeat
  if matched then activate
  return matched
end tell`
}

// ガード自体が drift で赤くなることを確かめる (test-the-test)。
//
// **実際に壊れていた 2 形だけでなく、同じ故障クラスの別の書き方も食わせる。**
// 固定語彙の blocklist だった頃は `set index of w to 1` が素通りしていた
// (独立レビュー 2 体が実証)。許可リストに反転した今、語彙を列挙していない書き方も
// 捕まることを、ここで毎回実走させて確認する。緩い検査が「常に緑」のまま偽の安全を
// 作るのを防ぐのがこのテストの仕事。
func TestJumpScriptGuardCatchesTheOldForm(t *testing.T) {
	// 1) 実機で落ちた旧形式そのもの (正典は legacyMutatingJumpScript)。
	for _, harness := range []string{"iterm2", "apple-terminal"} {
		body, ok := windowLoopBody(legacyMutatingJumpScript(harness, "ttys005"))
		if !ok {
			t.Fatalf("%s: 旧形式から windows ループを抜き出せない (抽出器が壊れている)", harness)
		}
		if scanPhaseViolation(body) == "" {
			t.Errorf("%s: 旧形式 (実機で落ちた形) を検出できていない。"+
				"このガードは drift で赤くならないので偽の安全になっている", harness)
		}
	}

	// 2) 語彙を列挙していない「同じ故障クラス」の書き方。blocklist 時代はここで
	//    素通りしていた。1 行でも通ると偽の安全が復活するので全件赤を要求する。
	for _, stmt := range []string{
		"select w",
		"set frontmost of w to true",
		"set selected of t to true",
		"activate",
		"set index of w to 1",                 // blocklist が取り逃していた実例
		"set bounds of w to {0, 0, 100, 100}", // 同クラス (窓の形を変える)
		"close w",                             // 窓が消えて添字が範囲外になる
		"reveal w",                            // 架空の未知動詞 (列挙できない将来)
		"if true then select w",               // then の後ろに隠す形
	} {
		body := "    repeat with t in tabs of w\n      " + stmt + "\n    end repeat"
		if bad := scanPhaseViolation(body); bad == "" {
			t.Errorf("走査段の副作用 %q を検出できていない (許可リストに穴がある)", stmt)
		}
	}

	// 3) 逆に、走査段に在ってよい形は通ること (偽陽性で正しい変更を妨げない)。
	for _, stmt := range []string{
		"set wid to id of w",
		"set sid to id of s",
		"if tty of s is \"/dev/ttys005\" then",
		"end if",
		"exit repeat",
		"if sid is not \"\" then exit repeat",
		"-- コメント",
	} {
		if bad := scanPhaseViolation("      " + stmt); bad != "" {
			t.Errorf("走査段に在ってよい形 %q を誤って弾いている (偽陽性)", bad)
		}
	}
}

// osascript の失敗が、画面から原因を辿れる文面になること (e-7219 の AC)。
// exec.ExitError を %v しただけでは "exit status 1" しか出ず、並び順の問題だと
// 読み取れなかった。標準エラーを拾い、-1719 には平叙文の説明を添える。
func TestOsascriptFailureDetail(t *testing.T) {
	// 標準エラーが在れば、それを出す (終了コードではなく中身)。
	ee := &exec.ExitError{ProcessState: nil}
	ee.Stderr = []byte("  execution error: ... 正しくないインデックスです。 (-1719)\n")
	got := osascriptFailureDetail(ee)
	if !strings.Contains(got, "-1719") {
		t.Errorf("標準エラーの中身を拾っていない: %q", got)
	}
	if !strings.Contains(got, "並び") {
		t.Errorf("-1719 に平叙文の説明が添えられていない (番号だけでは原因が辿れない): %q", got)
	}
	// 標準エラーが空なら、元のエラー文字列に落ちる (情報を失わない)。
	plain := errors.New("exec: \"osascript\": executable file not found")
	if got := osascriptFailureDetail(plain); !strings.Contains(got, "osascript") {
		t.Errorf("標準エラーが無い場合に元のエラーを失っている: %q", got)
	}
}

// 手動の実機検証用に、生成される AppleScript をファイルへ書き出す
// (scripts/verify-jump-terminal-reorder.sh が呼ぶ)。
//
// 実機検証でも**本番と同じ生成器を通す**ための継ぎ目。スクリプトを検証側に
// 書き写すと、本番の生成器を直しても検証が古い形を測り続ける (= 緑のまま壊れる)。
// 環境変数が無いときは何もしない (通常のテスト実行では skip)。
func TestDumpJumpScriptsForManualVerification(t *testing.T) {
	out := os.Getenv("BEACON_JUMP_SCRIPT_OUT")
	ttys := os.Getenv("BEACON_JUMP_SCRIPT_TTYS")
	harness := os.Getenv("BEACON_JUMP_SCRIPT_HARNESS")
	if out == "" || ttys == "" || harness == "" {
		t.Skip("BEACON_JUMP_SCRIPT_OUT / _TTYS / _HARNESS 未設定 (実機検証時のみ使う)")
	}
	build, err := jumpScriptBuilder(harness)
	if err != nil {
		t.Fatalf("harness %q の生成器が無い: %v", harness, err)
	}
	for _, tty := range strings.Split(ttys, ",") {
		tty = strings.TrimSpace(tty)
		if tty == "" {
			continue
		}
		if err := os.WriteFile(out+"/jump_"+tty+".applescript",
			[]byte(build(tty)), 0o644); err != nil {
			t.Fatalf("書き出せない: %v", err)
		}
		// BEACON_JUMP_SCRIPT_LEGACY=1 のときは、修正前の形も同じ正典
		// (legacyMutatingJumpScript) から書き出す。実機検証の --with-old が
		// 「修正前は本当に落ちる」を同条件で示すための基準線で、以前は
		// シェル側に手で書き写されていた (独立保守性レビュー指摘)。
		if os.Getenv("BEACON_JUMP_SCRIPT_LEGACY") == "1" {
			if err := os.WriteFile(out+"/legacy_"+tty+".applescript",
				[]byte(legacyMutatingJumpScript(harness, tty)), 0o644); err != nil {
				t.Fatalf("旧形式を書き出せない: %v", err)
			}
		}
	}
}
