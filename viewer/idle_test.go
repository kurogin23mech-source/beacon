package main

// 誰も見ていない盤を畳む判定の試験 (ms-170 e-7147)。
//
// 時計を渡す形にしてあるので、実時間を待たずに境界を測れる。実機での確認
// (閾値 12 秒で実際に畳まれ、理由を言い残す / 問い合わせ続ける間は畳まれない) は
// 別途行っている —— ここで固定するのは**判定の規則**。

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestShouldCloseOnlyPastTheThreshold(t *testing.T) {
	cases := []struct {
		name    string
		idle    time.Duration
		timeout time.Duration
		want    bool
	}{
		{"閾値より手前では畳まない", 9 * time.Minute, 10 * time.Minute, false},
		{"ちょうど閾値で畳む", 10 * time.Minute, 10 * time.Minute, true},
		{"閾値を超えたら畳む", 11 * time.Minute, 10 * time.Minute, true},
		// 0 は「畳まない」。これが効かないと --idle-timeout 0 で止められない。
		{"0 は畳まない", 100 * time.Hour, 0, false},
		{"負でも畳まない", 100 * time.Hour, -1, false},
	}
	for _, c := range cases {
		if got := shouldClose(c.idle, c.timeout); got != c.want {
			t.Errorf("%s: shouldClose(%v, %v) = %v, want %v",
				c.name, c.idle, c.timeout, got, c.want)
		}
	}
}

func TestTouchResetsTheIdleClock(t *testing.T) {
	base := time.Unix(1000, 0)
	tr := newIdleTracker(base)
	if got := tr.idleFor(base.Add(30 * time.Second)); got != 30*time.Second {
		t.Fatalf("経過時間が合わない: %v", got)
	}
	tr.touch(base.Add(30 * time.Second))
	if got := tr.idleFor(base.Add(31 * time.Second)); got != time.Second {
		t.Fatalf("問い合わせで時計が戻っていない: %v", got)
	}
}

func TestEveryRequestCountsAsBeingWatched(t *testing.T) {
	// 道で選り分けない。「この道は数えない」を作ると、その道だけを叩く相手が
	// 居たときに、見られているのに畳む形が生まれる。
	base := time.Unix(1000, 0)
	now := base
	tr := newIdleTracker(base)
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	h := wrapWithIdleTracking(inner, tr, func() time.Time { return now })

	for _, path := range []string{"/", "/api/board", "/api/state", "/nope"} {
		now = now.Add(time.Minute)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", path, nil))
		if got := tr.idleFor(now); got != 0 {
			t.Fatalf("%s を数えていない (経過 %v)", path, got)
		}
	}
}

func TestWatchIdleDoesNothingWhenDisabled(t *testing.T) {
	// 畳まない設定のときは見張りを立てない。立てて即 stop を呼ぶと、
	// --idle-timeout 0 を指定した人の盤が落ちる。
	tr := newIdleTracker(time.Unix(0, 0))
	called := make(chan string, 1)
	watchIdle(tr, 0, time.Millisecond, func() time.Time { return time.Unix(1<<30, 0) },
		func(reason string) { called <- reason })
	select {
	case reason := <-called:
		t.Fatalf("畳まない設定なのに畳もうとした: %s", reason)
	case <-time.After(50 * time.Millisecond):
	}
}

