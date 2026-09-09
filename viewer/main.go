// beacon-view — Beacon の盤を単体で表示するビューワー (ms-170)。
//
// Beacon が入っていない PC でも盤を見られるようにするための、独立した実行ファイル。
// 読み取り専用で、Beacon のデータを書き換えない。
package main

import (
	"bufio"
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

	// プロジェクトが見つからなくても止めない。画面から場所を選んでもらう
	// (実行ファイルを配ってもらった人には、起動場所に .beacon が無いのが普通)。
	src, openErr := OpenLocal(*path)

	if *asJSON {
		// 中身をそのまま出す用途では選ばせる相手が居ないので、その場で断る。
		if openErr != nil {
			fail(openErr)
		}
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

	// 立ち上げる前に一度読んでおく。壊れた場所を渡されたとき、ブラウザを開いてから
	// 気づくのではなく、その場で理由を伝えるため。
	var loaded *Project
	if openErr != nil {
		src = nil
	} else {
		var err error
		loaded, err = src.Load()
		if err != nil {
			fail(err)
		}
	}

	srv, err := NewServer(src, *host, *port, *expose)
	if err != nil {
		fail(err)
	}

	fmt.Printf("盤を開きました: %s\n", srv.URL)
	if src != nil {
		fmt.Printf("  読んだ先: %s (%s)\n", src.BeaconDir, src.Kind)
		fmt.Printf("  対象 %d 件\n",
			len(BuildBoard(loaded, SourceLocal, "", nil, nil).Targets))
	} else {
		fmt.Println("  この場所に Beacon のデータが無いので、画面から場所を選んでください。")
	}
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
	waitIfWindowWillClose()
}

// fail は理由を伝えて終わる。
//
// エクスプローラーからダブルクリックで起動された場合は、**そのまま終わると窓が
// 閉じてメッセージが読めない**ので、読んでもらってから閉じる。シェルから実行された
// 場合は窓が残るので待たない (待つと自動処理の邪魔になる)。
func fail(err error) {
	fmt.Fprintln(os.Stderr, "Error:", err)
	waitIfWindowWillClose()
	os.Exit(1)
}

func waitIfWindowWillClose() {
	if !ownsConsole() {
		return
	}
	fmt.Println()
	fmt.Println("Enter キーを押すと閉じます。")
	bufio.NewReader(os.Stdin).ReadString('\n')
}
