package main

// 盤を二重に立てないための控えと、居残りの検知の試験 (ms-170 e-7147)。
//
// 守っているもの:
//   - 控えの置き場所が **道ごとに別**で、同じ道の別の綴り (相対 / symlink) では
//     同じ場所になること (別になると再利用が効かず、また積み上がる)
//   - 生きているかの判定が **番号・応答・配っている道の 3 つすべて**を見ること。
//     どれか 1 つでも抜けると、別プロジェクトの盤や別人の口を自分の盤と
//     見間違える
//   - 居残りの数え方が自分自身を含めないこと (含めると常に 1 本居ることになる)

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestPortfilePathDiffersPerProject(t *testing.T) {
	a := portfilePath("/tmp/project-a")
	b := portfilePath("/tmp/project-b")
	if a == b {
		t.Fatalf("別のプロジェクトで同じ控えを使っている: %s", a)
	}
}

func TestPortfilePathIsStableAcrossSpellings(t *testing.T) {
	// 同じ場所を違う綴りで指しても同じ控えに辿り着く。別になると、呼び方が違う
	// だけで盤が増える。
	//
	// **作業場所を変えて試さない**: 最初は os.Chdir で相対パスを試したが、
	// chdir はテストの実行プロセス全体に効くので、同じ束の他のテストが
	// main.go / page.html を見つけられなくなった (実際に 5 件落ちた)。
	// 綴りの違いは chdir 無しでも作れる。
	dir := t.TempDir()
	sub := filepath.Join(dir, "sub")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("用意できない: %v", err)
	}
	plain := portfilePath(dir)
	for _, spelling := range []string{
		dir + string(filepath.Separator), // 末尾の区切りあり
		filepath.Join(dir, "."),          // 自分自身を指す形
		filepath.Join(sub, ".."),         // 一度降りて戻る形
	} {
		if got := portfilePath(spelling); got != plain {
			t.Errorf("同じ場所で控えが分かれた:\n  素=%s\n  %q=%s",
				plain, spelling, got)
		}
	}
}

func TestReadPortfileRejectsGarbage(t *testing.T) {
	dir := t.TempDir()
	// 無い
	if got := readPortfile(filepath.Join(dir, "nope.json")); got != nil {
		t.Fatalf("無い控えを読んでしまった: %+v", got)
	}
	// 壊れている
	broken := filepath.Join(dir, "broken.json")
	_ = os.WriteFile(broken, []byte("{not json"), 0o600)
	if got := readPortfile(broken); got != nil {
		t.Fatalf("壊れた控えを読んでしまった: %+v", got)
	}
	// 口が無い (= 何も分からない控え)
	empty := filepath.Join(dir, "empty.json")
	_ = os.WriteFile(empty, []byte(`{"pid":1}`), 0o600)
	if got := readPortfile(empty); got != nil {
		t.Fatalf("口の無い控えを使えると判断した: %+v", got)
	}
}

func TestWriteThenReadPortfileRoundTrips(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rec.json")
	want := boardRecord{Port: 7377, PID: os.Getpid(), Host: "127.0.0.1",
		Path: "/tmp/x", StartedAt: "2026-11-02T00:00:00Z"}
	if err := writePortfile(path, want); err != nil {
		t.Fatalf("置けなかった: %v", err)
	}
	got := readPortfile(path)
	if got == nil || got.Port != want.Port || got.Path != want.Path {
		t.Fatalf("読み戻せていない: %+v", got)
	}
}

// _boardServer は `/api/state` だけを返す偽の盤。生きているかの判定が
// 「応答」と「配っている道」をどう見るかを試すために使う。
func _boardServer(t *testing.T, servedPath string) (*httptest.Server, int, string) {
	return _boardServerWith(t, map[string]any{"path": servedPath})
}

