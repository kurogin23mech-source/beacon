package main

import (
	"os"
	"path/filepath"
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

// ミリ秒の時刻は、文字列でも数値でも読めること。
// 台帳の書き手が型を変えたときに黙って空になるのを防ぐ。
func TestMsToTime(t *testing.T) {
	// 時刻はどこで見ても同じになるよう UTC に揃える (現地時間ではない)。
	want := "2026-09-09T11:52:06Z"
	ms := int64(1788954726000)
	if got := msToTime(float64(ms)); got != want {
		t.Errorf("数値: %q, want %q", got, want)
	}
	if got := msToTime("1788954726000"); got != want {
		t.Errorf("文字列: %q, want %q", got, want)
	}
	for _, bad := range []any{nil, "", "abc", float64(0), int64(5)} {
		if got := msToTime(bad); got != "" {
			t.Errorf("読めない値 %v から %q が出た", bad, got)
		}
	}
}

// 生きていない番号を「動作中」と言わないこと。
func TestProcessAliveRejectsInvalidPid(t *testing.T) {
	if processAlive(0) || processAlive(-1) {
		t.Error("無効な番号を動作中と判定している")
	}
}

// 「そのセッションが動いているか」と「その道具が動いているか」を混ぜないこと。
//
// 道具単位の判定を全セッションに適用していた頃は、何時間も前に終わったものまで
// 稼働中に見えていた (2026-09-10 に「稼働中 · 15 時間前」で発覚)。
func TestPerSessionAndPerToolLivenessAreSeparate(t *testing.T) {
	// セッション単位で確かめられない道具は Running を立てない。
	row := LocalSessionRow{Tool: "codex", ToolRunning: true}
	if row.Running {
		t.Error("プロセス番号が分からないのに、このセッションが動いていると言っている")
	}
	if !row.ToolRunning {
		t.Error("道具が起動していることまで失われている")
	}
}

// bridge の名乗り札 (<フォルダ>/.beacon/bridges/<sid>.json) の parent_pid と
// ローカル行の PID を突き合わせて Beacon の識別子を引く経路 (e-6446)。
func TestResolveBeaconSIDs(t *testing.T) {
	dir := t.TempDir()
	bridges := filepath.Join(dir, ".beacon", "bridges")
	if err := os.MkdirAll(bridges, 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(name, body string) {
		if err := os.WriteFile(filepath.Join(bridges, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("sv-aaa.json", `{"session_id":"sv-aaa","pid":900,"parent_pid":111}`)
	write("sv-bbb.json", `{"session_id":"sv-bbb","pid":901,"parent_pid":222}`)
	// 配信ジャーナル (.delivery.json) は名乗り札ではない — 読み飛ばすこと。
	write("sv-aaa.delivery.json", `{"session_id":"sv-junk","parent_pid":111}`)
	// 壊れた札は黙って無視 (best-effort)。
	write("sv-broken.json", `{not json`)

	rows := resolveBeaconSIDs([]LocalSessionRow{
		{Tool: "claude-code", Directory: dir, PID: 111},
		{Tool: "claude-code", Directory: dir, PID: 222},
		{Tool: "claude-code", Directory: dir, PID: 333}, // 札なし → 空のまま
		{Tool: "claude-code", Directory: dir},           // PID 不明 → 触らない
	})
	if rows[0].SessionID != "sv-aaa" {
		t.Errorf("PID 111 の識別子: %q (want sv-aaa)", rows[0].SessionID)
	}
	if rows[1].SessionID != "sv-bbb" {
		t.Errorf("PID 222 の識別子: %q (want sv-bbb)", rows[1].SessionID)
	}
	if rows[2].SessionID != "" || rows[3].SessionID != "" {
		t.Error("札の無い行に識別子が付いた (推測してはいけない)")
	}
}

// .beacon が無いフォルダでは何もしない (best-effort、素のプロジェクトを壊さない)。
func TestResolveBeaconSIDsWithoutBeaconDir(t *testing.T) {
	rows := resolveBeaconSIDs([]LocalSessionRow{
		{Tool: "claude-code", Directory: t.TempDir(), PID: 111},
	})
	if rows[0].SessionID != "" {
		t.Errorf(".beacon 無しで識別子が付いた: %q", rows[0].SessionID)
	}
}
