package main

// 誰も見ていない盤を居残らせない (ms-170 e-7147)。
//
// **なぜ時間で測るのか**: 盤はサーバーなので、ブラウザのタブを閉じても盤には
// 何も届かない。「閉じた」を直接知る手段が無い。一方、画面は 5 秒ごとに
// `/api/board` を叩き続ける (page.html の `setInterval(refresh, 5000)`) ので、
// **問い合わせが止まったこと**が「もう誰も見ていない」の観測可能な代理になる。
//
// **閾値をなぜ分単位にするか**: ブラウザは背景タブの `setInterval` を間引く
// (多くの実装で 1 分に 1 回程度まで)。秒単位で切ると、別のタブを見ている間に
// 盤が落ちる。間引かれた間隔より十分長く取る必要があるので既定は 30 分。
// 画面を開いたまま席を離れても落ちない長さで、かつ何日も居残らない長さ。
//
// **落ちることの副作用を承知で選んでいる**: 閾値を過ぎて落ちた後、開いたままの
// タブは次の問い合わせで繋がらなくなる。再読み込みすれば `beacon view` を
// 開き直すまで戻らない。これは「使うたびに盤が積み上がり、端末が重くなる」
// (2026-10-25 実測: 11 本・約 207MB、最古は 2 日 23 時間) より軽い害だと判断した。
// 落ちるときは理由を言い残すので、黙って消えたようには見えない。

import (
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"
)

// defaultIdleTimeout は、問い合わせが途切れてから畳むまでの時間。
// 0 以下にすると畳まない (= 従来どおり居残る)。
const defaultIdleTimeout = 30 * time.Minute

// idleTracker は最後に問い合わせが来た時刻を覚えておく。
type idleTracker struct {
	mu   sync.Mutex
	last time.Time
}

func newIdleTracker(now time.Time) *idleTracker {
	return &idleTracker{last: now}
}

// touch は「いま誰かが見ている」を記録する。
func (t *idleTracker) touch(now time.Time) {
	t.mu.Lock()
	t.last = now
	t.mu.Unlock()
}

// idleFor は最後の問い合わせからの経過時間。
func (t *idleTracker) idleFor(now time.Time) time.Duration {
	t.mu.Lock()
	defer t.mu.Unlock()
	return now.Sub(t.last)
}

// wrapWithIdleTracking は、どの問い合わせでも時刻を更新する層を被せる。
//
// 道ごとに選り分けない。「この道は数えない」を作ると、数えない道だけを叩く
// 相手が居たときに、見られているのに畳む / 見られていないのに残る のどちらかが
// 起きる。全部数えるのが一番説明しやすい。
func wrapWithIdleTracking(h http.Handler, t *idleTracker, now func() time.Time) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.touch(now())
		h.ServeHTTP(w, r)
	})
}

// shouldClose は畳むべきかを返す (純関数、時計を渡して試験する)。
func shouldClose(idle, timeout time.Duration) bool {
	if timeout <= 0 {
		return false
	}
	return idle >= timeout
}

// watchIdle は、問い合わせが途切れたら盤を畳む見張りを始める。
//
// `timeout <= 0` のときは何もしない (= 畳まない)。`check` は確認の間隔で、
// 試験から短くできるようにしてある。
func watchIdle(t *idleTracker, timeout, check time.Duration, now func() time.Time,
	stop func(string)) {
	if timeout <= 0 {
		return
	}
	go func() {
		for {
			time.Sleep(check)
			if idle := t.idleFor(now()); shouldClose(idle, timeout) {
				stop(fmt.Sprintf(
					"%s のあいだ誰も見ていないので盤を畳みます "+
						"(また見るには beacon view を実行してください)",
					timeout))
				return
			}
		}
	}()
}

// exitIdle は見張りが畳むときの既定の後始末。理由を言い残してから終わる。
func exitIdle(reason string) {
	fmt.Fprintln(os.Stderr, reason)
	os.Exit(0)
}

// reportOrphanBoards は、控えに載っていない盤が居残っていれば伝える
// (ms-170 e-7147、受入条件 4)。
//
// 控え (portfile.go) が効くのは控えを置いた盤に対してだけ。控えの仕組みが入る
// 前に立った盤は、ランダムな口で孤児として残り続け、控えからは見えない。再利用で
// **新しい孤児は生まれなくなる**が、既に居るものは誰も教えてくれないと気づけない。
//
// 伝えるだけで、勝手に終わらせない。他のプロジェクトの盤かもしれないし、人が
// 意図して複数立てていることもある。終わらせるかは人が決める。
func reportOrphanBoards(selfPID int) {
	// 1 本は自分が使っている / 立てた盤なので、それを超えた分だけを孤児として扱う。
	others := boardProcessPIDs(selfPID)
	if len(others) <= 1 {
		return
	}
	fmt.Fprintf(os.Stderr,
		"注意: この機械で beacon-view が %d 本動いています。"+
			"いまは 1 本に畳まれるので新しくは増えませんが、"+
			"以前からの居残りがあります: %v\n", len(others), others)
	fmt.Fprintln(os.Stderr,
		"  要らなければ終了してください (例: kill "+pidList(others)+")")
}

func pidList(pids []int) string {
	out := ""
	for i, p := range pids {
		if i > 0 {
			out += " "
		}
		out += fmt.Sprint(p)
	}
	return out
}
