// 作業フォルダから、いま乗っているブランチを読む。
//
// Beacon に名乗っていないセッションには担当が付かない (担当はサーバ側で決まる)。
// だが、このプロジェクトではブランチ名が `ms-170-go-viewer` のように対象を含む
// 慣習があるので、そこから **推測** できる。
//
// あくまで推測なので、画面では推測と分かる形で出すこと。ブランチ名は人が自由に
// 付けられる以上、外れることがある。
package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// gitBranch は、そのフォルダが乗っているブランチ名を返す。読めなければ空。
//
// 通常のリポジトリでは `.git/HEAD` を読む。worktree では `.git` がファイルで、
// 本体の場所を指しているので辿る。
func gitBranch(dir string) string {
	gitPath := filepath.Join(dir, ".git")
	info, err := os.Stat(gitPath)
	if err != nil {
		return ""
	}
	headPath := filepath.Join(gitPath, "HEAD")
	if !info.IsDir() {
		// worktree: `gitdir: <本体の中の場所>` と書かれている。
		raw, err := os.ReadFile(gitPath)
		if err != nil {
			return ""
		}
		line := strings.TrimSpace(string(raw))
		target := strings.TrimSpace(strings.TrimPrefix(line, "gitdir:"))
		if target == "" || target == line {
			return ""
		}
		headPath = filepath.Join(target, "HEAD")
	}
	raw, err := os.ReadFile(headPath)
	if err != nil {
		return ""
	}
	head := strings.TrimSpace(string(raw))
	// 枝に乗っていれば `ref: refs/heads/<名前>`。そうでなければ commit を直指し
	// (= どの枝でもない状態) なので、名前は無い。
	const prefix = "ref: refs/heads/"
	if !strings.HasPrefix(head, prefix) {
		return ""
	}
	return strings.TrimPrefix(head, prefix)
}

// 対象の番号を含むブランチ名から、その対象を拾う (ms-170-go-viewer → ms-170)。
var branchTargetPattern = regexp.MustCompile(`(?i)\b(ms-\d+)`)

// targetFromBranch はブランチ名から担当の対象を推測する。拾えなければ空。
func targetFromBranch(branch string) string {
	m := branchTargetPattern.FindStringSubmatch(branch)
	if m == nil {
		return ""
	}
	return strings.ToLower(m[1])
}
