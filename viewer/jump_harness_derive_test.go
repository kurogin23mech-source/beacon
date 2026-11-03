package main

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// 端末の種類を「名乗りが無ければプロセスに訊く」経路 (e-7261)。
//
// 実機で起きたこと (2026-11-02 実測): iTerm しか使っていない機械で、運用室の
// 11 行のうち 4 行が「飛べる」と表示されたまま、押しても何も起きなかった。
// 内訳は名乗りが届いていない 3 行 (名簿に居ない = harness "") と、bridge が
// TERM_PROGRAM を継承できず下限値を送った 1 行 (harness "terminal")。どちらも
// Apple Terminal の別名として登録されていたため、Terminal.app 用の AppleScript
// を iTerm の tty に投げていた。対象が見つからないので無反応になる。
//
// ここで固定するのは 2 つの約束:
//   1. 「分からない」を既定値で埋めない (嘘の「飛べる」を作らない)
//   2. 分からないときはプロセスに訊いて、**実際に飛べるようにする**

// fakeRunner は ps の応答を差し替える (pid → TERM_PROGRAM の値)。
func fakeRunner(env map[int]string, calls *[]string) commandRunner {
	return func(name string, args ...string) ([]byte, error) {
		if calls != nil {
			*calls = append(*calls, name+" "+strings.Join(args, " "))
		}
		if name == "ps" && len(args) >= 5 && args[0] == "-E" {
			pid := 0
			fmt.Sscanf(args[len(args)-1], "%d", &pid)
			v, ok := env[pid]
			if !ok {
				return nil, errors.New("no such process")
			}
			if v == "" { // 端末の外 (TERM_PROGRAM 無し)
				return []byte("/bin/claude PATH=/usr/bin SHELL=/bin/zsh\n"), nil
			}
			return []byte("/bin/claude PATH=/usr/bin TERM_PROGRAM=" + v +
				" TERM=xterm-256color\n"), nil
		}
		return nil, errors.New("unexpected call")
	}
}

func TestTermProgramForPID(t *testing.T) {
	run := fakeRunner(map[int]string{10: "iTerm.app", 11: "Apple_Terminal", 12: ""}, nil)
	// e-7265: 「読めたが端末ではない」と「読めなかった」を分けて返す。
	for _, c := range []struct {
		pid     int
		want    string
		wantRes probeResult
		why     string
	}{
		{10, "iTerm.app", probeOK, "読めた"},
		{11, "Apple_Terminal", probeOK, "読めた"},
		{12, "", probeNoTerminal, "読めたが端末の外 (TERM_PROGRAM 無し)"},
		{99, "", probeUnreadable, "プロセスが居ない = 読めなかった"},
		{0, "", probeUnreadable, "pid 無効 — ps を叩かずに読めなかった扱い"},
		{-1, "", probeUnreadable, "同上"},
	} {
		got, res := termProgramForPIDWith(run, c.pid)
		if got != c.want || res != c.wantRes {
			t.Errorf("termProgramForPIDWith(pid=%d) = (%q,%v), want (%q,%v) — %s",
				c.pid, got, res, c.want, c.wantRes, c.why)
		}
	}
}

func TestResolveHarnessPrefersDeclarationThenProcess(t *testing.T) {
	run := fakeRunner(map[int]string{10: "iTerm.app", 11: "Apple_Terminal", 12: ""}, nil)
	for _, c := range []struct {
		pid      int
		declared string
		want     string
		why      string
	}{
		// 名乗りが端末を特定していれば信じる (別マシンでも効く唯一の材料)。
		{10, "iterm2", "iterm2", "特定済みの名乗りは尊重する"},
		{10, "apple-terminal", "apple-terminal", "特定済みの名乗りは尊重する (別端末でも)"},
		{10, "terminal:iTerm.app", "terminal:iTerm.app", "生の形も特定済み"},
		// 分からない名乗りはプロセスに訊く。
		{10, "", "terminal:iTerm.app", "空 → プロセスの TERM_PROGRAM から導出"},
		{10, "terminal", "terminal:iTerm.app", "下限値 → 同じく導出 (これが実機の 1 行)"},
		{11, "", "terminal:Apple_Terminal", "Apple Terminal も同じ経路で導出"},
		// 訊いても分からなければ不明のまま (= jumpVerdict が正直に落とす)。
		{12, "", "", "端末の外のプロセスは導出できない"},
		{99, "terminal", "terminal", "プロセスが居なければ名乗りのまま"},
		{0, "", "", "pid が無ければ導出しない"},
	} {
		got, _ := resolveHarnessWith(run, c.pid, c.declared)
		if got != c.want {
			t.Errorf("resolveHarnessWith(pid=%d, %q) = %q, want %q — %s",
				c.pid, c.declared, got, c.want, c.why)
		}
	}
}

