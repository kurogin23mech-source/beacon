// クラウドから盤を読む (ms-170 e-6364)。
//
// 「Beacon が入っていない PC でも盤を見たい」場合、その PC に .beacon は無いのが
// 普通なので、**クラウドに繋いで見る経路が本線**になる。
//
// 認証は beacon 本体と同じ仕組みに乗る。バイナリが合図の符号を発行し、利用者が
// ブラウザで承認し、こちらが受け取って保存する。保存先も beacon 本体と同じ場所に
// するので、beacon が入っている PC では **既にログイン済みならそのまま使える**。
//
// 読み取り専用。クラウドのデータを一切書き換えない。
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DefaultAPI は Beacon のクラウドの住所。
const DefaultAPI = "https://beacon-ai.dev"

// Credentials は保存しておく認証情報。beacon 本体が書くものと同じ形にしてある。
type Credentials struct {
	Email       string `json:"email"`
	Token       string `json:"token"`
	TokenExpiry int64  `json:"token_expiry"`
	TokenType   string `json:"token_type"`
	WebAuth     bool   `json:"web_auth"`
}

// Expired は期限切れかどうか。少し手前で切れた扱いにして、使っている最中に
// 切れるのを避ける。
func (c *Credentials) Expired(now time.Time) bool {
	if c.TokenExpiry == 0 {
		return false // 期限の記載が無いものは、使ってみて判断する
	}
	return now.Add(60 * time.Second).Unix() > c.TokenExpiry
}

// CredentialsPath は認証情報の置き場所。beacon 本体と同じ場所を使う。
func CredentialsPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".beacon", "profiles", "default", "credentials.json")
}

