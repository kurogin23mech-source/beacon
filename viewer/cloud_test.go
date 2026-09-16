package main

import (
	"encoding/json"
	"testing"
	"time"
)

// 名簿行 (/api/me/sessions) の decode 契約を受信境界で pin する (ax/保守性 review #753 M1)。
//
// context_pct の JSON 名は server (SessionUpsert) ↔ Go (rosterSessionDTO → SessionRow →
// SessionOverview) に跨る cross-language 契約。carry テスト (sessions_view_test.go) は
// SessionRow→SessionOverview の leg しか見ていなかったので、**受信 unmarshal の leg**
// (server が送る JSON 名 "context_pct" を Go が field に載せられるか) をここで固定する。
// Go 側 tag を誤って変えたらこのテストが赤くなる (server rename は producer 側でしか防げ
// ないが、それは routers_projects.py の co-writer コメント + この対の存在で気付かせる)。
func TestRosterSessionDTOUnmarshalsContextPct(t *testing.T) {
	cases := []struct {
		name    string
		body    string
		wantNil bool
		wantVal int
	}{
		{"申告あり 57%", `{"session_id":"s1","context_pct":57}`, false, 57},
		{"0% は正当な値 (使いたて) — nil に化けない", `{"session_id":"s1","context_pct":0}`, false, 0},
		{"未申告はキー欠損 = nil", `{"session_id":"s1"}`, true, 0},
		{"明示 null も nil", `{"session_id":"s1","context_pct":null}`, true, 0},
	}
	for _, c := range cases {
		var dto rosterSessionDTO
		if err := json.Unmarshal([]byte(c.body), &dto); err != nil {
			t.Fatalf("%s: unmarshal 失敗: %v", c.name, err)
		}
		if c.wantNil {
			if dto.ContextPct != nil {
				t.Errorf("%s: nil を期待したが %d", c.name, *dto.ContextPct)
			}
			continue
		}
		if dto.ContextPct == nil {
			t.Fatalf("%s: non-nil を期待したが nil (JSON 名 context_pct → field の対応が壊れている)", c.name)
		}
		if *dto.ContextPct != c.wantVal {
			t.Errorf("%s: got %d, want %d", c.name, *dto.ContextPct, c.wantVal)
		}
	}
}

// 受信境界の clamp が範囲外の使用率を 0–100 に収めること (ax review #753 AX5)。
// 外部 producer が範囲外を送ってもバッジの閾値判定 (60/80%) が壊れない。nil は nil のまま。
func TestClampPct(t *testing.T) {
	ptr := func(v int) *int { return &v }
	cases := []struct {
		name string
		in   *int
		want *int
	}{
		{"nil はそのまま", nil, nil},
		{"範囲内はそのまま", ptr(57), ptr(57)},
		{"下限 0 はそのまま", ptr(0), ptr(0)},
		{"上限 100 はそのまま", ptr(100), ptr(100)},
		{"負値は 0 に", ptr(-5), ptr(0)},
		{"100 超は 100 に", ptr(150), ptr(100)},
	}
	for _, c := range cases {
		got := clampPct(c.in)
		if (got == nil) != (c.want == nil) {
			t.Errorf("%s: nil 一致せず got=%v want=%v", c.name, got, c.want)
			continue
		}
		if got != nil && *got != *c.want {
			t.Errorf("%s: got %d, want %d", c.name, *got, *c.want)
		}
	}
}

// 認証情報の期限判定。使っている最中に切れると、盤が突然読めなくなる。
func TestCredentialsExpiry(t *testing.T) {
	now := time.Date(2026, 9, 9, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name   string
		expiry int64
		want   bool
	}{
		{"まだ余裕がある", now.Add(time.Hour).Unix(), false},
		{"すでに切れている", now.Add(-time.Hour).Unix(), true},
		{"あと 10 秒で切れる (使う前に切れた扱いにする)",
			now.Add(10 * time.Second).Unix(), true},
		{"期限の記載が無い (使ってみて判断する)", 0, false},
	}
	for _, c := range cases {
		got := (&Credentials{TokenExpiry: c.expiry}).Expired(now)
		if got != c.want {
			t.Errorf("%s: Expired = %v, want %v", c.name, got, c.want)
		}
	}
}

// 保存先は beacon 本体と同じ場所。ここを違えると、beacon が入っている PC で
// 既にログイン済みなのに、また聞かれることになる。
func TestCredentialsPathMatchesBeacon(t *testing.T) {
	p := CredentialsPath()
	if p == "" {
		t.Skip("保存先を決められない環境")
	}
	for _, part := range []string{".beacon", "profiles", "default", "credentials.json"} {
		if !contains(p, part) {
			t.Errorf("保存先に %q が含まれていない: %s", part, p)
		}
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}