// _boardServerWith は `/api/state` が返す中身を丸ごと指定できる版。
// 席 (reuse_key) を名乗る / 名乗らない の両方を試すために要る。
func _boardServerWith(t *testing.T, state map[string]any) (*httptest.Server, int, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/api/state" {
				http.NotFound(w, r)
				return
			}
			_ = json.NewEncoder(w).Encode(state)
		}))
	t.Cleanup(srv.Close)
	// httptest の住所から口を取り出す (127.0.0.1:NNNN)。
	parts := strings.Split(strings.TrimPrefix(srv.URL, "http://"), ":")
	port, err := strconv.Atoi(parts[len(parts)-1])
	if err != nil {
		t.Fatalf("口を読めない: %s", srv.URL)
	}
	return srv, port, parts[0]
}

func TestLiveBoardURLAcceptsTheSameProject(t *testing.T) {
	dir := t.TempDir()
	_, port, host := _boardServer(t, filepath.Join(dir, ".beacon"))
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: dir}
	if got := liveBoardURL(rec, dir); got == "" {
		t.Fatal("同じプロジェクトの生きた盤を使えないと判断した")
	}
}

func TestLiveBoardURLRejectsAnotherProject(t *testing.T) {
	// 同じ機械で別のプロジェクトの盤が動いているのは普通のこと。配っている道を
	// 見ないと、見たい場所と違う盤に繋いでしまう。
	other := t.TempDir()
	want := t.TempDir()
	_, port, host := _boardServer(t, filepath.Join(other, ".beacon"))
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: want}
	if got := liveBoardURL(rec, want); got != "" {
		t.Fatalf("別プロジェクトの盤を自分の盤と見間違えた: %s", got)
	}
}

func TestLiveBoardURLRejectsADeadPID(t *testing.T) {
	// 番号が死んでいれば、口を叩く前に諦める (安い順に見る)。
	_, port, host := _boardServer(t, "/whatever/.beacon")
	rec := &boardRecord{Port: port, PID: 1 << 30, Host: host, Path: "/whatever"}
	if got := liveBoardURL(rec, "/whatever"); got != "" {
		t.Fatalf("死んだ番号の控えを使うと判断した: %s", got)
	}
}

func TestLiveBoardURLRejectsASilentPort(t *testing.T) {
	// 番号が生きていても、その口が応えないなら使えない (控えが古い場合)。
	rec := &boardRecord{Port: 1, PID: os.Getpid(), Host: "127.0.0.1", Path: "/x"}
	if got := liveBoardURL(rec, "/x"); got != "" {
		t.Fatalf("応えない口を使うと判断した: %s", got)
	}
}

func TestLiveBoardURLWithoutARecord(t *testing.T) {
	if got := liveBoardURL(nil, "/x"); got != "" {
		t.Fatalf("控えが無いのに使えると判断した: %s", got)
	}
}

func TestSameBoardPathMatchesBeaconDirAndItsParent(t *testing.T) {
	dir := t.TempDir()
	beacon := filepath.Join(dir, ".beacon")
	if !sameBoardPath(beacon, dir) {
		t.Fatal(".beacon とその親を別物と判断した")
	}
	if !sameBoardPath(dir, beacon) {
		t.Fatal("逆向きで別物と判断した")
	}
}

func TestSameBoardPathTreatsEmptyAsUnknown(t *testing.T) {
	// 空同士を一致にすると、データの無い場所で開いた盤を別の場所の盤と
	// 同一視してしまう。
	if sameBoardPath("", "") {
		t.Fatal("どちらも不明なのに一致と判断した")
	}
	if sameBoardPath("/x", "") || sameBoardPath("", "/x") {
		t.Fatal("片方が不明なのに一致と判断した")
	}
}

func TestParseBoardPIDsExcludesSelf(t *testing.T) {
	ps := "100 /usr/bin/beacon-view-darwin-arm64\n" +
		"200 beacon-view\n" +
		"300 beacon-view.exe\n" +
		"400 some-other-process\n"
	got := parseBoardPIDs(ps, 200)
	want := []int{100, 300}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("居残りの数え方が合わない: got=%v want=%v", got, want)
	}
}

func TestParseBoardPIDsIgnoresJunkLines(t *testing.T) {
	ps := "\n  PID COMM\nnotanumber beacon-view\n500 beacon-view\n"
	got := parseBoardPIDs(ps, 0)
	if fmt.Sprint(got) != fmt.Sprint([]int{500}) {
		t.Fatalf("見出しや壊れた行を数えた: %v", got)
	}
}

