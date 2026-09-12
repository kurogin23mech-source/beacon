package main

import (
	"os"
	"path/filepath"
	"testing"
)

// ブランチ名から担当を推し量る規則を固定する。
//
// これは **推測** であって確定ではない。人が自由に名前を付けられる以上、拾えない
// ことも、間違って拾うこともある。だからこそ規則を明示しておく。
func TestTargetFromBranch(t *testing.T) {
	cases := []struct {
		branch, want, why string
	}{
		{"ms-170-go-viewer", "ms-170", "先頭にある"},
		{"feat/ms-42-something", "ms-42", "途中にある"},
		{"ms-170-fork-672e41", "ms-170", "fork の枝"},
		{"MS-170-Upper", "ms-170", "大文字でも拾い、小文字で返す"},
		{"main", "", "対象を含まない"},
		{"", "", "枝に乗っていない"},
		{"release-2026", "", "数字はあるが対象ではない"},
		{"msabc-1", "", "似ているだけの名前は拾わない"},
	}
	for _, c := range cases {
		if got := targetFromBranch(c.branch); got != c.want {
			t.Errorf("targetFromBranch(%q) = %q, want %q (%s)",
				c.branch, got, c.want, c.why)
		}
	}
}

// 通常のリポジトリからブランチ名を読めること。
func TestGitBranchReadsHead(t *testing.T) {
	dir := t.TempDir()
	gitDir := filepath.Join(dir, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatal(err)
	}
	head := "ref: refs/heads/ms-170-go-viewer\n"
	if err := os.WriteFile(filepath.Join(gitDir, "HEAD"), []byte(head), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := gitBranch(dir); got != "ms-170-go-viewer" {
		t.Errorf("gitBranch = %q", got)
	}
}

// worktree では .git がファイルで本体を指す。そこも辿れること。
func TestGitBranchFollowsWorktree(t *testing.T) {
	root := t.TempDir()
	real := filepath.Join(root, "realgit")
	if err := os.MkdirAll(real, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(real, "HEAD"),
		[]byte("ref: refs/heads/ms-42-fork\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	work := filepath.Join(root, "work")
	if err := os.MkdirAll(work, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(work, ".git"),
		[]byte("gitdir: "+real+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := gitBranch(work); got != "ms-42-fork" {
		t.Errorf("worktree の枝を読めていない: %q", got)
	}
}

// 枝に乗っていない状態 (commit 直指し) では名前を返さないこと。
func TestGitBranchOnDetachedHead(t *testing.T) {
	dir := t.TempDir()
	gitDir := filepath.Join(dir, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitDir, "HEAD"),
		[]byte("41ecdf2c9a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := gitBranch(dir); got != "" {
		t.Errorf("枝に乗っていないのに %q を返した", got)
	}
}

// git が無いフォルダでも壊れないこと。
func TestGitBranchOnPlainFolder(t *testing.T) {
	if got := gitBranch(t.TempDir()); got != "" {
		t.Errorf("git の無い場所で %q を返した", got)
	}
}