// 導出した値が、既存の受け付け表でそのまま解決すること。
// **語彙を増やしていない**ことの証明でもある: TERM_PROGRAM を小文字にした綴りが
// rawNames そのものなので、導出結果は既存の生の形の経路で解ける。
func TestDerivedHarnessResolvesToTheRightDriver(t *testing.T) {
	run := fakeRunner(map[int]string{10: "iTerm.app", 11: "Apple_Terminal"}, nil)
	for _, c := range []struct {
		pid     int
		wantApp string
	}{
		{10, "iTerm2"}, {11, "Terminal.app"},
	} {
		h, _ := resolveHarnessWith(run, c.pid, "")
		d := driverForHarness(h)
		if d == nil {
			t.Fatalf("導出した %q が受け付け表で解決しない (語彙がずれている)", h)
		}
		if d.appName != c.wantApp {
			t.Errorf("pid=%d → %q を狙っている, want %s", c.pid, d.appName, c.wantApp)
		}
		ok, reason := jumpVerdict("", false, c.pid, h)
		if !ok {
			t.Errorf("pid=%d は導出後に飛べるべき (reason=%q)", c.pid, reason)
		}
	}
}

// 一覧側: 名乗りが無い行だけ埋め、特定済みの行と別マシンの行は ps を叩かない。
func TestResolveHarnessesOnlyAsksWhenNeeded(t *testing.T) {
	var calls []string
	run := fakeRunner(map[int]string{10: "iTerm.app", 20: "iTerm.app", 30: "iTerm.app"}, &calls)
	sessions := []SessionOverview{
		{PID: 10, Harness: ""},               // 訊く
		{PID: 20, Harness: "terminal"},       // 訊く
		{PID: 30, Harness: "iterm2"},         // 訊かない (特定済み)
		{PID: 40, Harness: "", Remote: true}, // 訊かない (別マシン)
		{PID: 0, Harness: ""},                // 訊かない (pid 無し)
	}
	resolveHarnessesWith(run, sessions)

	if sessions[0].Harness != "terminal:iTerm.app" || sessions[1].Harness != "terminal:iTerm.app" {
		t.Errorf("名乗りが無い行が埋まっていない: %q / %q",
			sessions[0].Harness, sessions[1].Harness)
	}
	if sessions[2].Harness != "iterm2" {
		t.Errorf("特定済みの名乗りを上書きした: %q", sessions[2].Harness)
	}
	if sessions[3].Harness != "" || sessions[4].Harness != "" {
		t.Errorf("別マシン / pid 無しの行を埋めた: %q / %q",
			sessions[3].Harness, sessions[4].Harness)
	}
	if len(calls) != 2 {
		t.Errorf("ps を %d 回叩いた (埋めるべき 2 行だけのはず): %v", len(calls), calls)
	}
}

// 飛ぶ本体でも再確定すること。画面は一覧で配った値を送ってくるので、古いタブや
// API を直叩きする呼び出し元という **別の入口** が残る。片方だけ塞ぐと経路で
// 挙動が割れる。
func TestJumpResolvesHarnessItself(t *testing.T) {
	var script string
	run := func(name string, args ...string) ([]byte, error) {
		switch {
		case name == "ps" && len(args) >= 1 && args[0] == "-E":
			return []byte("/bin/claude TERM_PROGRAM=iTerm.app\n"), nil
		case name == "ps":
			return []byte("ttys003\n"), nil // tty 解決
		case name == "osascript":
			script = args[len(args)-1]
			return []byte("true\n"), nil
		}
		return nil, errors.New("unexpected " + name)
	}
	// 画面が空の harness を送ってきても、iTerm2 を狙うこと。
	if err := jumpToTerminalWith(run, "darwin", 123, ""); err != nil {
		t.Fatalf("空 harness で飛べなかった: %v", err)
	}
	if !strings.Contains(script, `application "iTerm2"`) {
		t.Errorf("Terminal.app を狙っている (導出が効いていない): %.80s", script)
	}
}

// 素性 (interactive / bg) で「飛べない理由」を割ること (e-7263)。
//
// 実測 (2026-11-02): Claude Code の台帳は kind='bg' と 'interactive' を区別して
// 持っているのに viewer が読んで捨てていたため、バックグラウンドのセッション
// (pe-schema-merge-status / pid 78608) が「この端末は前面化に未対応です」と
// 表示されていた。対処が真逆になる 2 つの理由が 1 つの値に潰れていた形。
func TestBackgroundSessionGetsItsOwnReason(t *testing.T) {
	for _, c := range []struct {
		kind    string
		harness string
		wantOK  bool
		wantWhy string
		why     string
	}{
		{"bg", "iterm2", false, JumpBlockedBackgroundSession,
			"端末種別が対応済みでも、bg は飛び先が無い"},
		{"bg", "", false, JumpBlockedBackgroundSession,
			"bg は端末種別より先に判定する (端末の話に潰さない)"},
		{"interactive", "iterm2", true, "",
			"interactive は従来どおり飛べる"},
		{"interactive", "kitty", false, JumpBlockedUnsupportedTerminal,
			"interactive で未対応端末なら端末種別の理由"},
		{"", "iterm2", true, "",
			"素性が分からない道具は従来どおり端末種別だけで判定する"},
		{"", "kitty", false, JumpBlockedUnsupportedTerminal,
			"素性不明を bg 扱いにして止めない"},
	} {
		ok, why := jumpVerdictKind("", false, 100, c.harness, c.kind, "")
		if ok != c.wantOK || why != c.wantWhy {
			t.Errorf("jumpVerdictKind(kind=%q, harness=%q) = (%v,%q), want (%v,%q) — %s",
				c.kind, c.harness, ok, why, c.wantOK, c.wantWhy, c.why)
		}
	}
}