func TestResolvePathAgreesForExistingAndMissingSiblings(t *testing.T) {
	// **同じ場所が、実在するかどうかで別物に見えてはいけない。**
	//
	// macOS の一時置き場は symlink (`/var/folders` → `/private/var/...`)。素朴に
	// EvalSymlinks を呼ぶと、実在する道だけが解決され、まだ無い道は元の綴りで
	// 残る。盤が配っている `.beacon` は実在し、こちらが組み立てた道は実在しない
	// ことがあるので、そこで判定が外れて盤がもう 1 本立つ (= 直そうとしている
	// 「使うたびに増える」に戻る)。実際にこの試験が最初に捕まえた形。
	dir := t.TempDir()
	missing := filepath.Join(dir, "not-created-yet")
	if got, want := filepath.Dir(resolvePath(missing)), resolvePath(dir); got != want {
		t.Fatalf("実在する親と、実在しない子の親が食い違う:\n  子の親=%s\n  親=%s",
			got, want)
	}
}

func TestSameBoardPathMatchesWhenBeaconDirIsMissing(t *testing.T) {
	// 盤が配っている `.beacon` がまだ無い場合でも、親との対応は崩れない。
	dir := t.TempDir()
	beacon := filepath.Join(dir, ".beacon") // 作らない
	if !sameBoardPath(beacon, dir) {
		t.Fatal(".beacon が未作成だと親と別物と判断した")
	}
}

func TestResolvePathLeavesAWhollyMissingTreeAbsolute(t *testing.T) {
	// 根まで遡っても解決できない道は、絶対化だけして返す (落ちない)。
	got := resolvePath(filepath.Join(string(filepath.Separator),
		"no-such-root-xyz", "a", "b"))
	if !filepath.IsAbs(got) {
		t.Fatalf("絶対の道になっていない: %s", got)
	}
}

// --- 再利用の判定 (e-7147 保守性 review high: main() 内だと試験が通らない) ----

func _noRec(string) *boardRecord { return nil }

func TestDecideReuseHonoursNoReuse(t *testing.T) {
	// `--no-reuse` を無効化する変異で緑のままだった箇所。ここで固定する。
	called := false
	live := func(*boardRecord, string) string { called = true; return "http://x/" }
	got := decideReuse(true, false, "/p", "/pf", _noRec, live)
	if got.URL != "" {
		t.Fatalf("--no-reuse を無視して再利用した: %+v", got)
	}
	if called {
		t.Fatal("--no-reuse なのに盤を探しに行った (無駄な問い合わせ)")
	}
	if got.Reason == "" {
		t.Fatal("なぜ使わないのかを言っていない")
	}
}

func TestDecideReuseHonoursAnExplicitPort(t *testing.T) {
	// 口を指定するのは「そこに立てたい」という意思表示。
	live := func(*boardRecord, string) string { return "http://x/" }
	got := decideReuse(false, true, "/p", "/pf", _noRec, live)
	if got.URL != "" {
		t.Fatalf("--port 明示を無視して再利用した: %+v", got)
	}
	if !strings.Contains(got.Reason, "--port") {
		t.Fatalf("理由が --port を指していない: %s", got.Reason)
	}
}

func TestDecideReuseUsesALiveBoard(t *testing.T) {
	live := func(*boardRecord, string) string { return "http://127.0.0.1:7377/" }
	got := decideReuse(false, false, "/p", "/pf", _noRec, live)
	if got.URL != "http://127.0.0.1:7377/" {
		t.Fatalf("生きた盤を使わなかった: %+v", got)
	}
	if got.Reason != "" {
		t.Fatalf("使うのに理由が付いている: %s", got.Reason)
	}
}

func TestDecideReuseFallsThroughWhenNoBoardIsUsable(t *testing.T) {
	live := func(*boardRecord, string) string { return "" }
	got := decideReuse(false, false, "/p", "/pf", _noRec, live)
	if got.URL != "" {
		t.Fatalf("使えない盤を使うと判断した: %+v", got)
	}
	if got.Reason == "" {
		t.Fatal("なぜ使わないのかを言っていない")
	}
}

