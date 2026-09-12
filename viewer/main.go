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
	"path/filepath"
	"strings"
	"time"
)

func main() {
	path := flag.String("path", ".", "Beacon のフォルダ (.beacon、またはそれを含む場所)")
	port := flag.Int("port", defaultPort, "待ち受ける口")
	host := flag.String("host", loopbackHost, "待ち受け先 (既定は自分の機械の中だけ)")
	expose := flag.Bool("expose", false, "自分の機械の外に開くことを承知して指定する")
	noOpen := flag.Bool("no-open", false, "ブラウザを自動で開かない")
	asJSON := flag.Bool("json", false, "画面を出さず、盤の中身をそのまま出力する")
	flag.Parse()

	// Go 標準 flag は最初の非フラグ引数でパースを打ち切る。そのため
	// `beacon-view <場所> --json` と打つと --json 以降が黙って捨てられ、意図と違う
	// 挙動 (画面起動でブロック) に静かに落ちる。AI は「コマンド 引数 フラグ」の順を
	// 高頻度で生成するので、フラグが場所より後ろに来たら黙って無視せず断る。
	rest := flag.Args()
	for _, a := range rest {
		if strings.HasPrefix(a, "-") {
			fail(fmt.Errorf(
				"フラグ %q が場所の指定より後ろにあります。"+
					"引数解釈は最初の場所でフラグの読み取りを止めるため、"+
					"このままでは %q は黙って無視されます。"+
					"フラグは場所より前に置いてください (例: beacon-view --json <場所>)。",
				a, a))
		}
	}
	if len(rest) > 1 {
		fail(fmt.Errorf(
			"場所は 1 つだけ指定してください (受け取った: %v)。", rest))
	}

	// 位置引数でも場所を受け取れるようにする (beacon-view ./あるフォルダ)。
	// --path と位置引数の同時指定は、どちらが効くか分かりにくいので断る。
	if len(rest) == 1 {
		pathSetByFlag := false
		flag.Visit(func(f *flag.Flag) {
			if f.Name == "path" {
				pathSetByFlag = true
			}
		})
		if pathSetByFlag {
			fail(fmt.Errorf(
				"場所は --path か位置引数のどちらか一方で指定してください " +
					"(両方渡されました)。"))
		}
		path = &rest[0]
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
		board := BuildBoard(p, SourceLocal, "", nil, nil)
		// 画面経由と同じ中身にする。ここを揃えないと、目で見たものと道具に渡した
		// ものが食い違う。
		board.LocalSessions = LocalSessions(
			filepath.Dir(src.BeaconDir), 24*time.Hour, time.Now())
		enc := json.NewEncoder(os.Stdout)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(board); err != nil {
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
