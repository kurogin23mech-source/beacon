package main

import (
	"testing"
	"time"
)

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