func TestDecideReusePassesTheProjectKeyThrough(t *testing.T) {
	// 道を取り違えると別プロジェクトの盤に繋ぐので、何を渡したかを押さえる。
	var sawKey, sawPortfile string
	rec := func(p string) *boardRecord { sawPortfile = p; return &boardRecord{Port: 1} }
	live := func(_ *boardRecord, key string) string { sawKey = key; return "" }
	decideReuse(false, false, "/project", "/tmp/note.json", rec, live)
	if sawKey != "/project" {
		t.Fatalf("プロジェクトの道が渡っていない: %q", sawKey)
	}
	if sawPortfile != "/tmp/note.json" {
		t.Fatalf("控えの場所が渡っていない: %q", sawPortfile)
	}
}

// --- 旗が実際に渡されたかの判定 ------------------------------------------

func TestFlagWasSetSeesAnExplicitDefaultValue(t *testing.T) {
	// **値比較では近似できない**ことの固定 (保守性 review medium)。
	// 既定値と同じ値を明示的に渡した人も「そこに立てたい」と言っている。
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	port := fs.Int("port", defaultPort, "")
	if err := fs.Parse([]string{"-port", strconv.Itoa(defaultPort)}); err != nil {
		t.Fatalf("parse できない: %v", err)
	}
	if *port != defaultPort {
		t.Fatalf("前提が崩れた: %d", *port)
	}
	if !flagWasSet(fs.Visit, "port") {
		t.Fatal("既定値と同じ値を明示したのに『渡されていない』と判断した")
	}
}

func TestFlagWasSetIsFalseWhenOmitted(t *testing.T) {
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	fs.Int("port", defaultPort, "")
	if err := fs.Parse([]string{}); err != nil {
		t.Fatalf("parse できない: %v", err)
	}
	if flagWasSet(fs.Visit, "port") {
		t.Fatal("渡していないのに『渡された』と判断した")
	}
}

// --- 控えを上書きしてよいかの判定 (e-7147 AX review high) --------------------

func TestShouldNotClobberALiveBoardsRecord(t *testing.T) {
	// **この PR の目的そのものを守る試験**。以前は無条件に書いていたので、
	// --no-reuse の盤が生きた盤の控えを上書きし、その盤が再利用から永久に
	// 見えなくなっていた (= 「使うたびに増える」が戻る)。検証手順自身が
	// --no-reuse を使うので、確かめるたびに本番の盤を孤児にしていた。
	existing := &boardRecord{Port: 19001, PID: 4242, Path: "/p"}
	live := func(*boardRecord, string) string { return "http://127.0.0.1:19001/" }
	ok, reason := shouldWritePortfile(existing, "/p", 9999, live)
	if ok {
		t.Fatal("生きた盤の控えを上書きすると判断した")
	}
	if reason == "" {
		t.Fatal("なぜ触らないのかを言っていない")
	}
	if !strings.Contains(reason, "4242") {
		t.Fatalf("どの盤が席を取っているか言っていない: %s", reason)
	}
}

func TestShouldWriteOverADeadRecord(t *testing.T) {
	existing := &boardRecord{Port: 19001, PID: 4242, Path: "/p"}
	dead := func(*boardRecord, string) string { return "" }
	if ok, _ := shouldWritePortfile(existing, "/p", 9999, dead); !ok {
		t.Fatal("死んだ控えを書き換えられないと判断した (席が空かない)")
	}
}

func TestShouldWriteOverOwnRecord(t *testing.T) {
	// 自分の控えは更新できないと、口が変わったときに古い番号が残る。
	existing := &boardRecord{Port: 19001, PID: 777, Path: "/p"}
	live := func(*boardRecord, string) string { return "http://127.0.0.1:19001/" }
	if ok, _ := shouldWritePortfile(existing, "/p", 777, live); !ok {
		t.Fatal("自分の控えを更新できないと判断した")
	}
}