func TestWatchIdleClosesWithAReason(t *testing.T) {
	// 黙って消えると「盤が壊れた」と読まれる。理由を言い残すことまで含めて
	// 1 つの振る舞い。
	tr := newIdleTracker(time.Unix(0, 0))
	got := make(chan string, 1)
	watchIdle(tr, time.Minute, time.Millisecond,
		func() time.Time { return time.Unix(3600, 0) },
		func(reason string) { got <- reason })
	select {
	case reason := <-got:
		if reason == "" {
			t.Fatal("理由が空")
		}
		if !strings.Contains(reason, "beacon view") {
			t.Fatalf("また見る方法が書かれていない: %s", reason)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("閾値を超えても畳まなかった")
	}
}

// --- 既定 (モード非依存、e-7264) ------------------------

func TestNoBoardSelfClosesByDefault(t *testing.T) {
	// e-7264: どちらのモードでも既定では畳まない。
	//
	// 以前は「設置した盤は畳まない / 手元の盤は 30 分で畳む」と分けていた。
	// 手元を畳む理由は積み上がりだが、それは席 (reuse_key) による再利用で
	// 閉じている。一方で畳むことの害は実地で重く、席を離れて戻ると盤が死んで
	// おり、開いたままのタブは無言で繋がらなくなっていた (2026-11-02 に
	// 「運用室が全然動かない」として報告された)。
	//
	// e-7265: モードという入力自体が無くなった (引数から exposed を落とした)。
	// 「モードで既定が変わる」考え方が消えたことを、引数が無いことで言い切る。
	if got := idleTimeoutFor(0, false); got != 0 {
		t.Errorf("既定が畳む側になっている: %v "+
			"(畳むのは --idle-timeout を明示したときだけ)", got)
	}
	if defaultIdleTimeout != 0 {
		t.Errorf("既定の定数が 0 でない: %v", defaultIdleTimeout)
	}
}

func TestAnExplicitTimeoutIsRespected(t *testing.T) {
	// 渡された値は必ず尊重する。既定で上書きすると、指定したのに効かない旗になる。
	if got := idleTimeoutFor(90*time.Second, true); got != 90*time.Second {
		t.Errorf("指定値が尊重されていない: %v", got)
	}
	// 0 の明示 (= 畳まない) も指定として尊重する。既定と同じ値だが、
	// 「指定した」事実は案内の文面に出る (下の表で区別している)。
	if got := idleTimeoutFor(0, true); got != 0 {
		t.Errorf("0 の明示が尊重されていない: %v", got)
	}
}

func TestTheCloseMessageDoesNotAskARemoteViewerToRunACommand(t *testing.T) {
	// 遠隔の閲覧者に `beacon view` を実行させることはできない。文言は
	// 「この機械で」と限定し、設置した盤では畳まない前提に揃える。
	got := make(chan string, 1)
	tr := newIdleTracker(time.Unix(0, 0))
	watchIdle(tr, time.Minute, time.Millisecond,
		func() time.Time { return time.Unix(3600, 0) },
		func(reason string) { got <- reason })
	select {
	case reason := <-got:
		if !strings.Contains(reason, "この機械で") {
			t.Fatalf("どこで実行するのかが書かれていない: %s", reason)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("畳まなかった")
	}
}

// --- 起動時に名乗る 1 行 (ms-170 AX review medium) ---------------------------

func TestIdleAnnouncementAlwaysSaysSomething(t *testing.T) {
	// **3 通りの「畳まない」すべてで名乗る**。以前は --idle-timeout 0 を
	// 明示したときだけ無言で、意思を持って指定した場合こそ確かめたいのに
	// そこが名乗らない形だった。
	// **期待値は完全一致** (e-7265)。以前は部分一致で、既定のケースの期待が
	// 「しません」だけだった。3 通りの「畳まない」はすべて「しません」を含むので、
	// 既定の分岐が消えて別の文面が返っても緑のまま通った —— 守ると称する性質
	// (どの理由で畳まないのか) を識別できていなかった (独立保守性レビュー)。
	cases := []struct {
		name      string
		effective time.Duration
		requested bool
		want      string
	}{
		{"30 分を明示", 30 * time.Minute, true,
			"  無操作での終了: 30m0s 誰も見に来なければ畳みます"},
		{"既定 (畳まない)", 0, false, "  無操作での終了: しません (既定)"},
		{"0 を明示", 0, true, "  無操作での終了: しません (指定により)"},
		{"負を明示", -5 * time.Minute, true,
			"  無操作での終了: しません (指定により)"},
		{"指定された値", 2 * time.Hour, true,
			"  無操作での終了: 2h0m0s 誰も見に来なければ畳みます"},
	}
	for _, c := range cases {
		got := idleAnnouncement(c.effective, c.requested)
		if got == "" {
			t.Errorf("%s: 何も名乗らなかった", c.name)
			continue
		}
		if got != c.want {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
		if !strings.Contains(got, "無操作での終了") {
			t.Errorf("%s: 見出しが無い: %q", c.name, got)
		}
	}
}
