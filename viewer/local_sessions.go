// このマシンで動いている作業セッションの観測 (ms-171 e-6367)。
//
// Beacon を経由しないセッション (素の Claude Code / OpenCode / Codex) は誰にも
// 名乗らないので、Beacon の名簿には載らない。しかし **そのマシンの上には痕跡が残る**
// ので、ビューワーが動いている機械に限れば「どのフォルダで、いつ動いていたか」を
// 読み取れる。
//
// 届く範囲が根本的に違うことに注意 (doc lM9gHMOAm8VK2xZGKfEe):
//
//	Beacon 経由        … どのマシンからでも見える (クラウドの名簿に載る)
//	ここで拾うもの      … このマシンの上だけ
//
// 別のマシンで動いている素のセッションは、何も送っていない以上こちらに届く経路が
// 存在せず、原理的に見えない。**画面ではこの 2 種類を混ぜないこと。** 混ぜると
// 「相手のマシンの分も見えているはず」と誤解し、見えていないものに気づけなくなる。
//
// すべて読み取り専用。他の道具のデータを一切書き換えない。
package main

import (
	"bufio"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// LocalSessionRow は、このマシンで観測できた作業セッション 1 件。
//
// Beacon の名簿 (SessionRow) とは別の型にしてある。持っている情報が違い、届く範囲も
// 違うので、同じ型に混ぜると画面で見分けられなくなるため。
type LocalSessionRow struct {
	// Tool は何のセッションか (claude-code / opencode / codex)。
	Tool string `json:"tool"`
	// Directory は作業していたフォルダ。
	Directory string `json:"directory"`
	// Title は分かる場合の作業の呼び名 (OpenCode はセッション名を持っている)。
	Title string `json:"title"`
	// LastActive は最後に動いた時刻。
	LastActive string `json:"last_active"`
	// Running は、いまその道具のプロセスが動いているか (道具単位の判定)。
	Running bool `json:"running"`
}

// LocalSessions は、与えられたプロジェクトのフォルダで動いていたセッションを集める。
//
// ``root`` に一致するもの、およびその配下のフォルダのものを拾う (worktree のように
// 枝分かれした作業場所も同じプロジェクトの作業として見せたいため)。
// ``since`` より古いものは落とす。
func LocalSessions(root string, since time.Duration, now time.Time) []LocalSessionRow {
	rows := []LocalSessionRow{}
	home, err := os.UserHomeDir()
	if err != nil {
		return rows
	}
	rows = append(rows, claudeSessions(home)...)
	rows = append(rows, opencodeSessions(home)...)
	rows = append(rows, codexSessions(home)...)

	return filterAndSort(rows, root, since, now)
}

// filterAndSort は、このプロジェクトのものだけを残して新しい順に並べる。
// 判定を 1 箇所に閉じ込めて試験できるようにするために分けてある。
func filterAndSort(rows []LocalSessionRow, root string,
	since time.Duration, now time.Time) []LocalSessionRow {

	cutoff := now.Add(-since)
	kept := []LocalSessionRow{}
	for _, r := range rows {
		if !underRoot(r.Directory, root) {
			continue
		}
		// 何日も前に終わったものを「動いている」欄に並べると、いま何が
		// 起きているかが読めなくなる。
		if t, err := time.Parse(time.RFC3339, r.LastActive); err == nil {
			if t.Before(cutoff) {
				continue
			}
		}
		kept = append(kept, r)
	}
	// 新しい順。いま動いているものを先に見せる。
	sort.SliceStable(kept, func(i, j int) bool {
		return kept[i].LastActive > kept[j].LastActive
	})
	return kept
}

// underRoot は、その作業フォルダがこのプロジェクトのものかを判定する。
// 大文字小文字とスラッシュの向きの違いを吸収する (Windows で表記が揺れるため)。
func underRoot(dir, root string) bool {
	if dir == "" || root == "" {
		return false
	}
	norm := func(s string) string {
		s = strings.ReplaceAll(s, "\\", "/")
		s = strings.TrimRight(s, "/")
		return strings.ToLower(s)
	}
	d, r := norm(dir), norm(root)
	return d == r || strings.HasPrefix(d, r+"/")
}

// --- Claude Code -----------------------------------------------------------
// 会話の記録が ~/.claude/projects/<フォルダ名を符号化したもの>/<session>.jsonl に
// 残る。ファイルの更新時刻が最後に動いた時刻になる。

func claudeSessions(home string) []LocalSessionRow {
	base := filepath.Join(home, ".claude", "projects")
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil
	}
	running := processRunning("claude")
	var rows []LocalSessionRow
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dir := decodeClaudeDir(e.Name())
		files, err := os.ReadDir(filepath.Join(base, e.Name()))
		if err != nil {
			continue
		}
		for _, f := range files {
			if f.IsDir() || !strings.HasSuffix(f.Name(), ".jsonl") {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			rows = append(rows, LocalSessionRow{
				Tool:       "claude-code",
				Directory:  dir,
				LastActive: info.ModTime().UTC().Format(time.RFC3339),
				Running:    running,
			})
		}
	}
	return rows
}

