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
// **既定では畳まない (e-7264 で反転)**。以前は手元で開いた盤を 30 分で畳んで
// いた。理由は「使うたびに盤が積み上がり、端末が重くなる」(2026-10-25 実測:
// 11 本・約 207MB、最古は 2 日 23 時間) で、それは実在の問題だった。
//
// だが**同じ e-7147 で席 (reuse_key) による再利用も入っており、積み上がりの
// 主因はそちらで閉じている**。同じプロジェクトで何度 `beacon view` を叩いても
// 盤は増えない。時間で畳むのは、再利用が解いた問題に対する二重の対策だった。
//
// 一方で畳むことの害は実地で重かった: 席を離れて戻ると盤が死んでおり、開いたまま
// のタブは無言で繋がらなくなる。運用室は「並列で動くセッションを見失わないための
// 常設の面」なので、見ようとした時に居ないのは機能そのものの否定になる。しかも
// タブ側には畳まれたことを知る経路が無いため、利用者には「運用室が壊れている」と
// しか見えない (2026-11-02 に実際そう報告された)。
//
// **残る積み上がりは「別プロジェクトの盤」だけ**。席はプロジェクトのパスなので、
// 別プロジェクトから開くと別の盤が立つ。運用室自体はプロジェクトを跨いで全部を
// 並べるので、普段は 1 本で足りる。畳みたい人は `--idle-timeout` / 
// `BEACON_VIEW_IDLE_TIMEOUT` で選べる (口は残してある)。

import (
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"
)

// defaultIdleTimeout は、問い合わせが途切れてから畳むまでの時間。
// **0 = 畳まない (既定)。** 畳みたいときだけ `--idle-timeout` で明示する。
const defaultIdleTimeout = 0

// idleTimeoutFor は、その起動モードでの既定を返す。
//
// **どちらのモードでも畳まない (e-7264)。** 以前は「設置した盤は畳まない / 手元の
// 盤は 30 分で畳む」とモードで既定を分けていた。分けた理由は手元の積み上がりだが、
// それは席 (reuse_key) による再利用で閉じている (idle.go 冒頭)。手元でも畳まない
// ことにしたので、この関数はモードで分岐しなくなった。
//
// **関数を残してあるのは、分岐が無いことを 1 箇所で言い切るため。** モードごとの
// 既定という考え方自体を消したのに呼び出し側が自分で 0 を書くと、次に誰かが
// 「手元だけ畳もう」と思ったとき 2 箇所に分かれる。ここが唯一の決め所。
//
// 利用者が `--idle-timeout` を渡したときは、その値をどのモードでも尊重する。
func idleTimeoutFor(exposed bool, requested time.Duration, wasRequested bool) time.Duration {
	if wasRequested {
		return requested
	}
	return defaultIdleTimeout
}

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
				// 開き直し方は **この盤を動かしている人** に向けて書く。
				// 遠隔の閲覧者に `beacon view` を実行させることはできないので、
				// 設置した盤では畳まない (idleTimeoutFor) 前提の文言にしてある。
				stop(fmt.Sprintf(
					"%s のあいだ誰も見ていないので盤を畳みます "+
						"(開き直すには、この機械で beacon view を実行してください)",
					timeout))
				return
			}
		}
	}()
}

// idleAnnouncement は、起動時に名乗る 1 行を組み立てる (純関数)。
//
// **実効値だけで決める** (ms-170 AX review medium): 以前は「モードが旗の既定を
// 上書きしたか」で出すかどうかを決めていた。そのため `--idle-timeout 0` を
// **明示した**ときだけ無言になった —— 意思を持って指定した場合こそ、効いたことを
// 確かめたいのに、そこが唯一名乗らない形だった。出力を grep して意図が通ったかを
// 見る側は「旗が効かなかった」と誤読する。
//
// 理由 (なぜ畳まないのか) は飾りとして添えるだけで、**出すかどうかの条件にしない**。
func idleAnnouncement(effective time.Duration, exposed, wasRequested bool) string {
	if effective > 0 {
		return fmt.Sprintf("  無操作での終了: %s 誰も見に来なければ畳みます", effective)
	}
	switch {
	case wasRequested:
		return "  無操作での終了: しません (指定により)"
	case exposed:
		return "  無操作での終了: しません (サーバ設置のため)"
	default:
		return "  無操作での終了: しません"
	}
}

// exitIdle は見張りが畳むときの既定の後始末。理由を言い残してから終わる。
func exitIdle(reason string) {
	fmt.Fprintln(os.Stderr, reason)
	os.Exit(0)
}

// reportOrphanBoards は、**どの控えにも載っていない**盤が居残っていれば伝える
// (ms-170 e-7147、受入条件 4)。
//
// 控えが効くのは控えを置いた盤に対してだけ。控えの仕組みが入る前に立った盤は
// ランダムな口で残り続け、控えからは見えない。再利用で **新しい居残りは生まれ
// なくなる**が、既に居るものは誰も教えてくれないと気づけない。
//
// **生きている盤を「居残り」と呼ばない** (e-7147 AX review misleading): 以前は
// beacon-view のプロセスを数えるだけで、控えに載っている他プロジェクトの盤まで
// 「以前からの居残り」と呼び、`kill` を添えて勧めていた。手元で 2 つの
// プロジェクトを開くのは普通のことなので、普段使いで destructive な助言が出る。
// しかも上書きの穴 (同 review high) と重なると、**いま開こうとした盤を殺せ**と
// 言いうる。だから控えを総なめして、載っている生きた盤は除いてから数える。
//
// 伝えるだけで、勝手に終わらせない。人が意図して複数立てていることもある。
func reportOrphanBoards(selfPID int) {
	running := boardProcessPIDs(selfPID)
	if len(running) == 0 {
		return
	}
	tracked := trackedBoardPIDs(os.TempDir(), liveBoardURL)
	var orphans []int
	others := 0
	for _, pid := range running {
		if _, ok := tracked[pid]; ok {
			others++
			continue
		}
		orphans = append(orphans, pid)
	}
	if others > 0 {
		fmt.Fprintf(os.Stderr,
			"ℹ 他のプロジェクトの盤が %d 件開いています (控えに載っているので"+
				"そのプロジェクトからは再利用されます)\n", others)
	}
	if len(orphans) == 0 {
		return
	}
	fmt.Fprintf(os.Stderr,
		"注意: どの控えにも載っていない beacon-view が %d 件あります: %v\n",
		len(orphans), orphans)
	fmt.Fprintln(os.Stderr,
		"  控えの仕組みが入る前に立った盤と思われます。要らなければ終了して"+
			"ください (例: kill "+pidList(orphans)+")")
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
