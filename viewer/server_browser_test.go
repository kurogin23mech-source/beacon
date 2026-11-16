package main

// ブラウザを起こした子を **必ず回収する** ことの試験 (ms-170 e-7147)。
//
// 直す前は `Start()` だけで `Wait()` が無く、`open` がすぐ終わるのに回収されず
// `<defunct>` が盤 1 本につき 1 件残っていた。受入条件そのものなのに機械的な回帰
// 防止が無く、`cmd.Wait()` の行を消しても `go test` は緑のままだった (独立レビュー
// が実測)。実ブラウザを起こさずに済むよう、起動と回収を `openWith` に分けてある。

import (
	"os/exec"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestOpenWithReapsTheChild(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("`true` に相当する害のない道具が無い環境")
	}
	cmd := exec.Command("true")
	reaped := openWith(cmd)
	select {
	case <-reaped:
	case <-time.After(5 * time.Second):
		t.Fatal("子を回収していない (Wait が呼ばれていない)")
	}
	if cmd.ProcessState == nil {
		t.Fatal("ProcessState が無い = Wait を通っていない")
	}
}

func TestOpenWithSurvivesACommandThatCannotStart(t *testing.T) {
	// 起こせなくても盤は成立する。待ち続けて固まらないことを押さえる。
	cmd := exec.Command("/no/such/binary-xyz")
	select {
	case <-openWith(cmd):
	case <-time.After(5 * time.Second):
		t.Fatal("起こせない道具で固まった")
	}
}

func TestBrowserCommandPerOS(t *testing.T) {
	cases := map[string]string{
		"darwin":  "open",
		"windows": "rundll32",
		"linux":   "xdg-open",
	}
	for goos, want := range cases {
		cmd := browserCommand(goos, "http://127.0.0.1:1/")
		if !strings.Contains(cmd.Path, want) && cmd.Args[0] != want {
			t.Errorf("%s で %q を使っていない: %v", goos, want, cmd.Args)
		}
		if cmd.Args[len(cmd.Args)-1] != "http://127.0.0.1:1/" {
			t.Errorf("%s で住所を渡していない: %v", goos, cmd.Args)
		}
	}
}
