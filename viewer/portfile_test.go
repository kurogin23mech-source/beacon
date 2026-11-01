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
		dir + string(filepath.Separator),  // 末尾の区切りあり
		filepath.Join(dir, "."),           // 自分自身を指す形
		filepath.Join(sub, ".."),          // 一度降りて戻る形
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
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/api/state" {
				http.NotFound(w, r)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"path": servedPath})
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