// LoadCredentials は保存済みの認証情報を読む。無ければ nil を返す (異常ではない)。
func LoadCredentials() *Credentials {
	path := CredentialsPath()
	if path == "" {
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var c Credentials
	if err := json.Unmarshal(raw, &c); err != nil || c.Token == "" {
		return nil
	}
	return &c
}

// SaveCredentials は認証情報を保存する。次に起動したときログインし直さずに済む。
func SaveCredentials(c *Credentials) error {
	path := CredentialsPath()
	if path == "" {
		return errors.New("保存先を決められませんでした")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	// 本人以外が読めない置き方にする (認証情報なので)。
	return os.WriteFile(path, raw, 0o600)
}

// --- ログイン --------------------------------------------------------------

// LoginStart はログインの合図を発行する。返した URL を利用者がブラウザで開き、
// 承認すると LoginPoll で受け取れるようになる。
type LoginStart struct {
	Code      string `json:"code"`
	URL       string `json:"url"`
	ExpiresIn int    `json:"expires_in"`
}

// StartLogin は合図の符号を発行する。
func StartLogin(api string) (*LoginStart, error) {
	resp, err := http.Post(strings.TrimRight(api, "/")+"/api/auth/cli-start",
		"application/json", nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ログインを始められませんでした (応答 %d)", resp.StatusCode)
	}
	var out LoginStart
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return &out, nil
}

// PollLogin は承認されたかを 1 回確認する。
// まだなら (nil, nil) を返す — これは失敗ではない。
func PollLogin(api, code string) (*Credentials, error) {
	url := fmt.Sprintf("%s/api/auth/cli-poll?code=%s",
		strings.TrimRight(api, "/"), code)
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusNotFound:
		return nil, errors.New("合図の符号が見つかりません。もう一度やり直してください")
	case http.StatusGone:
		return nil, errors.New("合図の符号の有効期限が切れました。もう一度やり直してください")
	default:
		return nil, fmt.Errorf("確認できませんでした (応答 %d)", resp.StatusCode)
	}

	var body struct {
		Status      string `json:"status"`
		Email       string `json:"email"`
		IDToken     string `json:"id_token"`
		TokenExpiry int64  `json:"token_expiry"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "approved" {
		return nil, nil // まだ承認されていない
	}
	return &Credentials{
		Email:       body.Email,
		Token:       body.IDToken,
		TokenExpiry: body.TokenExpiry,
		TokenType:   "beacon_cli",
		WebAuth:     true,
	}, nil
}

// --- クラウドから読む ------------------------------------------------------

// CloudSource はクラウド上のプロジェクト 1 件。``Store`` のクラウド側に当たる。
type CloudSource struct {
	API       string
	Token     string
	ProjectID string
}

func (c *CloudSource) get(path string, out any) error {
	req, err := http.NewRequest("GET", strings.TrimRight(c.API, "/")+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		return errUnauthorized
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s を読めませんでした (応答 %d)", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// errUnauthorized は認証が通らなかったこと。画面でログインし直しへ誘導する。
var errUnauthorized = errors.New("認証が通りませんでした。ログインし直してください")

// CloudProject は参加しているプロジェクト 1 件。
type CloudProject struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Role string `json:"role"`
}

// ListProjects は参加しているプロジェクトを返す。
func (c *CloudSource) ListProjects() ([]CloudProject, error) {
	var out []CloudProject
	if err := c.get("/api/me/projects", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// Load はプロジェクトを読む。ローカルと同じ形の辞書が返るので、変換層はそのまま使える。
func (c *CloudSource) Load() (*Project, error) {
	var p Project
	if err := c.get("/api/projects/"+c.ProjectID, &p); err != nil {
		return nil, err
	}
	return &p, nil
}

// Sessions は Beacon に名乗っているセッションの名簿を返す。
//
// **これがクラウドに繋ぐ最大の意味**。ローカルのデータだけでは、誰がどの対象を
// 進めているかが分からない。他のマシンで動いているセッションもここに出る。
func (c *CloudSource) Sessions() ([]SessionRow, error) {
	var raw []struct {
		SessionID string `json:"session_id"`
		Cwd       string `json:"cwd"`
		ProjectID string `json:"project_id"`
		Live      bool   `json:"live"`
		LastActive string `json:"last_active"`
		Actor     struct {
			Email   string `json:"email"`
			Machine string `json:"machine"`
		} `json:"actor"`
		Agent struct {
			Kind string `json:"kind"`
		} `json:"agent"`
		Git struct {
			Branch      string `json:"branch"`
			HeadSubject string `json:"head_subject"`
		} `json:"git"`
		Focus struct {
			Milestone struct {
				ID    string `json:"id"`
				Title string `json:"title"`
			} `json:"milestone"`
		} `json:"focus"`
		// サーバが解決した「そのセッションの作業対象」。宣言 → fork → ブランチ →
		// cwd の順で決まる。**これが本当の担当**。
		WorkingTarget *struct {
			Target struct {
				ID    string `json:"id"`
				Label string `json:"label"`
			} `json:"target"`
			Source string `json:"source"`
		} `json:"working_target"`
		PollHealth struct {
			Healthy bool `json:"healthy"`
		} `json:"poll_health"`
		// サーバが出す「今何をしているか」の要約 (ms-159)。まだ出していなければ空。
		Activity string `json:"activity"`
		// 端末の種類 (apple-terminal / iterm2 等)。端末へ飛ぶときに、どの端末アプリを
		// 前面化するかの分岐に使う (ms-173 e-6403)。空なら不明。
		Runtime struct {
			Harness struct {
				Kind string `json:"kind"`
			} `json:"harness"`
		} `json:"runtime"`
	}
	if err := c.get("/api/me/sessions?live=true", &raw); err != nil {
		return nil, err
	}
	rows := []SessionRow{}
	for _, s := range raw {
		// 名簿は参加している全プロジェクト分が返るので、いま見ている盤のものに絞る。
		if c.ProjectID != "" && s.ProjectID != c.ProjectID {
			continue
		}
		if !s.Live {
			continue
		}
		row := SessionRow{
			ID:      s.SessionID,
			Who:     s.Actor.Email,
			Machine: s.Actor.Machine,
			Agent:   s.Agent.Kind,
			Cwd:     s.Cwd,
			Live:        s.Live,
			Healthy:     s.PollHealth.Healthy,
			LastActive:  s.LastActive,
			Branch:      s.Git.Branch,
			HeadSubject: s.Git.HeadSubject,
		}

		// 担当はサーバが解決した working_target を使う。
		//
		// **focus.milestone を担当として使ってはいけない。** あれは「プロジェクトの
		// 進行中マイルストーン」であって、そのセッションが何をしているかではない。
		// Beacon 自身のコードにも「session-specific ではない、別の対象で fork した
		// セッションに誤った値を出す」と明記されている (lib/working_target.py)。
		if wt := s.WorkingTarget; wt != nil && wt.Target.ID != "" {
			row.Target = wt.Target.ID
			row.TargetLabel = wt.Target.Label
			row.TargetSource = wt.Source
		}
		// プロジェクトの進行中マイルストーンは、担当とは別の欄で持つ。
		row.ProjectFocus = s.Focus.Milestone.ID
		// 「今何をしているか」はサーバの申告をそのまま運ぶ (無ければ空のまま)。
		row.Activity = s.Activity
		// 端末の種類も運ぶ (端末へ飛ぶときの分岐に使う)。
		row.Harness = s.Runtime.Harness.Kind
		rows = append(rows, row)
	}
	return rows, nil
}

// Documents はプロジェクトのドキュメント一覧を返す。
//
// 盤の脇に「何が書かれているか」が並ぶと、目的や決定の在り処に辿り着ける。
// 取れなくても盤は出す (呼び出し側で握りつぶす)。
func (c *CloudSource) Documents() ([]DocumentRow, error) {
	var raw []struct {
		DocID     string `json:"doc_id"`
		ID        string `json:"id"`
		Title     string `json:"title"`
		Scope     string `json:"scope"`
		UpdatedAt string `json:"updated_at"`
		Milestone string `json:"milestone"`
		Target    string `json:"target"`
	}
	if err := c.get("/api/projects/"+c.ProjectID+"/documents", &raw); err != nil {
		return nil, err
	}
	rows := []DocumentRow{}
	for _, d := range raw {
		id := d.DocID
		if id == "" {
			id = d.ID
		}
		target := d.Target
		if target == "" {
			target = d.Milestone
		}
		rows = append(rows, DocumentRow{
			ID: id, Title: d.Title, Scope: d.Scope,
			UpdatedAt: d.UpdatedAt, Target: target,
		})
	}
	return rows, nil
}
