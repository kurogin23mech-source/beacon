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

// --- 起動モードごとの既定 (ms-170 思想 review medium) ------------------------

func TestServerDeployedBoardsNeverSelfClose(t *testing.T) {
	// サーバに置いた盤を畳むと「しばらく放っておくと盤が死んでいる」状態になる。
	// 畳む理由 (手元で開くたびに積み上がる) が設置物には無い。
	if got := idleTimeoutFor(true, 0, false); got != 0 {
		t.Fatalf("サーバ設置の既定が畳む側になっている: %v", got)
	}
}

func TestLocalBoardsCloseByDefault(t *testing.T) {
	if got := idleTimeoutFor(false, 0, false); got != defaultIdleTimeout {
		t.Fatalf("手元の既定が %v (期待 %v)", got, defaultIdleTimeout)
	}
}

func TestAnExplicitTimeoutWinsInEitherMode(t *testing.T) {
	// 渡された値はモードに関係なく尊重する。モードで上書きすると、
	// 指定したのに効かない旗になる。
	for _, exposed := range []bool{true, false} {
		if got := idleTimeoutFor(exposed, 90*time.Second, true); got != 90*time.Second {
			t.Errorf("exposed=%v で指定値が尊重されていない: %v", exposed, got)
		}
		// 0 の明示 (= 畳まない) も指定として尊重する
		if got := idleTimeoutFor(exposed, 0, true); got != 0 {
			t.Errorf("exposed=%v で 0 の明示が尊重されていない: %v", exposed, got)
		}
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
