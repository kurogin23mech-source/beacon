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
		Live: true, LastActive: "2030-01-01T00:00:00Z",
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
