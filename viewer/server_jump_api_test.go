package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

// /api/jump のエラーが **全経路 {error} JSON** で返ること (e-6429)。
// 以前は 405 だけ http.Error の text/plain で、画面の res.json() が経路によって
// 落ちた。ここでは OS に依存しない 2 経路 (405 / 403) を pin する — 405 は method
// 判定のみ、403 は expose=true で GOOS に関わらず jumpAvailable が false になるため、
// macOS のローカルでも linux の CI でも同じ結果になる。
// (jumpToTerminal 失敗ごとの status 対応は TestJumpToTerminalWithStatuses が担う。)
func TestAPIJumpErrorsAreJSON(t *testing.T) {
	assertJSONError := func(t *testing.T, rec *httptest.ResponseRecorder, wantStatus int) {
		t.Helper()
		if rec.Code != wantStatus {
			t.Errorf("status = %d, want %d", rec.Code, wantStatus)
		}
		if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
			t.Errorf("Content-Type = %q, want application/json (text/plain 混在の再発)", ct)
		}
		var body struct {
			Error string `json:"error"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body.Error == "" {
			t.Errorf("応答が {error} JSON でない: %q (err=%v)", rec.Body.String(), err)
		}
	}

	// method 違い (GET) は 405 でも JSON。
	s := &Server{Host: "127.0.0.1"}
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, httptest.NewRequest("GET", "/api/jump", nil))
	assertJSONError(t, rec, 405)

	// 外部公開中は 403 (入力を直しても無駄、を status で伝える)。
	exposed := &Server{Host: "0.0.0.0", expose: true}
	rec = httptest.NewRecorder()
	exposed.handler().ServeHTTP(rec,
		httptest.NewRequest("POST", "/api/jump", strings.NewReader(`{"pid":1}`)))
	assertJSONError(t, rec, 403)
}
