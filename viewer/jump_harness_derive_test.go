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
	for _, c := range []struct{ pid int; want string }{
		{10, "iTerm.app"}, {11, "Apple_Terminal"}, {12, ""},
		{99, ""}, // プロセスが居ない
		{0, ""},  // pid 無効 — ps を叩かずに空
		{-1, ""},
	} {
		if got := termProgramForPIDWith(run, c.pid); got != c.want {
			t.Errorf("termProgramForPIDWith(pid=%d) = %q, want %q", c.pid, got, c.want)
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
		if got := resolveHarnessWith(run, c.pid, c.declared); got != c.want {
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
	for _, c := range []struct{ pid int; wantApp string }{
		{10, "iTerm2"}, {11, "Terminal.app"},
	} {
		h := resolveHarnessWith(run, c.pid, "")
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
		{PID: 10, Harness: ""},                      // 訊く
		{PID: 20, Harness: "terminal"},              // 訊く
		{PID: 30, Harness: "iterm2"},                // 訊かない (特定済み)
		{PID: 40, Harness: "", Remote: true},        // 訊かない (別マシン)
		{PID: 0, Harness: ""},                       // 訊かない (pid 無し)
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