func TestShouldWriteWhenThereIsNoRecord(t *testing.T) {
	live := func(*boardRecord, string) string { return "http://x/" }
	if ok, _ := shouldWritePortfile(nil, "/p", 1, live); !ok {
		t.Fatal("控えが無いのに書けないと判断した")
	}
}

// --- 控えに載っている生きた盤を集める (居残りの判別に使う) ------------------

func TestTrackedBoardPIDsCollectsLiveRecordsOnly(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, rec boardRecord) {
		if err := writePortfile(filepath.Join(dir, name), rec); err != nil {
			t.Fatalf("置けない: %v", err)
		}
	}
	write("beacon-view-aaa.json", boardRecord{Port: 1, PID: 101, Path: "/a"})
	write("beacon-view-bbb.json", boardRecord{Port: 2, PID: 202, Path: "/b"})
	write("unrelated.json", boardRecord{Port: 3, PID: 303, Path: "/c"})
	// 101 だけ生きている扱いにする
	live := func(rec *boardRecord, _ string) string {
		if rec.PID == 101 {
			return "http://x/"
		}
		return ""
	}
	got := trackedBoardPIDs(dir, live)
	if _, ok := got[101]; !ok {
		t.Fatalf("生きた控えを拾えていない: %v", got)
	}
	if _, ok := got[202]; ok {
		t.Fatalf("死んだ控えを拾った: %v", got)
	}
	if _, ok := got[303]; ok {
		t.Fatalf("beacon-view- 以外の名前まで読んだ: %v", got)
	}
}

func TestTrackedBoardPIDsSurvivesAMissingDirectory(t *testing.T) {
	got := trackedBoardPIDs(filepath.Join(t.TempDir(), "nope"),
		func(*boardRecord, string) string { return "http://x/" })
	if len(got) != 0 {
		t.Fatalf("無いディレクトリで何か返した: %v", got)
	}
}

// --- 取得元を切り替えた盤でも席が動かない (ms-170 思想 review medium) --------

func TestLiveBoardURLUsesTheSeatNotTheDataSource(t *testing.T) {
	// **クラウドの取得元に切り替えた盤**を模す: `path` は空 (src==nil のため)
	// だが、席 (reuse_key) は起動時の道のまま。これを「どの席でもない」と読むと、
	// その盤は再利用されず・控えを奪われ・居残りとして kill を勧められる ——
	// この MS が閉じた「使うたびに増える」が、クラウドに向けた盤にだけ残る。
	dir := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{
		"path": "", "reuse_key": dir,
	})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: dir}
	if got := liveBoardURL(rec, dir); got == "" {
		t.Fatal("取得元を切り替えた盤を『別の席』と判断した")
	}
}

func TestLiveBoardURLStillRejectsAnotherSeat(t *testing.T) {
	// 席で見るようにしても、別プロジェクトの盤は弾く (緩めていない)。
	mine := t.TempDir()
	other := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{
		"path": "", "reuse_key": other,
	})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: mine}
	if got := liveBoardURL(rec, mine); got != "" {
		t.Fatalf("別の席の盤を自分の席と見間違えた: %s", got)
	}
}

func TestLiveBoardURLFallsBackToPathForOlderBoards(t *testing.T) {
	// 席を名乗らない古い盤は `path` で突き合わせる (急に再利用できなくならない)。
	dir := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{
		"path": filepath.Join(dir, ".beacon"),
	})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: dir}
	if got := liveBoardURL(rec, dir); got == "" {
		t.Fatal("席を名乗らない古い盤を再利用できないと判断した")
	}
}

