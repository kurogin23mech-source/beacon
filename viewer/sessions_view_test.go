package main

import "testing"

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
