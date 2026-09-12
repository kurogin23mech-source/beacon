// ローカルのドキュメント一覧 — 盤の脇に「何が書かれているか」を並べるため。
//
// `.beacon/documents/*.md` の前書き (--- で囲まれた部分) から題や区分を読む。
// 形は Beacon 本体 (lib/store_local.py list_documents) に合わせてあるので、
// クラウドから取ったものと画面で同じに扱える。
//
// 読み取り専用。読めなくても盤は出す (空の一覧で通す)。
package main

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// localDocuments は .beacon/documents の中身を一覧にする。
func localDocuments(beaconDir string) []DocumentRow {
	rows := []DocumentRow{}
	dir := filepath.Join(beaconDir, "documents")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return rows
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		row := DocumentRow{ID: strings.TrimSuffix(e.Name(), ".md")}
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err == nil {
			applyFrontMatter(&row, string(raw))
		}
		if info, err := e.Info(); err == nil && row.UpdatedAt == "" {
			row.UpdatedAt = info.ModTime().UTC().Format("2006-01-02T15:04:05Z")
		}
		if row.Title == "" {
			row.Title = row.ID // 題が無くても、何かは出す
		}
		rows = append(rows, row)
	}
	sort.SliceStable(rows, func(i, j int) bool {
		return rows[i].UpdatedAt > rows[j].UpdatedAt // 新しい順
	})
	return rows
}

// applyFrontMatter は前書きから題・区分・紐づく対象を読む。
func applyFrontMatter(row *DocumentRow, text string) {
	if !strings.HasPrefix(text, "---") {
		return
	}
	rest := strings.TrimPrefix(text, "---")
	end := strings.Index(rest, "\n---")
	if end < 0 {
		return
	}
	for _, line := range strings.Split(rest[:end], "\n") {
		key, value, found := strings.Cut(line, ":")
		if !found {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.Trim(strings.TrimSpace(value), `"'`)
		switch key {
		case "title":
			row.Title = value
		case "scope":
			row.Scope = value
		case "updated_at":
			row.UpdatedAt = value
		case "target", "milestone":
			if row.Target == "" {
				row.Target = value
			}
		}
	}
}
