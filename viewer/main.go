// beacon-view — Beacon の盤を単体で表示するビューワー (ms-170)。
//
// Beacon が入っていない PC でも盤を見られるようにするための、独立した実行ファイル。
// 読み取り専用で、Beacon のデータを書き換えない。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
)

func main() {
	path := flag.String("path", ".", "Beacon のフォルダ (.beacon、またはそれを含む場所)")
	asJSON := flag.Bool("json", false, "画面を出さず、盤の中身をそのまま出力する")
	flag.Parse()

	// 位置引数でも場所を受け取れるようにする (beacon-view ./あるフォルダ)。
	if args := flag.Args(); len(args) > 0 {
		path = &args[0]
	}

	src, err := OpenLocal(*path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	p, err := src.Load()
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}

	board := BuildBoard(p, SourceLocal, "", nil, nil)

	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(board); err != nil {
			fmt.Fprintln(os.Stderr, "Error:", err)
			os.Exit(1)
		}
		return
	}

	fmt.Printf("%s — 対象 %d 件 (完了 %d / 進行中 %d)\n",
		board.Project.Name, board.Progress.Total,
		board.Progress.Done, board.Progress.Open)
	if board.Unsupported != nil {
		fmt.Printf("  [注意] %s (%d 件)\n",
			board.Unsupported.Reason, board.Unsupported.TargetClassCount)
	}
}
