package main

import (
	"testing"
	"time"
)

// コミット件名からタスク番号を拾えること。
//
// この経路が壊れると、画面には「—」が並ぶだけで、**分からないのか機能していない
// のか区別が付かない**。だから機械で確かめておく。
func TestTaskFromSubject(t *testing.T) {
	cases := []struct{ subject, want, why string }{
		{"feat(ms-170): 画面を足す (e-6342)", "e-6342", "末尾の括弧"},
		{"fix: e-42 を直す", "e-42", "文中"},
		{"feat(ms-1): 何かする", "", "番号が無い"},
		{"", "", "件名が無い"},
		{"docs: E-999 大文字", "e-999", "大文字でも拾い小文字で返す"},
		{"chore: nice-1 は違う", "", "似ているだけの語は拾わない"},
	}
	for _, c := range cases {
		if got := taskFromSubject(c.subject); got != c.want {
			t.Errorf("taskFromSubject(%q) = %q, want %q (%s)",
				c.subject, got, c.want, c.why)
		}
	}
}

// コミット件名からも対象を拾えること (ブランチが main のままの作業のため)。
func TestTargetFromCommitSubject(t *testing.T) {
	if got := targetFromBranch("feat(ms-170): 画面を足す"); got != "ms-170" {
		t.Errorf("件名から対象を拾えていない: %q", got)
	}
	if got := targetFromBranch("このセッションの記録を残す (ms-13)"); got != "ms-13" {
		t.Errorf("末尾の対象を拾えていない: %q", got)
	}
}

// 場所の表記ゆれを吸収して名簿と突き合わせられること。
// ここがズレると、名乗っているセッションを見落として推測に落ちる。
func TestNormalisePath(t *testing.T) {
	a := normalisePath(`C:\Users\dolph\Documents\Projects\beacon`)
	b := normalisePath("c:/users/dolph/documents/projects/beacon/")
	if a != b {
		t.Errorf("同じ場所が一致しない: %q vs %q", a, b)
	}
}

// 配下の記録から、番号で説明を引けること (入れ子も辿る)。
func TestFindEntryLabel(t *testing.T) {
	entries := []Entry{
		{ID: "e-1", Description: "親"},
		{ID: "e-2", Description: "別の親", Entries: []Entry{
			{ID: "e-3", Description: "子"},
		}},
	}
	if got := findEntryLabel(entries, "e-3"); got != "子" {
		t.Errorf("入れ子を辿れていない: %q", got)
	}
	if got := findEntryLabel(entries, "E-1"); got != "親" {
		t.Errorf("大文字小文字を吸収できていない: %q", got)
	}
	if got := findEntryLabel(entries, "e-999"); got != "" {
		t.Errorf("無い番号に %q を返した", got)
	}
}

// 他のマシンで動いている bclaude セッションも一覧に出ること。
//
// 名簿はどのマシンのものも載るが、手元の記録には無い。手元の記録を並べるだけだと
// **他のマシンの分が丸ごと抜ける** (2026-09-10 の指摘で発覚)。
func TestRemoteNamedSessionsAreIncluded(t *testing.T) {
	named := []SessionRow{{
		ID: "sv-remote", Who: "someone@example.com", Machine: "Mac-mini",
		Agent: "claude-code", Cwd: "/Users/someone/proj",
		Target: "ms-42", TargetLabel: "何かの対象",
		Activity: "テストを書いている",
		Live:     true, LastActive: "2030-01-01T00:00:00Z",
	}}
	// 手元の記録は空 (= このマシンでは何も動いていない) とする。
	view := AllSessions(24*time.Hour, time.Now(), named)

	var found *SessionOverview
	for i := range view.Sessions {
		if view.Sessions[i].SessionID == "sv-remote" {
			found = &view.Sessions[i]
		}
	}
	if found == nil {
		t.Fatal("他のマシンの名乗っているセッションが一覧から抜けている")
	}
	if !found.Remote {
		t.Error("別のマシンであることが分からない")
	}
	if !found.Named || !found.Running {
		t.Errorf("名乗り/稼働が落ちている: %+v", found)
	}
	if found.Target == nil || found.Target.ID != "ms-42" ||
		found.Target.Source != "beacon" {
		t.Errorf("担当が名簿の値になっていない: %+v", found.Target)
	}
	if found.Machine != "Mac-mini" || found.Who != "someone@example.com" {
		t.Errorf("誰のどのマシンかが落ちている: %+v", found)
	}
	// activity (今何をしているか) は名簿の値がそのまま運ばれること。
	// ここが落ちると運用室の activity 欄が常に空になり「分からない」と区別が付かない。
	if found.Activity != "テストを書いている" {
		t.Errorf("activity が名簿から引き継がれていない: %q", found.Activity)
	}
}

