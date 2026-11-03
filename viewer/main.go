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

// viewerVersion は build.sh が -ldflags -X で刻む (beacon 本体と同じ版)。
// 手元の素の go build では "dev" のまま — 版握手 (e-6482) は dev を通す。
var viewerVersion = "dev"

func main() {
	path := flag.String("path", ".", "Beacon のフォルダ (.beacon、またはそれを含む場所)")
	port := flag.Int("port", defaultPort, "待ち受ける口")
	host := flag.String("host", loopbackHost, "待ち受け先 (既定は自分の機械の中だけ)")
	expose := flag.Bool("expose", false, "自分の機械の外に開くことを承知して指定する")
	noOpen := flag.Bool("no-open", false, "ブラウザを自動で開かない")
	asJSON := flag.Bool("json", false, "画面を出さず、盤の中身をそのまま出力する")
	noReuse := flag.Bool("no-reuse", false,
		"既に動いている同じプロジェクトの盤を使わず、必ず新しく立てる")
	idleTimeout := flag.Duration("idle-timeout", defaultIdleTimeout,
		"この時間だけ誰も見に来なければ盤を畳む (既定 0 = 畳まない)")
	showVersion := flag.Bool("version", false, "版を出して終わる (beacon 側の委譲前の版握手用)")
	flag.Parse()

	// 版握手 (ms-173 e-6482): beacon 本体 (Python 入口) が委譲前に
	// `beacon-view --version` を叩いて版を照合する。この flag を持たない古い
	// beacon-view は flag パースで失敗して非 0 で終わるので、それ自体が
	// 「古い」の合図になる。出力形は 1 行固定 "beacon-view <版>"。
	if *showVersion {
		fmt.Printf("beacon-view %s\n", viewerVersion)
		return
	}

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
	//
	// ただし **実在しないパスを渡されたら walk-up させない** (#754 review M3)。OpenLocal は
	// filepath.Abs で相対パスを cwd に接ぎ木し親へ walk-up するため、存在しない --path を
	// 直接渡すと cwd 側の無関係な既存プロジェクトを誤って掴んで silent open する。/api/open で
	// 塞いだのと同一クラスの穴なので、CLI 起動も同じ入口 guard (resolveOpenTarget) を通して
	// 対称にする。実在すれば OpenLocal に検証済み abs を渡す (walk-up はそこで従来どおり働く)。
	// 実在しなければ openErr を立て、GUI では画面から選び直し / --json ではその場で断る。
	var src *LocalSource
	var openErr error
	if absPath, err := resolveOpenTarget(*path); err != nil {
		openErr = err
	} else {
		src, openErr = OpenLocal(absPath)
	}

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

	// 同じプロジェクトの盤が既に生きているなら、そちらを使う (ms-170 e-7147)。
	//
	// `listen` は希望の口が塞がっていれば黙って別の空き口を借りるので、この確認が
	// 無いと呼ぶたびに盤が増え、ランダムな口に散る (実測: 3 回開くと 3 本増える)。
	// 控えの番号が生きているかだけでなく **実際に問い合わせて同じプロジェクトを
	// 配っているか** まで確かめる (portfile.go)。
	//
	// `--port` を明示されたときは再利用しない。口を指定するのは「そこに立てたい」
	// という意思表示なので、黙って別の口の盤へ送ると指示と違う結果になる。
	// **明示されたかは `flag.Visit` で見る** —— 値が既定と同じかどうかで近似すると、
	// 既定値を明示的に渡した人の意思を取り違える (e-7147 保守性 review medium)。
	//
	// 判定そのものは `decideReuse` (portfile.go) に出してある。main() の中に置くと
	// どの試験も通らず、`--no-reuse` を無効化しても緑のままだった (同 review high)。
	reuseKey := *path
	if src != nil {
		reuseKey = filepath.Dir(src.BeaconDir)
	}
	pf := portfilePath(reuseKey)
	decision := decideReuse(*noReuse, flagWasSet(flag.Visit, "port"),
		reuseKey, pf, readPortfile, liveBoardURL)
	if decision.URL != "" {
		// 住所の出し方は新規に立てたときと同じ 1 行に揃える。呼び出し側
		// (scripts/open-board.py) はこの行だけを見て住所を拾うので、ここが
		// 揃っていないと「立ったのに住所が読めない」ことになる。
		fmt.Printf("盤を開きました: %s\n", decision.URL)
		fmt.Println("  (既に動いている盤を使いました。新しくは立てていません)")
		reportOrphanBoards(os.Getpid())
		if !*noOpen {
			OpenBrowser(decision.URL)
		}
		return
	}

	// 占めている席は盤を立てるときに決める (ms-170 思想 review medium / 保守性
	// review high)。画面からクラウドの取得元に切り替えても席は動かないので、
	// 再利用・控え・居残り判定が取得元によって割れない。構築後に変えられない
	// 形にしてあるのは、隣の『取得元切り替え時にリセットする』形に引きずられて
	// 席を消す変更が入っても、型で止まるようにするため。
	srv, err := NewServer(src, *host, *port, *expose, reuseKey)
	if err != nil {
		fail(err)
	}

	// 控えを置く。ただし **席を取っている生きた盤が他に居るなら触らない**
	// (e-7147 AX review high)。`--no-reuse` / `--port` 明示で再利用を飛ばした盤が
	// 上書きすると、その生きた盤が再利用から永久に見えなくなり「使うたびに増える」
	// が戻る。判断は shouldWritePortfile (portfile.go)。
	//
	// 置けなくても盤は成立する (= 次回の再利用ができないだけ) ので、失敗は伝える
	// だけで止めない。黙って落とすと「なぜ毎回増えるのか」が追えない。
	okToWrite, keepReason := shouldWritePortfile(
		readPortfile(pf), reuseKey, os.Getpid(), liveBoardURL)
	if !okToWrite {
		fmt.Fprintf(os.Stderr, "注意: %s\n", keepReason)
	} else if err := writePortfile(pf, boardRecord{
		Port: srv.Port(), PID: os.Getpid(), Host: srv.Host,
		Path: reuseKey, StartedAt: time.Now().Format(time.RFC3339),
	}); err != nil {
		fmt.Fprintf(os.Stderr,
			"注意: 盤の控えを置けませんでした (%v)。次回も新しい盤が立ちます\n", err)
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
	reportOrphanBoards(os.Getpid())

	if !*noOpen && isLoopback(srv.Host) {
		OpenBrowser(srv.URL)
	}

	// 既定を決めるのは idleTimeoutFor (idle.go) の 1 箇所。ここでは理由を書かない
	// —— 同じ事実を 2 箇所に書くと、片方だけ古くなる (e-7265: このコメントは
	// 「既定はモードで変える」と言ったまま残り、独立レビュー 2 体に指摘された)。
	idleRequested := flagWasSet(flag.Visit, "idle-timeout")
	effectiveIdle := idleTimeoutFor(*idleTimeout, idleRequested)
	// **必ず 1 行名乗る** (AX review medium)。出すかどうかを「モードが上書きしたか」
	// で決めていたため、`--idle-timeout 0` を明示したときだけ無言だった。
	fmt.Println(idleAnnouncement(effectiveIdle, idleRequested))
	if err := srv.ServeWithIdleTimeout(effectiveIdle); err != nil {
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
