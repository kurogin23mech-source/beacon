package main

import (
	"sync"
	"time"
)

// 導出結果の短命キャッシュ (e-7265)。
//
// **なぜ要るか**: `/api/sessions` は画面が 5 秒ごとに叩く道で、その中で名乗りが
// 無い行ぶんだけ ps を直列に起動する。独立 AX レビューが、全 44 行が未解決という
// 悪いケースを実測して 549ms を観測した。盤を開いている間それが 5 秒ごとに繰り
// 返るので、ロスターが大きい環境では無警告でレイテンシが線形に悪化する。
//
// **なぜ短命でよいか**: 答えている問いは「この pid はどの端末の中で動いているか」
// で、プロセスの生涯で変わらない。pid が再利用されたときだけ古い答えが残りうるので、
// 寿命は短く取る (再利用が起きるほどの時間は置かない)。
//
// **なぜ「読めなかった」も覚えるか**: 覚えないと、読めない pid (権限不足の他ユーザー
// のプロセス等) に対して毎回 ps を起動し続ける。待ち時間の上限 (psProbeTimeout) に
// 毎回ぶつかる形になり、いちばん遅い行がいちばん頻繁に試される。ただし寿命は成功と
// 同じにしてある —— 失敗を長く覚えると、起動直後で読めなかった行が、読めるように
// なった後も飛べないままになる。
const harnessProbeTTL = 30 * time.Second

type harnessProbeEntry struct {
	harness string
	result  probeResult
	at      time.Time
}

// harnessProbeCache は pid ごとの導出結果を TTL 付きで覚える。
//
// 盤 1 本に 1 つ持たせる (= Server の持ち物)。パッケージ変数にしないのは、試験が
// 互いの残骸を見ないようにするため。
type harnessProbeCache struct {
	mu  sync.Mutex
	m   map[int]harnessProbeEntry
	now func() time.Time
}

func newHarnessProbeCache(now func() time.Time) *harnessProbeCache {
	if now == nil {
		now = time.Now
	}
	return &harnessProbeCache{m: map[int]harnessProbeEntry{}, now: now}
}

// resolve は declared が不明なときだけ訊く。答えは TTL のあいだ使い回す。
//
// 返り値は resolveHarnessWith と同じ形なので、呼び出し側は差し替えるだけでよい。
func (c *harnessProbeCache) resolve(run commandRunner, pid int, declared string) (string, probeResult) {
	// 名乗りで足りているなら、キャッシュにも触らない。pid ごとに覚えるのは
	// 「プロセスに訊いた答え」だけで、名乗りは行ごとに来るもの。
	if !unknownHarness(declared) {
		return declared, probeOK
	}
	if c == nil {
		return resolveHarnessWith(run, pid, declared)
	}
	now := c.now()
	c.mu.Lock()
	if e, ok := c.m[pid]; ok && now.Sub(e.at) < harnessProbeTTL {
		c.mu.Unlock()
		if e.result == probeOK {
			return e.harness, probeOK
		}
		return declared, e.result
	}
	c.mu.Unlock()

	// ロックの外で訊く。ps は外部プロセスなので、握ったまま待つと同じ盤の
	// 別の問い合わせまで止める。
	h, res := resolveHarnessWith(run, pid, declared)

	c.mu.Lock()
	c.m[pid] = harnessProbeEntry{harness: h, result: res, at: now}
	// 古い行は訊くついでに捨てる。盤は畳まなくなった (e-7264) ので、掃除しないと
	// 終わったセッションの pid が延々と溜まる。
	for k, e := range c.m {
		if now.Sub(e.at) >= harnessProbeTTL {
			delete(c.m, k)
		}
	}
	c.mu.Unlock()
	return h, res
}

// resolveHarnessesCached は一覧の各行を、キャッシュを通して埋める。
//
// resolveHarnessesWith と同じ選り分け (別マシンでなく pid が在る行だけ) をする。
// 選り分けを 2 箇所に書かないため、判定は resolve に任せて並び方だけここが持つ。
func resolveHarnessesCached(run commandRunner, c *harnessProbeCache, sessions []SessionOverview) {
	for i := range sessions {
		if sessions[i].Remote || sessions[i].PID <= 0 {
			continue
		}
		h, res := c.resolve(run, sessions[i].PID, sessions[i].Harness)
		sessions[i].Harness = h
		if res == probeUnreadable {
			sessions[i].HarnessProbe = harnessProbeUnreadable
		}
	}
}
