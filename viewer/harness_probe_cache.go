package main

import (
	"context"
	"strconv"
	"strings"
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
// で、プロセスの生涯で変わらない。
//
// **pid の再利用はキーで区別する** (e-7266)。以前は pid だけをキーにし、「寿命を
// 短く取れば再利用は起きない」という **検証していない仮定** に頼っていた
// (独立 AX レビューが指摘)。外れると、別プロセスの端末に向けたスクリプトを投げる
// —— 無反応か、最悪は別アプリの前面化になり、手がかりが何も残らない。いまは
// プロセスの開始時刻をキーに混ぜる。pid が同じでも開始時刻が違えば別プロセスなので、
// 古い答えは**引けなくなる** (消し忘れでなく、鍵が合わない)。
//
// **なぜ「読めなかった」も覚えるか**: 覚えないと、読めない pid (権限不足の他ユーザー
// のプロセス等) に対して毎回 ps を起動し続ける。待ち時間の上限 (psProbeTimeout) に
// 毎回ぶつかる形になり、いちばん遅い行がいちばん頻繁に試される。ただし寿命は成功と
// 同じにしてある —— 失敗を長く覚えると、起動直後で読めなかった行が、読めるように
// なった後も飛べないままになる。
const harnessProbeTTL = 30 * time.Second

// harnessProbeKey は pid と **そのプロセスの開始時刻** の組 (e-7266)。
//
// 開始時刻は `ps -o lstart=` の文字列をそのまま持つ。解析しないのは、要るのが
// 「同じプロセスか」の同値判定だけで、時刻としての意味は使わないから。読めない
// ときは空で、その場合は pid だけの鍵になる (= 従来どおり。読めないことを理由に
// キャッシュを捨てると、読めない環境で毎回 ps を 2 回叩くことになる)。
type harnessProbeKey struct {
	pid     int
	started string
}

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
	m   map[harnessProbeKey]harnessProbeEntry
	now func() time.Time
}

func newHarnessProbeCache(now func() time.Time) *harnessProbeCache {
	if now == nil {
		now = time.Now
	}
	return &harnessProbeCache{m: map[harnessProbeKey]harnessProbeEntry{}, now: now}
}

// processStartedWith は pid の開始時刻を読む (読めなければ空)。
//
// キャッシュの鍵に混ぜるためだけに使う。失敗を空に畳むのは、**読めないことを
// 理由に機能を止めない**ため (鍵が pid だけに退化するので、挙動は従来と同じ)。
func processStartedWith(run probeRunner, pid int) string {
	if pid <= 0 {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), psProbeTimeout)
	defer cancel()
	out, err := run(ctx, "ps", "-o", "lstart=", "-p", strconv.Itoa(pid))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// resolve は declared が不明なときだけ訊く。答えは TTL のあいだ使い回す。
//
// 返り値は resolveHarnessWith と同じ形なので、呼び出し側は差し替えるだけでよい。
func (c *harnessProbeCache) resolve(run probeRunner, pid int, declared string) (string, probeResult) {
	// 名乗りで足りているなら、キャッシュにも触らない。pid ごとに覚えるのは
	// 「プロセスに訊いた答え」だけで、名乗りは行ごとに来るもの。
	if !unknownHarness(declared) {
		return declared, probeOK
	}
	if c == nil {
		return resolveHarnessWith(run, pid, declared)
	}
	now := c.now()
	key := harnessProbeKey{pid: pid, started: processStartedWith(run, pid)}
	c.mu.Lock()
	if e, ok := c.m[key]; ok && now.Sub(e.at) < harnessProbeTTL {
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
	c.m[key] = harnessProbeEntry{harness: h, result: res, at: now}
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
func resolveHarnessesCached(run probeRunner, c *harnessProbeCache, sessions []SessionOverview) {
	for i := range sessions {
		if sessions[i].Remote || sessions[i].PID <= 0 {
			continue
		}
		h, res := c.resolve(run, sessions[i].PID, sessions[i].Harness)
		sessions[i].Harness = h
		sessions[i].HarnessProbe = res.rowMark()
	}
}