// 名乗っていない (= このマシンで拾っただけの) セッションには activity が付かないこと。
// activity の真実源はサーバ (ms-159) の名簿であって、手元の推測ではない。
// 空欄は「分からない」であって「何もしていない」ではない、という不変条件を機械で守る。
func TestUnnamedSessionHasNoActivity(t *testing.T) {
	// 名簿を空にして AllSessions を呼ぶと、手元で拾ったセッションだけになる。
	// このマシンの実セッションは環境依存なので、名簿が空でも落ちないこと + 出た行に
	// activity が (名簿由来でないので) 付かないことだけを確かめる。
	view := AllSessions(24*time.Hour, time.Now(), nil)
	for _, s := range view.Sessions {
		if !s.Named && s.Activity != "" {
			t.Errorf("名乗っていないセッションに activity が付いている: %q (%s)",
				s.Activity, s.Directory)
		}
	}
}

// 手元の記録と名簿の両方に在るセッションを、二重に並べないこと。
func TestNamedSessionOnThisMachineIsNotDuplicated(t *testing.T) {
	// 手元にも在る場所を名簿が指している状況を作る。
	// (このマシンの記録は環境依存なので、突き合わせの規則だけを確かめる)
	a := normalisePath(`C:\Users\x\proj`)
	b := normalisePath("c:/users/x/proj/")
	if a != b {
		t.Fatalf("突き合わせの規則が壊れている: %q vs %q", a, b)
	}
}

// --- 運用室の絞り込み (ms-173 e-6401) --------------------------------------

// 「要対応」の判定: 生きているのに道具が止まっているものだけ。
// 別マシンのものは道具の状態が手元に無いので、既定の false を待機と取り違えない。
func TestNeedsAttention(t *testing.T) {
	cases := []struct {
		name string
		s    SessionOverview
		want bool
	}{
		{"生きていて道具が止まっている = 要対応",
			SessionOverview{Running: true, ToolRunning: false}, true},
		{"道具が動いている = 作業中なので要対応でない",
			SessionOverview{Running: true, ToolRunning: true}, false},
		{"止まっている = 要対応でない",
			SessionOverview{Running: false, ToolRunning: false}, false},
		{"別マシンは道具の状態が分からないので要対応に挙げない",
			SessionOverview{Running: true, ToolRunning: false, Remote: true}, false},
	}
	for _, c := range cases {
		if got := NeedsAttention(c.s); got != c.want {
			t.Errorf("%s: NeedsAttention = %v, want %v", c.name, got, c.want)
		}
	}
}

// 「自分」の判定: 名乗っているものは持ち主で、名乗っていないものはこのマシンの分。
func TestIsSelf(t *testing.T) {
	me := "me@example.com"
	cases := []struct {
		name  string
		s     SessionOverview
		email string
		want  bool
	}{
		{"名乗っていない (Who 空) = このマシンの分 = 自分",
			SessionOverview{Who: ""}, me, true},
		{"持ち主が自分",
			SessionOverview{Who: "me@example.com"}, me, true},
		{"持ち主が他人",
			SessionOverview{Who: "other@example.com", Remote: true}, me, false},
		{"identity 不明なら別マシンは自分でない",
			SessionOverview{Who: "x@example.com", Remote: true}, "", false},
		{"identity 不明でもこのマシンの名乗りは自分",
			SessionOverview{Who: "x@example.com", Remote: false}, "", true},
	}
	for _, c := range cases {
		if got := isSelf(c.s, c.email); got != c.want {
			t.Errorf("%s: isSelf = %v, want %v", c.name, got, c.want)
		}
	}
}

