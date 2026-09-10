// 動いているセッションにプロンプトを送る (ms-171)。
//
// 送れるのは **Beacon に名乗っているセッション (bclaude) だけ**。Beacon の bus を
// 通すと、その文面は相手セッションの AI に届いてターン内で読まれる。
//
// なぜ他の道具に送らないか (2026-09-10 に調べた結果):
//
//	素の Claude Code … 名前付きパイプがあるが **非公開の内部プロトコル**。
//	                   更新で黙って壊れ、「送ったのに届かない」状態になる。
//	OpenCode        … HTTP の口はあるが Basic 認証を要求し、資格情報が手元の
//	                   どこにも無い (設定・DB・serve の引数すべて確認済)。
//	Codex           … 外から入力を渡す口が見当たらない。
//
// 送れない相手に送信の入口を出さないこと。押しても何も起きない入口は、
// 使えないことに気づけない状態を作る。
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// SendResult は送った結果と、届いたかどうか。
type SendResult struct {
	EventID string `json:"event_id"`
	// 3 段の到達確認。送っただけで「届いた」と思い込まないため。
	Sent      bool `json:"sent"`
	Delivered bool `json:"delivered"`
	Opened    bool `json:"opened"`
	// Note は受け取り確認が付かなかったときの説明。
	Note string `json:"note,omitempty"`
}

// localSessionID は、このマシンの Beacon セッション識別子 (分かれば)。
// 送り主として添える。分からなくても送れる。
func localSessionID(beaconDir string) string {
	raw, err := os.ReadFile(filepath.Join(beaconDir, "session.json"))
	if err != nil {
		return ""
	}
	var rec struct {
		SessionID string `json:"session_id"`
	}
	if err := json.Unmarshal(raw, &rec); err != nil {
		return ""
	}
	return rec.SessionID
}

// SendPrompt は名乗っているセッション宛に文面を送り、届いたかを確かめる。
func (c *CloudSource) SendPrompt(recipientSessionID, text, senderSessionID string) (
	*SendResult, error) {

	if strings.TrimSpace(text) == "" {
		return nil, errors.New("送る文面が空です")
	}
	if recipientSessionID == "" {
		return nil, errors.New("宛先のセッションが分かりません")
	}
	body := map[string]any{
		"channel":           "dm",
		"sender_session_id": senderSessionID,
		"delivery":          "propose-to-ai",
		"payload": map[string]any{
			"text":                 text,
			"recipient_session_id": recipientSessionID,
		},
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest("POST",
		strings.TrimRight(c.API, "/")+"/api/projects/"+c.ProjectID+"/bus",
		bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.Token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var detail struct {
			Error string `json:"error"`
			Hint  string `json:"hint"`
		}
		json.NewDecoder(resp.Body).Decode(&detail)
		msg := detail.Error
		if detail.Hint != "" {
			msg += " (" + detail.Hint + ")"
		}
		if msg == "" {
			msg = fmt.Sprintf("応答 %d", resp.StatusCode)
		}
		return nil, errors.New("送れませんでした: " + msg)
	}
	var ev struct {
		EventID string `json:"event_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&ev); err != nil {
		return nil, err
	}
	return &SendResult{EventID: ev.EventID, Sent: true}, nil
}

// CheckReceipt は 3 段の到達確認を読む。
//
// **送れたことと届いたことは別**。相手の受信が止まっていれば、送信は成功しても
// 届かない。届いたかどうかを出さないと「送ったつもり」を作ってしまう。
func (c *CloudSource) CheckReceipt(eventID string) (*SendResult, error) {
	// 項目名は Beacon 本体 (lib/cmd_bus.py cmd_bus_status) と揃える。
	//
	// **送信済みは created_at で表す** — 行が在ること自体が「サーバが受け取った」
	// の意味で、受け取り確認の項目は無い。delivered / opened は相手が受け取った
	// ときに初めて付く。
	var ev struct {
		CreatedAt   string `json:"created_at"`
		DeliveredAt string `json:"delivered_at"`
		OpenedAt    string `json:"opened_at"`
	}
	err := c.get("/api/projects/"+c.ProjectID+"/bus/"+eventID, &ev)
	if err != nil {
		return nil, err
	}
	res := &SendResult{
		EventID:   eventID,
		Sent:      ev.CreatedAt != "",
		Delivered: ev.DeliveredAt != "",
		Opened:    ev.OpenedAt != "",
	}
	if !res.Delivered {
		res.Note = "相手がまだ受け取っていません。受信の仕組みが止まっている可能性があります"
	} else if !res.Opened {
		res.Note = "受け取られましたが、まだ読まれていません"
	}
	return res, nil
}

// waitForReceipt は少し待ってから到達を確かめる。
// 相手の受信周期は数秒なので、それを待つ。
func (c *CloudSource) waitForReceipt(eventID string) *SendResult {
	time.Sleep(4 * time.Second)
	res, err := c.CheckReceipt(eventID)
	if err != nil {
		return &SendResult{EventID: eventID, Sent: true,
			Note: "送りましたが、届いたかどうかを確かめられませんでした"}
	}
	return res
}