// 素性より影響範囲の広い理由が先に出ること (順序の契約)。
func TestBroaderReasonsWinOverKind(t *testing.T) {
	if _, why := jumpVerdictKind(JumpBlockedExposed, false, 100, "iterm2", "bg", ""); why != JumpBlockedExposed {
		t.Errorf("公開中は盤全体の話なので素性より先: got %q", why)
	}
	if _, why := jumpVerdictKind("", true, 100, "iterm2", "bg", ""); why != JumpBlockedRemoteMachine {
		t.Errorf("別マシンは素性より先: got %q", why)
	}
	if _, why := jumpVerdictKind("", false, 0, "iterm2", "bg", ""); why != JumpBlockedNoPID {
		t.Errorf("pid 無しは素性より先: got %q", why)
	}
}

// e-7265: 「プロセスを読めなかった」を端末種別の話に潰さないこと。
//
// 独立 AX レビューが ps を差し替えて実測し、権限不足 / プロセス消失 / ps の失敗が
// すべて unsupported-terminal に合流することを指摘した。e-7263 でまさにその型
// (対処が真逆の 2 理由の合流) を切り出した直後に、同じ PR で新しい合流を作っていた。
func TestUnreadableProcessIsNotCalledAnUnsupportedTerminal(t *testing.T) {
	for _, c := range []struct {
		harness string
		kind    string
		probe   string
		wantOK  bool
		wantWhy string
		why     string
	}{
		{"", "", harnessProbeUnreadable, false, JumpBlockedProcessUnreadable,
			"読めず名乗りも無い → 読めなかったと言う"},
		{"terminal", "", harnessProbeUnreadable, false, JumpBlockedProcessUnreadable,
			"下限値のままで読めなかった場合も同じ"},
		{"iterm2", "", harnessProbeUnreadable, true, "",
			"読めなくても名乗りで解決するなら飛べる (読めなかったことは問題にならない)"},
		{"kitty", "", harnessProbeUnreadable, false, JumpBlockedProcessUnreadable,
			"未対応端末 + 読めない: 読めないほうが上位 (端末表に足しても直らない)"},
		{"kitty", "", "", false, JumpBlockedUnsupportedTerminal,
			"読めたのに表に無い → 端末種別の話"},
		{"", "bg", harnessProbeUnreadable, false, JumpBlockedBackgroundSession,
			"素性が分かっているならそちらが上位 (より確定した事実)"},
	} {
		ok, why := jumpVerdictKind("", false, 100, c.harness, c.kind, c.probe)
		if ok != c.wantOK || why != c.wantWhy {
			t.Errorf("probe=%q harness=%q kind=%q → (%v,%q), want (%v,%q) — %s",
				c.probe, c.harness, c.kind, ok, why, c.wantOK, c.wantWhy, c.why)
		}
	}
}

// 一覧側が「読めなかった」を行に残すこと (配線漏れの検出)。
func TestResolveHarnessesMarksUnreadable(t *testing.T) {
	run := fakeRunner(map[int]string{10: "iTerm.app"}, nil)
	rows := []SessionOverview{
		{PID: 10, Harness: ""}, // 読める
		{PID: 99, Harness: ""}, // 居ない = 読めない
	}
	resolveHarnessesWith(run, rows)
	if rows[0].HarnessProbe != "" {
		t.Errorf("読めた行に印が付いている: %q", rows[0].HarnessProbe)
	}
	if rows[1].HarnessProbe != harnessProbeUnreadable {
		t.Errorf("読めなかった行に印が無い: %q", rows[1].HarnessProbe)
	}
	applyJumpVerdicts(rows, "")
	if rows[1].JumpBlocked != JumpBlockedProcessUnreadable {
		t.Errorf("一覧が印を判定に渡していない: %q", rows[1].JumpBlocked)
	}
}

// 一覧の適用経路も素性を見ていること (applyJumpVerdicts が渡していないと、
// 判定関数だけ直して「足した」つもりになる = 配線漏れ)。
func TestApplyJumpVerdictsPassesKind(t *testing.T) {
	rows := []SessionOverview{
		{PID: 1, Harness: "iterm2", Kind: "bg"},
		{PID: 2, Harness: "iterm2", Kind: "interactive"},
	}
	applyJumpVerdicts(rows, "")
	if rows[0].Jumpable || rows[0].JumpBlocked != JumpBlockedBackgroundSession {
		t.Errorf("一覧が素性を渡していない: jumpable=%v blocked=%q",
			rows[0].Jumpable, rows[0].JumpBlocked)
	}
	if !rows[1].Jumpable {
		t.Error("interactive の行まで落ちている")
	}
}
