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
	port := flag.Int("port", defaultPort, "待ち受ける口")
	host := flag.String("host", loopbackHost, "待ち受け先 (既定は自分の機械の中だけ)")
	expose := flag.Bool("expose", false, "自分の機械の外に開くことを承知して指定する")
	noOpen := flag.Bool("no-open", false, "ブラウザを自動で開かない")
	asJSON := flag.Bool("json", false, "画面を出さず、盤の中身をそのまま出力する")
	flag.Parse()

	// 位置引数でも場所を受け取れるようにする (beacon-view ./あるフォルダ)。
	if args := flag.Args(); len(args) > 0 {
		path = &args[0]
	}

	src, err := OpenLocal(*path)
	if err != nil {
		fail(err)
	}

	if *asJSON {
		p, err := src.Load()
		if err != nil {
			fail(err)
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(BuildBoard(p, SourceLocal, "", nil, nil)); err != nil {
			fail(err)
		}
		return
	}

	// 立ち上げる前に一度読んでおく。読めない場所を渡されたとき、ブラウザを開いて
	// から気づくのではなく、その場で理由を伝えるため。
	p, err := src.Load()
	if err != nil {
		fail(err)
	}

	srv, err := NewServer(src, *host, *port, *expose)
	if err != nil {
		fail(err)
	}

	fmt.Printf("盤を開きました: %s\n", srv.URL)
	fmt.Printf("  読んだ先: %s (%s)\n", src.BeaconDir, src.Kind)
	fmt.Printf("  対象 %d 件\n", len(BuildBoard(p, SourceLocal, "", nil, nil).Targets))
	if !isLoopback(srv.Host) {
		fmt.Println("  [警告] この盤は自分の機械の外にも開いています。" +
			"認証は無いので、この口に届く全員が読めます。")
	}
	fmt.Println("  終了: Ctrl+C")

	if !*noOpen && isLoopback(srv.Host) {
		OpenBrowser(srv.URL)
	}

	if err := srv.Serve(); err != nil {
		fail(err)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "Error:", err)
	os.Exit(1)
}