// decodeClaudeDir は符号化されたフォルダ名を元に戻す。
// 例: "C--Users-dolph-Documents-Projects-beacon" → "C:/Users/dolph/Documents/Projects/beacon"
//
// 元のフォルダ名にハイフンが含まれていると元通りにはならないが、この用途では
// 「どのプロジェクトか」の突き合わせに使うだけなので、完全な復元は要らない。
func decodeClaudeDir(name string) string {
	s := strings.ReplaceAll(name, "--", ":/")
	s = strings.ReplaceAll(s, "-", "/")
	return s
}

// --- OpenCode --------------------------------------------------------------
// セッションが SQLite に入っており、作業フォルダと題名と更新時刻を持っている。

func opencodeSessions(home string) []LocalSessionRow {
	db := filepath.Join(home, ".local", "share", "opencode", "opencode.db")
	if _, err := os.Stat(db); err != nil {
		return nil
	}
	conn, err := sql.Open("sqlite",
		"file:"+filepath.ToSlash(db)+"?mode=ro")
	if err != nil {
		return nil
	}
	defer conn.Close()

	rows, err := conn.Query(
		"SELECT directory, title, time_updated FROM session")
	if err != nil {
		return nil
	}
	defer rows.Close()

	running := processRunning("opencode")
	var out []LocalSessionRow
	for rows.Next() {
		var dir, title sql.NullString
		var updated sql.NullInt64
		if err := rows.Scan(&dir, &title, &updated); err != nil {
			continue
		}
		last := ""
		if updated.Valid && updated.Int64 > 0 {
			// ミリ秒で入っている。
			last = time.UnixMilli(updated.Int64).UTC().Format(time.RFC3339)
		}
		out = append(out, LocalSessionRow{
			Tool:       "opencode",
			Directory:  dir.String,
			Title:      title.String,
			LastActive: last,
			Running:    running,
		})
	}
	return out
}

// --- Codex -----------------------------------------------------------------
// 記録が ~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl に残り、先頭行に作業
// フォルダが入っている。

func codexSessions(home string) []LocalSessionRow {
	base := filepath.Join(home, ".codex", "sessions")
	if _, err := os.Stat(base); err != nil {
		return nil
	}
	running := processRunning("codex")
	var out []LocalSessionRow
	// 記録は日付で階層になっている。古いものまで開くと重いので、新しい方から
	// 一定数だけ見る。
	var files []string
	filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".jsonl") {
			return nil
		}
		files = append(files, path)
		return nil
	})
	sort.Sort(sort.Reverse(sort.StringSlice(files)))
	if len(files) > 200 {
		files = files[:200]
	}
	for _, path := range files {
		dir := codexSessionDir(path)
		if dir == "" {
			continue
		}
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		out = append(out, LocalSessionRow{
			Tool:       "codex",
			Directory:  dir,
			LastActive: info.ModTime().UTC().Format(time.RFC3339),
			Running:    running,
		})
	}
	return out
}

// codexSessionDir は記録の先頭行から作業フォルダを読む。
func codexSessionDir(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	if !sc.Scan() {
		return ""
	}
	var head struct {
		Payload struct {
			Cwd string `json:"cwd"`
		} `json:"payload"`
	}
	if err := json.Unmarshal(sc.Bytes(), &head); err != nil {
		return ""
	}
	return head.Payload.Cwd
}