func TestSeatIsReportedOverHTTPByTheRealServer(t *testing.T) {
	// **本物の `handler()` を HTTP で叩いて確かめる** (ms-170 保守性 review high)。
	//
	// 最初は `&Server{ReuseKey: "x"}` に代入して読み戻すだけの試験を書いていた。
	// 代入した値が読めるかしか見ていないので、`main` が席を渡す 1 行を消しても、
	// `/api/state` が空を返すようにしても緑のまま通った (独立レビューが両方を
	// 個別に変異させて実証)。この PR が機能する根拠そのものが未検証だった。
	dir := t.TempDir()
	srv, err := NewServer(nil, loopbackHost, 0, false, dir)
	if err != nil {
		t.Fatalf("盤を立てられない: %v", err)
	}
	t.Cleanup(func() { _ = srv.Close() })

	ts := httptest.NewServer(srv.handler())
	t.Cleanup(ts.Close)
	resp, err := http.Get(ts.URL + "/api/state")
	if err != nil {
		t.Fatalf("/api/state を叩けない: %v", err)
	}
	defer resp.Body.Close()
	var state struct {
		ReuseKey string `json:"reuse_key"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		t.Fatalf("応答を読めない: %v", err)
	}
	if state.ReuseKey != dir {
		t.Fatalf("席を名乗っていない: got %q want %q", state.ReuseKey, dir)
	}
}

func TestSeatCannotBeChangedAfterTheBoardIsUp(t *testing.T) {
	// 席は起動時に決めて以後変えない。**型で止める** (ms-170 保守性 review high)。
	//
	// 以前は公開の可変フィールドで、約束はコメントだけだった。独立レビューが
	// `/api/cloud/open` の直後に 1 行足して席を消し、全テスト緑のままこの PR が
	// 直したバグを再現させた。隣に `s.src = nil` / `s.cloud = c` という
	// 「取得元切り替え時にリセットする」形が在るので、席も消す類推は自然に起きる。
	//
	// この試験は「読む口しか無い」ことを押さえる。書く口が復活したら、
	// ここではなく **コンパイルが** 落ちる (それが狙い) ので、ここでは席が
	// 構築時の値を保つことだけを確かめる。
	dir := t.TempDir()
	srv, err := NewServer(nil, loopbackHost, 0, false, dir)
	if err != nil {
		t.Fatalf("盤を立てられない: %v", err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	if srv.ReuseKey() != dir {
		t.Fatalf("席が構築時の値と違う: %q", srv.ReuseKey())
	}
	// 取得元を切り替える経路を通しても席は動かない (/api/cloud/open 相当)。
	srv.src = nil
	if srv.ReuseKey() != dir {
		t.Fatalf("取得元を外したら席が動いた: %q", srv.ReuseKey())
	}
}

// --- 席と配っている道が食い違う盤は使わない (ms-170 AX review high) ----------

func TestLiveBoardURLRefusesWhenTheSeatAndTheServedPathDisagree(t *testing.T) {
	// `/api/open` は動いたまま **ローカルの別プロジェクトに切り替える**既存の
	// 経路。席は起動時に決めて動かないので、切り替えた後は「席 = projA /
	// 配っている道 = projB」になる。席だけを信じると、projA を求めた相手に
	// projB を配っている盤を成功扱いで渡す (独立レビューが実機で再現した)。
	//
	// 変更前は `path` 比較でこれを検出できていたので、席の無条件優先は回帰だった。
	projA := t.TempDir()
	projB := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{
		"reuse_key": projA,                           // 起動時の席
		"path":      filepath.Join(projB, ".beacon"), // いま配っているのは別物
	})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: projA}
	if got := liveBoardURL(rec, projA); got != "" {
		t.Fatalf("別プロジェクトを配っている盤を渡した: %s", got)
	}
}

func TestLiveBoardURLAcceptsWhenBothAgree(t *testing.T) {
	// 両方在って一致していれば使う (締めすぎて再利用が死んでいないことの確認)。
	dir := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{
		"reuse_key": dir,
		"path":      filepath.Join(dir, ".beacon"),
	})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: dir}
	if got := liveBoardURL(rec, dir); got == "" {
		t.Fatal("席と道が一致している盤を使わなかった")
	}
}

func TestLiveBoardURLRefusesABoardThatNamesNeither(t *testing.T) {
	// どちらも名乗らない盤は、どの席のものか分からない。
	dir := t.TempDir()
	_, port, host := _boardServerWith(t, map[string]any{})
	rec := &boardRecord{Port: port, PID: os.Getpid(), Host: host, Path: dir}
	if got := liveBoardURL(rec, dir); got != "" {
		t.Fatalf("何も名乗らない盤を使うと判断した: %s", got)
	}
}
