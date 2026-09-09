package main

import (
	"testing"
	"time"
)

// 「このマシンで動いているセッション」の突き合わせ規則を固定する。
//
// ここを間違えると、別プロジェクトのセッションが混ざったり、逆に自分のものが
// 出てこなくなる。実際 2026-09-09 に「OpenCode が動いているのに出てこない」と
// 報告があり、この経路が丸ごと未接続だったことが分かった。
func TestUnderRoot(t *testing.T) {
	cases := []struct {
		dir, root string
		want      bool
		why       string
	}{
		{"C:/Users/a/proj", "C:/Users/a/proj", true, "同じ場所"},
		{"C:/Users/a/proj/sub", "C:/Users/a/proj", true, "配下のフォルダ"},
		{`C:\Users\a\proj`, "C:/Users/a/proj", true, "区切り記号の違いを吸収する"},
		{"c:/users/a/proj", "C:/Users/a/proj", true, "大文字小文字の違いを吸収する"},
		{"C:/Users/a/proj/", "C:/Users/a/proj", true, "末尾の区切りを無視する"},
		{"C:/Users/a/projX", "C:/Users/a/proj", false, "名前が前方一致なだけの別物"},
		{"C:/Users/a", "C:/Users/a/proj", false, "親は配下ではない"},
		{"", "C:/Users/a/proj", false, "場所が分からないものは含めない"},
		{"C:/Users/a/proj", "", false, "比べる相手が無ければ含めない"},
	}
	for _, c := range cases {
		if got := underRoot(c.dir, c.root); got != c.want {
			t.Errorf("underRoot(%q, %q) = %v, want %v (%s)",
				c.dir, c.root, got, c.want, c.why)
		}
	}
}

// 符号化されたフォルダ名から元の場所を戻せること。
func TestDecodeClaudeDir(t *testing.T) {
	got := decodeClaudeDir("C--Users-dolph-Documents-Projects-beacon")
	want := "C:/Users/dolph/Documents/Projects/beacon"
	if got != want {
		t.Errorf("decodeClaudeDir = %q, want %q", got, want)
	}
}

// 古いものを落とすこと。何日も前に終わったセッションを「動いている」欄に
// 並べると、いま何が起きているかが読めなくなる。
func TestLocalSessionsDropsOldOnes(t *testing.T) {
	now := time.Date(2026, 9, 9, 12, 0, 0, 0, time.UTC)
	rows := []LocalSessionRow{
		{Tool: "opencode", Directory: "C:/p",
			LastActive: now.Add(-1 * time.Hour).Format(time.RFC3339)},
		{Tool: "codex", Directory: "C:/p",
			LastActive: now.Add(-48 * time.Hour).Format(time.RFC3339)},
	}
	kept := filterAndSort(rows, "C:/p", 24*time.Hour, now)
	if len(kept) != 1 || kept[0].Tool != "opencode" {
		t.Errorf("古いものが落ちていない: %+v", kept)
	}
}

// 新しい順に並ぶこと。いま動いているものを先に見せる。
func TestLocalSessionsNewestFirst(t *testing.T) {
	now := time.Date(2026, 9, 9, 12, 0, 0, 0, time.UTC)
	rows := []LocalSessionRow{
		{Tool: "a", Directory: "C:/p",
			LastActive: now.Add(-3 * time.Hour).Format(time.RFC3339)},
		{Tool: "b", Directory: "C:/p",
			LastActive: now.Add(-1 * time.Hour).Format(time.RFC3339)},
	}
	kept := filterAndSort(rows, "C:/p", 24*time.Hour, now)
	if len(kept) != 2 || kept[0].Tool != "b" {
		t.Errorf("新しい順になっていない: %+v", kept)
	}
}
