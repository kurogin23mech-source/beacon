package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

// 導出の短命キャッシュ (e-7265)。
//
// 独立 AX レビューが「44 行すべて未解決の悪いケースで 549ms、しかも画面は 5 秒ごとに
// 叩く」を実測して指摘した。ここで固定するのは 3 つ: 2 度目は訊かない / 寿命を越えたら
// 訊き直す / 名乗りで足りている行は触らない。

func countingRunner(env map[int]string, calls *int) probeRunner {
	return func(_ context.Context, name string, args ...string) ([]byte, error) {
		*calls++
		pid := 0
		if len(args) > 0 {
			_, _ = fmtSscan(args[len(args)-1], &pid)
		}
		v, ok := env[pid]
		if !ok {
			return nil, errors.New("no such process")
		}
		// 開始時刻の問い合わせ (鍵に混ざる、e-7266)。pid ごとに安定した値を返す。
		if len(args) > 0 && args[0] == "-o" && len(args) > 1 && args[1] == "lstart=" {
			return []byte("Mon Nov  3 09:00:00 2026\n"), nil
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
	// 1 回の導出で 2 本叩く (開始時刻 + 環境)。寿命の内は導出が 1 回なので、
	// 開始時刻の問い合わせだけが毎回走る = 5 回 + 環境 1 回 = 6。
	if calls > 6 {
		t.Errorf("ps を %d 回叩いた (寿命の内は導出 1 回のはず)", calls)
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
	// 導出 2 回 (= 環境 2 本) + 開始時刻 2 本。
	if calls != 4 {
		t.Errorf("ps を %d 回叩いた (寿命を越えたら訊き直すはず = 導出 2 回)", calls)
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
	// 読めない pid は開始時刻も読めない (鍵は pid だけに退化) ので、
	// 導出は 1 回に収まる。開始時刻の試行は毎回走る。
	if calls > 8 {
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
	// pid 10 の導出 1 回 (環境 1 + 開始時刻 2: 2 行ぶん)。
	if calls > 4 {
		t.Errorf("ps を %d 回叩いた (同じ pid の導出は 1 回のはず)", calls)
	}
}

// 上限が **プローブ自身** に在ること (e-7266)。
//
// 以前は「上限つきの実行器」を呼び出し元が渡す形で、ジャンプ経路だけ無界だった。
// いまは termProgramForPIDWith が context を張るので、**どの実行器を渡しても**
// 上限が効く。ここでは返らない実行器を渡して、それが打ち切られることを見る。
func TestProbeBoundsItselfWhateverRunnerIsPassed(t *testing.T) {
	if psProbeTimeout > 5*time.Second {
		t.Fatalf("上限が長すぎる (%v) — 5 秒ごとの一覧取得を塞ぐ", psProbeTimeout)
	}
	// ctx が切れるまで返らない実行器。上限が効いていなければ永久に止まる。
	never := func(ctx context.Context, _ string, _ ...string) ([]byte, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	start := time.Now()
	v, res := termProgramForPIDWith(never, 123)
	elapsed := time.Since(start)

	if res != probeUnreadable || v != "" {
		t.Errorf("打ち切りが「読めなかった」に落ちていない: %q %v", v, res)
	}
	// **下限も見る** (独立保守性レビュー): 即座に失敗する実行器でも通る試験では、
	// 上限が実際に待ったことを確かめられない。
	if elapsed < psProbeTimeout/2 {
		t.Errorf("%v で返った — 上限を待たずに失敗している (上限 %v)", elapsed, psProbeTimeout)
	}
	if elapsed > psProbeTimeout*3 {
		t.Errorf("%v 待った (上限 %v で諦めるはず)", elapsed, psProbeTimeout)
	}
}

// 期限切れの行が、別 pid の問い合わせのついでに捨てられること (e-7266)。
//
// 掃除は書き込み経路に相乗りしている。消しても条件を変えても全テストが緑だった
// ので、header の「掃除しないと溜まる」という主張を機械で守る (独立保守性レビュー)。
func TestProbeCacheSweepsExpiredEntries(t *testing.T) {
	calls := 0
	run := countingRunner(map[int]string{10: "iTerm.app", 20: "iTerm.app"}, &calls)
	now := time.Now()
	c := newHarnessProbeCache(func() time.Time { return now })

	c.resolve(run, 10, "")
	if len(c.m) != 1 {
		t.Fatalf("1 件入っていない: %d", len(c.m))
	}
	now = now.Add(harnessProbeTTL + time.Second) // pid 10 が期限切れ
	c.resolve(run, 20, "")                       // 別 pid を訊く = 書き込み経路
	if len(c.m) != 1 {
		t.Errorf("期限切れが残っている (%d 件)。掃除が効いていない", len(c.m))
	}
	found := false
	for k := range c.m {
		if k.pid == 20 {
			found = true
		}
	}
	if !found {
		t.Error("いま訊いた pid が入っていない")
	}
}

// pid が再利用されたら古い答えを引かないこと (e-7266)。
//
// 以前は pid だけをキーにし、「寿命を短く取れば再利用は起きない」という検証して
// いない仮定に頼っていた (独立 AX レビューが指摘)。外れると別プロセスの端末に
// 向けたスクリプトを投げる — 無反応か、最悪は別アプリの前面化で、手がかりが残らない。
func TestProbeCacheDistinguishesReusedPIDs(t *testing.T) {
	started := "Mon Nov  3 09:00:00 2026"
	term := "iTerm.app"
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		if len(args) > 1 && args[1] == "lstart=" {
			return []byte(started + "\n"), nil
		}
		return []byte("/bin/claude TERM_PROGRAM=" + term + "\n"), nil
	}
	now := time.Now()
	c := newHarnessProbeCache(func() time.Time { return now })

	h, _ := c.resolve(run, 10, "")
	if h != "terminal:iTerm.app" {
		t.Fatalf("1 回目: %q", h)
	}

	// 同じ pid が **別プロセス** に再利用された (開始時刻が変わる)。
	// 寿命の内だが、古い答えを引いてはいけない。
	started = "Mon Nov  3 09:00:30 2026"
	term = "Apple_Terminal"
	h, _ = c.resolve(run, 10, "")
	if h != "terminal:Apple_Terminal" {
		t.Errorf("pid 再利用後に古い答えを引いた: %q "+
			"(開始時刻を鍵に混ぜていないと、別プロセスの端末を狙う)", h)
	}
}

// 開始時刻が読めない環境では、鍵が pid だけに退化して従来どおり動くこと。
// 読めないことを理由に機能 (キャッシュ) を止めない。
func TestProbeCacheFallsBackWhenStartTimeIsUnreadable(t *testing.T) {
	probes := 0
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		if len(args) > 1 && args[1] == "lstart=" {
			return nil, errors.New("cannot read start time")
		}
		probes++
		return []byte("/bin/claude TERM_PROGRAM=iTerm.app\n"), nil
	}
	c := newHarnessProbeCache(nil)
	for i := 0; i < 3; i++ {
		if h, _ := c.resolve(run, 10, ""); h != "terminal:iTerm.app" {
			t.Fatalf("%d 回目: %q", i+1, h)
		}
	}
	if probes != 1 {
		t.Errorf("環境の問い合わせが %d 回 (開始時刻が読めなくてもキャッシュは効くはず)", probes)
	}
}
