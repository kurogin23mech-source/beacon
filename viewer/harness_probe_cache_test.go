package main

import (
	"errors"
	"testing"
	"time"
)

// 導出の短命キャッシュ (e-7265)。
//
// 独立 AX レビューが「44 行すべて未解決の悪いケースで 549ms、しかも画面は 5 秒ごとに
// 叩く」を実測して指摘した。ここで固定するのは 3 つ: 2 度目は訊かない / 寿命を越えたら
// 訊き直す / 名乗りで足りている行は触らない。

func countingRunner(env map[int]string, calls *int) commandRunner {
	return func(name string, args ...string) ([]byte, error) {
		*calls++
		pid := 0
		if len(args) > 0 {
			_, _ = fmtSscan(args[len(args)-1], &pid)
		}
		v, ok := env[pid]
		if !ok {
			return nil, errors.New("no such process")
		}
		return []byte("/bin/claude TERM_PROGRAM=" + v + "\n"), nil
	}
}

// fmtSscan は fmt.Sscanf の薄い別名 (import を 1 つに留めるため)。
func fmtSscan(s string, out *int) (int, error) {
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0, errors.New("not a number")
		}
		n = n*10 + int(r-'0')
	}
	*out = n
	return 1, nil
}

func TestProbeCacheAsksOnceWithinTTL(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{10: "iTerm.app"}, &calls)
	now := time.Now()
	c := newHarnessProbeCache(func() time.Time { return now })

	for i := 0; i < 5; i++ {
		h, res := c.resolve(run, 10, "")
		if h != "terminal:iTerm.app" || res != probeOK {
			t.Fatalf("%d 回目で答えが変わった: %q %v", i+1, h, res)
		}
	}
	if calls != 1 {
		t.Errorf("ps を %d 回叩いた (寿命の内は 1 回のはず)", calls)
	}
}

func TestProbeCacheRefreshesAfterTTL(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{10: "iTerm.app"}, &calls)
	now := time.Now()
	c := newHarnessProbeCache(func() time.Time { return now })

	c.resolve(run, 10, "")
	now = now.Add(harnessProbeTTL + time.Second)
	c.resolve(run, 10, "")
	if calls != 2 {
		t.Errorf("ps を %d 回叩いた (寿命を越えたら訊き直すはず)", calls)
	}
}

// 「読めなかった」も覚える。覚えないと、読めない pid にだけ毎回 ps を起動し続け、
// いちばん遅い行がいちばん頻繁に試されることになる。
func TestProbeCacheRemembersFailuresToo(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{}, &calls) // どの pid も読めない
	now := time.Now()
	c := newHarnessProbeCache(func() time.Time { return now })

	for i := 0; i < 4; i++ {
		h, res := c.resolve(run, 77, "terminal")
		if res != probeUnreadable {
			t.Fatalf("%d 回目: 読めなかったはず (%v)", i+1, res)
		}
		if h != "terminal" {
			t.Fatalf("%d 回目: 名乗りのまま返すはず (%q)", i+1, h)
		}
	}
	if calls != 1 {
		t.Errorf("ps を %d 回叩いた (失敗も寿命の内は覚えるはず)", calls)
	}
}

// 名乗りで足りている行はキャッシュにも ps にも触らない。
func TestProbeCacheSkipsRowsThatDeclaredATerminal(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{10: "iTerm.app"}, &calls)
	c := newHarnessProbeCache(nil)
	h, res := c.resolve(run, 10, "iterm2")
	if h != "iterm2" || res != probeOK {
		t.Errorf("名乗りを上書きした: %q %v", h, res)
	}
	if calls != 0 {
		t.Errorf("ps を %d 回叩いた (訊く必要が無い行のはず)", calls)
	}
}

// 一覧経路でもキャッシュが効き、選り分け (別マシン / pid 無し) は変わらないこと。
func TestResolveHarnessesCachedKeepsTheSameSelection(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{10: "iTerm.app", 20: "iTerm.app"}, &calls)
	c := newHarnessProbeCache(nil)
	rows := []SessionOverview{
		{PID: 10, Harness: ""},
		{PID: 10, Harness: ""},               // 同じ pid が 2 行 (1 回しか訊かない)
		{PID: 30, Harness: "iterm2"},         // 名乗り済 → 訊かない
		{PID: 40, Harness: "", Remote: true}, // 別マシン → 訊かない
		{PID: 0, Harness: ""},                // pid 無し → 訊かない
	}
	resolveHarnessesCached(run, c, rows)
	if rows[0].Harness != "terminal:iTerm.app" || rows[1].Harness != "terminal:iTerm.app" {
		t.Errorf("同じ pid の 2 行が埋まっていない: %q / %q", rows[0].Harness, rows[1].Harness)
	}
	if rows[3].Harness != "" || rows[4].Harness != "" {
		t.Errorf("別マシン / pid 無しを埋めた: %q / %q", rows[3].Harness, rows[4].Harness)
	}
	if calls != 1 {
		t.Errorf("ps を %d 回叩いた (同じ pid は 1 回のはず)", calls)
	}
}

// 上限そのもの: 打ち切る実行器が、返らないコマンドを psProbeTimeout で諦めること。
func TestBoundedRunnerGivesUp(t *testing.T) {
	if psProbeTimeout > 5*time.Second {
		t.Fatalf("上限が長すぎる (%v) — 5 秒ごとの一覧取得を塞ぐ", psProbeTimeout)
	}
	start := time.Now()
	_, err := boundedRunner("sleep", "30")
	elapsed := time.Since(start)
	if err == nil {
		t.Error("返らないコマンドで成功を返した (打ち切っていない)")
	}
	if elapsed > psProbeTimeout*3 {
		t.Errorf("%v 待った (上限 %v で諦めるはず)", elapsed, psProbeTimeout)
	}
}
