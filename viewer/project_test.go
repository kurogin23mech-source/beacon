package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// /api/open が開く対象を検証する resolveOpenTarget の単体テスト (e-6526)。
//
// 実在しないパスを受理すると、OpenLocal の walk-up が cwd 側の無関係な既存
// プロジェクトを誤って掴む (「開けない場所が受理される」穴)。存在チェックで塞ぐ。
func TestResolveOpenTarget(t *testing.T) {
	existing := t.TempDir() // 実在するフォルダ

	t.Run("実在するフォルダは受理し abs を返す", func(t *testing.T) {
		abs, err := resolveOpenTarget(existing)
		if err != nil {
			t.Fatalf("実在フォルダを拒否した: %v", err)
		}
		if !filepath.IsAbs(abs) {
			t.Errorf("abs パスでない: %q", abs)
		}
	})

	t.Run("存在しないパスは『見つかりません』で拒否", func(t *testing.T) {
		bogus := filepath.Join(existing, "存在しない場所", "さらに奥")
		_, err := resolveOpenTarget(bogus)
		if err == nil {
			t.Fatal("存在しないパスを受理してしまった (walk-up で誤掴みする穴)")
		}
		if !strings.Contains(err.Error(), "見つかりません") {
			t.Errorf("拒否理由に『見つかりません』が無い: %v", err)
		}
	})

	t.Run("Windows 風の存在しないドライブパスも拒否 (parity test と同型)", func(t *testing.T) {
		// test_bad_path_is_refused_with_a_reason が送る値。macOS では cwd に接ぎ木
		// されて存在しないので拒否されるべき (この誤掴みが元バグ)。
		_, err := resolveOpenTarget("Z:/存在しない場所")
		if err == nil {
			t.Fatal("存在しないドライブパスを受理してしまった")
		}
		if !strings.Contains(err.Error(), "見つかりません") {
			t.Errorf("拒否理由に『見つかりません』が無い: %v", err)
		}
	})

	t.Run("空パスは指定なしとして拒否", func(t *testing.T) {
		_, err := resolveOpenTarget("   ")
		if err == nil {
			t.Fatal("空パスを受理してしまった")
		}
	})

	t.Run("ファイルはフォルダ契約に反するので拒否 (#754 M1)", func(t *testing.T) {
		f := filepath.Join(existing, "file.txt")
		if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
			t.Fatalf("テスト用ファイル作成失敗: %v", err)
		}
		_, err := resolveOpenTarget(f)
		if err == nil {
			t.Fatal("ファイルを受理してしまった (os.Stat はファイルも通す)")
		}
		if !strings.Contains(err.Error(), "フォルダではありません") {
			t.Errorf("拒否理由に『フォルダではありません』が無い: %v", err)
		}
	})
}
