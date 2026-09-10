package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// 空の文面や宛先なしを、サーバに投げる前に断ること。
func TestSendPromptRefusesIncompleteInput(t *testing.T) {
	c := &CloudSource{API: "http://example.invalid", ProjectID: "p"}
	if _, err := c.SendPrompt("sv-1", "   ", ""); err == nil {
		t.Error("空の文面が通ってしまった")
	}
	if _, err := c.SendPrompt("", "こんにちは", ""); err == nil {
		t.Error("宛先なしが通ってしまった")
	}
}

// 送る形が Beacon の bus の契約どおりであること。
//
// ここがズレると、サーバには受理されても相手に届かない (宛先は payload の中に
// 入れる決まり)。届かないことは送信時には分からないので、形を固定しておく。
func TestSendPromptPayloadShape(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if !strings.HasSuffix(r.URL.Path, "/api/projects/p1/bus") {
				t.Errorf("送り先の道が違う: %s", r.URL.Path)
			}
			if auth := r.Header.Get("Authorization"); auth != "Bearer tok" {
				t.Errorf("認証が付いていない: %q", auth)
			}
			json.NewDecoder(r.Body).Decode(&got)
			w.Write([]byte(`{"event_id":"ev-1"}`))
		}))
	defer srv.Close()

	c := &CloudSource{API: srv.URL, Token: "tok", ProjectID: "p1"}
	res, err := c.SendPrompt("sv-2", "これを読んでください", "sv-me")
	if err != nil {
		t.Fatal(err)
	}
	if res.EventID != "ev-1" || !res.Sent {
		t.Errorf("送った結果が拾えていない: %+v", res)
	}
	if got["channel"] != "dm" {
		t.Errorf("channel = %v", got["channel"])
	}
	if got["sender_session_id"] != "sv-me" {
		t.Errorf("送り主が入っていない: %v", got["sender_session_id"])
	}
	payload, _ := got["payload"].(map[string]any)
	if payload["recipient_session_id"] != "sv-2" {
		t.Errorf("宛先が payload に入っていない: %v", payload)
	}
	if payload["text"] != "これを読んでください" {
		t.Errorf("文面が入っていない: %v", payload["text"])
	}
}

// 受け取り確認の読み方。
//
// **行が在ること (created_at) が「送信済み」** で、受け取り確認の項目は無い。
// ここを取り違えると、送った直後に「送れていない」と表示してしまう
// (2026-09-10 に実際に起きた)。
func TestCheckReceiptStages(t *testing.T) {
	cases := []struct {
		body                      string
		sent, delivered, opened   bool
		wantNote                  bool
		why                       string
	}{
		{`{"created_at":"t"}`, true, false, false, true, "送っただけ"},
		{`{"created_at":"t","delivered_at":"t"}`, true, true, false, true, "届いたが未読"},
		{`{"created_at":"t","delivered_at":"t","opened_at":"t"}`,
			true, true, true, false, "読まれた"},
	}
	for _, c := range cases {
		srv := httptest.NewServer(http.HandlerFunc(
			func(w http.ResponseWriter, r *http.Request) {
				w.Write([]byte(c.body))
			}))
		cs := &CloudSource{API: srv.URL, Token: "t", ProjectID: "p"}
		res, err := cs.CheckReceipt("ev-1")
		srv.Close()
		if err != nil {
			t.Fatal(err)
		}
		if res.Sent != c.sent || res.Delivered != c.delivered || res.Opened != c.opened {
			t.Errorf("%s: %+v", c.why, res)
		}
		if (res.Note != "") != c.wantNote {
			t.Errorf("%s: 説明の有無が違う (%q)", c.why, res.Note)
		}
	}
}