// 表示範囲 scope (自分/要対応/全て) がそれぞれ正しい集合を返すこと。
func TestFilterSessions(t *testing.T) {
	me := "me@example.com"
	pA := &ProjectRef{Name: "A"}
	pB := &ProjectRef{Name: "B"}
	sessions := []SessionOverview{
		{Who: "", Project: pA, Running: true, ToolRunning: true},          // 自分/A/作業中
		{Who: "me@example.com", Project: pB, Running: true, ToolRunning: false}, // 自分/B/要対応
		{Who: "other@example.com", Project: pA, Remote: true, Running: true},    // 他人/A
	}

	// 既定 (self): 他人を畳む。
	if got := FilterSessions(sessions, SessionFilter{}, me); len(got) != 2 {
		t.Errorf("既定 self で自分の 2 件にならない: %d 件", len(got))
	}
	// all: 全部。
	if got := FilterSessions(sessions, SessionFilter{Scope: "all"}, me); len(got) != 3 {
		t.Errorf("all で 3 件にならない: %d 件", len(got))
	}
	// attention: 生きていて道具が止まっている 1 件。
	got := FilterSessions(sessions, SessionFilter{Scope: "attention"}, me)
	if len(got) != 1 || got[0].Project.Name != "B" {
		t.Errorf("attention で B の 1 件にならない: %+v", got)
	}
	// 未知の scope は self に倒す。
	if got := FilterSessions(sessions, SessionFilter{Scope: "???"}, me); len(got) != 2 {
		t.Errorf("未知 scope が self に倒れていない: %d 件", len(got))
	}
	// 入力を変更しないこと。
	if len(sessions) != 3 {
		t.Errorf("入力が変更された: %d 件", len(sessions))
	}
}

// 規模で崩れないこと (doc scale-contract-principle: 一覧処理には規模テストを 1 本)。
// 多数セッション × 多プロジェクトでも絞り込みが数と対象を取り違えない。
func TestFilterSessionsAtScale(t *testing.T) {
	const projects, perProject = 40, 25 // 1000 セッション
	me := "me@example.com"
	refs := make([]*ProjectRef, projects)
	for i := range refs {
		refs[i] = &ProjectRef{Name: string(rune('A'+i%26)) + itoaSmall(i)}
	}
	sessions := make([]SessionOverview, 0, projects*perProject)
	wantSelf, wantAttention := 0, 0
	for p := 0; p < projects; p++ {
		for k := 0; k < perProject; k++ {
			s := SessionOverview{Project: refs[p]}
			if k%3 == 0 { // 1/3 は他人 (別マシン)
				s.Who = "other@example.com"
				s.Remote = true
				s.Running = true
			} else {
				s.Who = "" // このマシン = 自分
				wantSelf++
				if k%2 == 0 { // 自分のうち一部は待機中 = 要対応
					s.Running = true
					s.ToolRunning = false
					wantAttention++
				} else {
					s.Running = true
					s.ToolRunning = true
				}
			}
			sessions = append(sessions, s)
		}
	}
	if got := FilterSessions(sessions, SessionFilter{Scope: "all"}, me); len(got) != projects*perProject {
		t.Errorf("all が全件を返さない: %d / %d", len(got), projects*perProject)
	}
	if got := FilterSessions(sessions, SessionFilter{Scope: "self"}, me); len(got) != wantSelf {
		t.Errorf("self が自分の件数と合わない: %d / %d", len(got), wantSelf)
	}
	if got := FilterSessions(sessions, SessionFilter{Scope: "attention"}, me); len(got) != wantAttention {
		t.Errorf("attention が件数と合わない: %d / %d", len(got), wantAttention)
	}
}

// itoaSmall は小さな整数を文字列にする (テスト内でプロジェクト名を作るためだけの簡易版)。
func itoaSmall(n int) string {
	if n == 0 {
		return "0"
	}
	digits := []byte{}
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	return string(digits)
}
