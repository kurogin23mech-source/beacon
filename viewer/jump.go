// 端末へ飛ぶ (jump-to-terminal, ms-173 e-6402)。
//
// 運用室の一覧から「実物の端末」へ辿り着けるようにする。並列に走るセッションを
// 見ているとき、「これを直接見たい」となったら手で端末を探すのではなく、一覧の操作で
// その端末ウィンドウ/タブを前面化する。
//
// 辿り方: セッションのプロセス番号 (pid) → 制御端末 (tty) → その tty を持つ端末
// ウィンドウ/タブを AppleScript で前面化する。
//
// **これは「橋渡し」であって主役ではない** (SPEC 方針5)。運用室は対象・状態・
// activity で作業が完結するのが目標で、飛ぶのはどうしても実物を見たい時の最後の手段。
//
// **loopback (= 自分の機械の中) 限定** (SPEC 方針4)。外部公開時 (--expose) は無効。
// 他人のブラウザから他所の端末は前面化できず、無意味かつ危険なため。
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// commandRunner はコマンド実行を差し替え可能にする継ぎ目 (テストで stub するため)。
// これが無いと ps / osascript を実際に叩くしか検証手段がなく、エラー処理の変更を
// 局所フィードバックで確かめられない。
type commandRunner func(name string, args ...string) ([]byte, error)

// defaultRunner は実プロセスを起動する既定の実行器。
func defaultRunner(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).Output()
}

// psProbeTimeout は導出で叩く ps 1 件あたりの待ち時間の上限 (e-7265)。
//
// 名前が `probeTimeout` でないのは、portfile.go が控えへの HTTP 問い合わせ用に
// その名前を既に使っているから。別の外部依存の別の上限なので、同じ名前にすると
// 「どちらの待ち時間か」が読めなくなる。
//
// **上限を置く理由**: `/api/sessions` は画面が 5 秒ごとに叩く道で、その中で
// 名乗りが無い行ぶんだけ ps を直列に起動する。ps が返らない状況 (NFS 上の
// ホーム、別プロセスの異常に巻き込まれる等) は実在し、上限が無いと 1 行の
// 停止が一覧全体を無期限に止める。このリポジトリは「本番が生きているのに
// 応答しない。真因は外部依存にタイムアウトが無かったこと」を既に一度払って
// いるので、新しい外部プロセス呼び出しに同じ穴を開けない。
//
// 値は「導出できないより遅いほうがまし」ではなく逆向きに選ぶ: 導出は
// 補助であって、失敗しても飛べない理由を正直に出せば済む。だから短い。
const psProbeTimeout = 2 * time.Second

// probeRunner は **ps へのプローブ専用** の継ぎ目 (e-7266)。
//
// commandRunner と形が違う (context を受け取る) のは、**上限を呼び出し元の選択に
// しないため**。
//
// 最初の版は「上限つきの実行器」と「上限無しの実行器」を 2 本用意し、
// 呼ぶ側がプローブには上限つきを渡す約束にしていた。両方が同じ型 `commandRunner`
// だったので、渡し間違えてもコンパイルもテストも赤くならない —— そして **実際に
// 間違えていた**: 一覧経路 (`/api/sessions`) は上限つきを渡していたが、単発ジャンプ
// 経路 (`jumpToTerminal`) は既定の実行器を渡しており、同じ `ps -E` が無期限に走った。
// 人が押して待っている側にだけ上限が無い、という最悪の割れ方で、独立レビュー 2 体が
// 別々に実測して指摘した。
//
// いまは **上限を termProgramForPIDWith が自分で張る**。どの経路から呼ばれても、
// どの実行器を渡されても、プローブは psProbeTimeout で諦める。前面化そのもの
// (osascript) は人が待っている操作なので、そちらは従来どおり commandRunner のまま
// —— 型が違うので、取り違えると**コンパイルが通らない**。
type probeRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

// execProbeRunner は実プロセスを context 付きで起動する既定のプローブ実行器。
func execProbeRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).Output()
}

// jumpEnvBlocked は「端末へ飛ぶ」が環境ごと使えない理由を **1 か所** で判定する
// (純関数)。使えるなら空文字列を返す。
//
// macOS (osascript がある) かつ 自分の機械の中 (loopback) かつ 非公開 のときだけ
// 使える。理由は 2 値に分かれる — OS 非対応 (unsupported-os) と外部公開・非
// loopback (exposed)。同じ「環境起因」でも直し方が違う (OS は変えられない /
// 公開は起動フラグ) ので、1 つの値に潰すと受け手が誤診する (#756 独立 AX レビュー)。
// 対応 OS / 公開ポリシーを変えるときはこの関数 1 つを直せば、ボタンの表示可否
// (jumpAvailable 経由) も揃って変わる (= 判定の真実源を 1 つに)。
func jumpEnvBlocked(goos, host string, expose bool) string {
	if goos != "darwin" {
		return JumpBlockedUnsupportedOS
	}
	if !isLoopback(host) || expose {
		return JumpBlockedExposed
	}
	return ""
}

// jumpSupported は「端末へ飛ぶ」が使えるか (= 環境ブロックが無いか)。
func jumpSupported(goos, host string, expose bool) bool {
	return jumpEnvBlocked(goos, host, expose) == ""
}

// ttyFromPS は `ps -o tty= -p <pid>` の出力から制御端末名を取り出す (純関数)。
//
// ps は環境により "s000" / "ttys000" / "/dev/ttys000" と表記が揺れる。制御端末が
// 無いプロセスは "?" / "??" / 空を返す — その場合は空文字 (= 前面化できない) を返す。
func ttyFromPS(out string) string {
	t := strings.TrimSpace(out)
	if t == "" || t == "?" || t == "??" || t == "-" {
		return ""
	}
	t = strings.TrimPrefix(t, "/dev/")
	// ps は "ttys000" を "s000" と短縮することがあるので "tty" を補う。
	if !strings.HasPrefix(t, "tty") {
		t = "tty" + t
	}
	return t
}

// resolveTTY は pid の制御端末を引く (既定の実行器で ps を叩く)。
func resolveTTY(pid int) (string, error) {
	return resolveTTYWith(defaultRunner, pid)
}

// resolveTTYWith は実行器を差し替え可能にした本体 (テスト可能)。
//
// **pid の有効性検査はここが唯一のガード** (#756 独立保守性レビュー: 呼び出し側と
// 二重に置くと、片方だけ直したときにエラー型・status が silent に割れる)。無効 pid
// は入力起因 (400) なので *jumpFailure で返し、呼び出し側はそのまま status に写す。
func resolveTTYWith(run commandRunner, pid int) (string, error) {
	if pid <= 0 {
		// pid が無効 / 欠落 (0 は Go の int ゼロ値)。「別マシン」とは別の失敗。
		return "", &jumpFailure{http.StatusBadRequest, fmt.Sprintf(
			"プロセス番号が指定されていないか無効です (pid=%d)", pid)}
	}
	out, err := run("ps", "-o", "tty=", "-p", strconv.Itoa(pid))
	if err != nil {
		return "", fmt.Errorf(
			"プロセス %d の端末を辿れません (すでに終了している可能性)", pid)
	}
	tty := ttyFromPS(string(out))
	if tty == "" {
		return "", fmt.Errorf(
			"プロセス %d に制御端末がありません (端末の外で動いている可能性)", pid)
	}
	return tty, nil
}

// appleTerminalJumpScript は tty を持つ Terminal.app のタブを前面化する
// AppleScript を組む (純関数、テスト可能)。見つかれば "true"、無ければ "false" を返す。
//
// **2 段構成にしてある理由** (e-7219): 以前は windows をなめている最中に
// `set frontmost of w to true` していた。前面化は窓の前後関係 —— つまり windows の
// 並びそのもの —— を変えるので、進行中の `w` (= `window 3` のような相対参照) が
// 別の窓を指すか範囲外になり、「正しくないインデックスです (-1719)」で落ちる。
// 対象の窓が並びのどこに居るかで落ちるか通るかが変わるため、利用者には
// 「飛べる時と飛べない時がある」と見えていた。
//
// 第 1 段は**読むだけ**で窓の id (整数、安定) を拾い、ループを抜ける。第 2 段は
// その id の**絶対参照** (`first window whose id is N`) で解決し直してから選択する。
// 絶対参照は並びが変わっても同じ窓を指し続けるので、選択による並び替えと衝突しない。
// 「先に見つけてループを抜けてから選択する」だけでは直らない —— 持ち回る参照が
// 相対参照のままだと、後から解決し直す時点で同じ問題を踏む (e-7219 で実測済み)。
//
// **ただし Terminal.app では、この落ち方は実際には起きない** (e-7219 で実測):
// Terminal.app の AppleScript は iTerm2 とモデルが違い、**タブを 1 つずつ別の
// `window` オブジェクトとして見せる**。3 タブの物理ウィンドウは 3 個の `window`
// として現れ、どれも `tabs` は 1 個しか持たない (Terminal.app 2.14 で計測。同じ
// 物理ウィンドウのタブは `bounds` が一致するので、そこで見分けられる)。
// -1719 は `item 2 of every tab of item M of every window` が範囲外になる現象
// なので、内側が常に 1 要素なら起こり得ない。iTerm2 3.7.6 は入れ子 (最大 3) で、
// そちらでは 48 試行中 15 件落ちた。
//
// それでも同じ 2 段構成に揃えてある。理由は 2 つ。① 外側の `windows` の並びは
// `set frontmost` で実際に変わるので、一致した後も列挙を続ける形は「どの窓を
// 見たか」が不定になる (今は tty が一意なので結果は正しいが、前提に寄りかかって
// いる)。② 片方だけ違う書き方で残すと、次に触る人がどちらが正しい形か判断できない。
// **他方の端末で落ちたからこちらも落ちる、と一般化しないこと** —— ホストごとに
// オブジェクトモデルが違うので、必ず計測してから言う。
func appleTerminalJumpScript(tty string) string {
	dev := "/dev/" + tty
	return fmt.Sprintf(`tell application "Terminal"
  -- 第 1 段: 読むだけ。ここで選択すると windows の並びが変わり、
  -- なめている最中の相対参照が無効になって -1719 で落ちる (e-7219)。
  set wid to missing value
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is %[1]q then
        set wid to id of w
        exit repeat
      end if
    end repeat
    if wid is not missing value then exit repeat
  end repeat
  if wid is missing value then return false
  -- 第 2 段: id の絶対参照で解決し直してから選択する。並びが変わっても
  -- 同じ窓/タブを指し続ける。
  set tw to (first window whose id is wid)
  set selected of (first tab of tw whose tty is %[1]q) to true
  set frontmost of tw to true
  activate
  return true
end tell`, dev)
}

// terminalDriver は 1 種類の端末アプリの前面化の仕方を束ねる。
//
// **端末種別に関する知識はこのテーブル 1 か所だけ** に置く (SPEC の単一の真実源)。
// 新しい端末 (WezTerm 等) を足すときは、ここに 1 エントリ (kinds / script / appName)
// を加えるだけで、飛べる判定・スクリプト選択・表示名・画面のボタン (Jumpable 経由) が
// 揃って対応する。判定を JS 側に複製しない (= Go が唯一の正典、page.html は Jumpable を読むだけ)。
type terminalDriver struct {
	// kinds はサーバの harness.kind として来うる別名 (すべて小文字)。正典名
	// (名乗り側 channel/bus-harness-kind.mjs の TERM_PROGRAM_KINDS の値) と旧別名。
	kinds []string
	// rawNames は、名乗り側が正規化表を持たなかった頃 (または直す前の bridge) が
	// `terminal:<TERM_PROGRAM>` の生の形で送ってくる TERM_PROGRAM の値 (小文字)。
	// 生の形は **この rawNames との完全一致でだけ** 受ける — 不明値 (unknownHarnessKinds)
	// ("" / "terminal") や短縮形 ("iterm") には落とさない。未知の端末が偶然
	// `terminal:Terminal` と名乗ったとき、既知端末に化けて前面化を試み、的外れな
	// 回復ヒントを出す経路を閉じるため (PR#820 独立 AX レビュー)。
	rawNames []string
	// script は tty を持つタブを前面化する AppleScript を組む純関数。
	script func(tty string) string
	// appName は見つからない時のメッセージに出す表示名。
	appName string
}

// terminalDrivers は対応端末の一覧 (= 受け付け側の唯一の正典)。ここに無い端末
// (kitty / VS Code / tmux 等) は前面化に未対応で、画面では fallback (パスのコピー)
// に回す。
//
// **「どの端末か分からない」はこの表で解決しない** (e-7261)。不明値の定義と理由は
// unknownHarnessKinds の 1 箇所にある。以前この見出しは「空文字は種類不明で、macOS の
// 既定端末 Terminal.app とみなす」と書いていた —— 既定で埋める挙動を外した後もその
// まま残り、**表の中に書いた経緯と、表の外の見出しが逆のことを言う**状態になっていた
// (独立保守性レビュー medium)。見出しは最初に読まれる位置なので、局所だけ読む人は
// 旧契約を前提に変更を書いてしまう。
//
// **実機検証スクリプトは、このテーブルから自動化されていない** (独立保守性レビュー
// 指摘、e-7219)。`scripts/verify-jump-terminal-reorder.sh` は対応端末を独自の
// `case` 文と端末ごとの列挙スクリプトで持っている (AppleScript のオブジェクト
// モデルが端末ごとに違い、列挙の書き方を導出できないため)。**端末を足すときは
// あちらにも 1 分岐足す**。ここに足すだけで揃うのは下の 4 点 (飛べる判定 /
// スクリプト選択 / 表示名 / 画面のボタン) に限られる。
//
// 名乗り側 (channel/bus-harness-kind.mjs の TERM_PROGRAM_KINDS) の各行
// `TERM_PROGRAM → 正典名` は、**同じ 1 つの driver** の rawNames (= 小文字の
// TERM_PROGRAM) と kinds (= 正典名) に対応していなければならない。別名を変える /
// 端末を足すときは両方を同じ変更で直す — tests/test_harness_kind_parity_e7199.py が
// 片方だけの変更や、別の driver への置き間違いを赤にする (e-7199: 片方にだけ iTerm2
// が在って実機で一度も飛べなかった)。
var terminalDrivers = []terminalDriver{
	{
		// "" と "terminal" は**ここに置かない** (e-7261)。どちらも
		// 「どの端末か分からない」であって「Apple Terminal である」ではない。
		// 以前は両方を Apple Terminal の別名として登録し、不明なら Terminal.app
		// を既定にして「飛べる」と答えていた (jump_test の
		// 「harness 不明は Terminal.app 既定で飛べる」がその決定)。
		//
		// 実機で起きたこと (2026-11-02 実測): iTerm しか使っていない機械で、
		// 名乗りが届いていない 3 セッションと、bridge が TERM_PROGRAM を継承でき
		// ずに下限値 "terminal" を送った 1 セッションの計 4 行が、すべて
		// 「飛べる」と表示されたまま Terminal.app 用のスクリプトを iTerm の tty に
		// 投げていた。AppleScript は対象を見つけられないので**何も起きない**。
		// 利用者には「運用室が全然 iTerm に遷移しない」としか見えない。
		//
		// 既定で埋めるのをやめ、代わりに **プロセスに訊く** (resolveHarnessWith)。
		// 端末の種類はプロセスの性質であり、同じ機械に居る盤は pid を持っている
		// ので、名乗りが無くても確かめられる。既定値は推測で、推測が外れたとき
		// 「飛べる」と嘘をつく側に倒れていた。
		kinds:    []string{"apple-terminal", "apple_terminal"},
		rawNames: []string{"apple_terminal"},
		script:   appleTerminalJumpScript,
		appName:  "Terminal.app",
	},
	{
		kinds:    []string{"iterm2", "iterm", "iterm.app"},
		rawNames: []string{"iterm.app"},
		script:   iTerm2JumpScript,
		appName:  "iTerm2",
	},
}

// rawTerminalPrefix は、名乗り側 (channel/bus-harness-kind.mjs) が正規化表に無い
// 端末を送るときの接頭辞 (`terminal:<TERM_PROGRAM>`)。JS 側の RAW_TERMINAL_PREFIX と
// 同じ値 — tests/test_harness_kind_parity_e7199.py が突き合わせる。
const rawTerminalPrefix = "terminal:"

// splitHarnessKind は名乗られた端末の種類を「生の形か」と「表引きの鍵」に分ける
// (純関数)。小文字化・前後空白の除去をした上で、生の形 (`terminal:iTerm.app`) なら
// raw=true と接頭辞を剥がした残り (`iterm.app`) を、そうでなければ raw=false と
// 値そのものを返す。
//
// e-7199: 直す前の bridge は iTerm2 を `terminal:iTerm.app` と名乗っていた (今も
// 再起動するまで名乗り続ける)。生の形を rawNames に当てれば古い bridge のセッション
// にも飛べる。知らない端末 (`terminal:kitty`) は rawNames に無いので未対応に落ちる。
func splitHarnessKind(harness string) (raw bool, key string) {
	k := strings.ToLower(strings.TrimSpace(harness))
	if strings.HasPrefix(k, rawTerminalPrefix) {
		return true, strings.TrimSpace(strings.TrimPrefix(k, rawTerminalPrefix))
	}
	return false, k
}

// unknownHarnessKinds は「どの端末か分からない」を意味する名乗り (e-7261)。
//
// ふたつとも端末の種類ではなく **情報の欠如** を表す:
//
//	""         … 名乗りが届いていない (名簿に居ない / 古い bridge)。名乗り側
//	             (channel/bus-harness-kind.mjs) はこの値を返さない — 下限は
//	             "terminal" なので、空は「送られてこなかった」でしかない。
//	"terminal" … 名乗り側の下限値 = TERM_PROGRAM が **bridge の環境に** 無かった。
//	             bridge は MCP ホストが起こすので端末の環境を継承しないことが
//	             あり、端末の中で動いている claude 本体には TERM_PROGRAM が
//	             在るのにこちらだけ空、という形が実機で起きる。
//
// どちらも Apple Terminal と同じ扱いにしてはならない。混ぜると「分からない」が
// 「Terminal.app である」に化け、別の端末を使っている人に対して黙って外れる。
var unknownHarnessKinds = map[string]bool{"": true, "terminal": true}

// unknownHarness は名乗りが端末の種類を特定していないか (純関数)。
func unknownHarness(harness string) bool {
	_, k := splitHarnessKind(harness)
	return unknownHarnessKinds[k]
}

// termProgramPrefix は ps が返す環境の中で端末アプリが立てる変数。
const termProgramPrefix = "TERM_PROGRAM="

// probeResult は導出の結果を 3 通りに分ける (e-7265)。
//
// **「読めなかった」と「読めたが端末ではなかった」を同じ値に畳まない。** 前者は
// 権限不足・プロセスの消失・ps の失敗で、待てば変わる / 別の原因を調べる話。後者は
// 端末の外で動いているという確定した事実。対処が真逆なので、同じ「不明」にすると
// 読み手が誤った方向に動く。e-7263 で `background-session` を
// `unsupported-terminal` から切り出したのと同じ理由だが、**導出を入れた時点で私は
// 同じ合流を新しく作っていた** (独立 AX レビューが実測して指摘)。
type probeResult int

const (
	// probeOK はプロセスから端末の種類が読めた。
	probeOK probeResult = iota
	// probeNoTerminal はプロセスは読めたが TERM_PROGRAM が無かった
	// (= 端末の外で動いている)。
	probeNoTerminal
	// probeUnreadable はプロセスを読めなかった (権限不足 / 消失 / ps の失敗)。
	probeUnreadable
)

// termProgramForPIDWith は pid のプロセス環境から TERM_PROGRAM を読む。
//
// macOS の ps は -E で環境を command の後ろに連ねて出す。環境変数の値に空白は
// 入り得るが、ここで要るのは TERM_PROGRAM の値だけで、端末アプリ名に空白は
// 入らない (Apple_Terminal / iTerm.app)。空白区切りの最初の一致を取る。
func termProgramForPIDWith(run probeRunner, pid int) (string, probeResult) {
	if pid <= 0 {
		return "", probeUnreadable
	}
	// **上限はここで張る** (e-7266)。呼び出し元に選ばせない — 詳細は probeRunner。
	ctx, cancel := context.WithTimeout(context.Background(), psProbeTimeout)
	defer cancel()
	out, err := run(ctx, "ps", "-E", "-o", "command=", "-p", strconv.Itoa(pid))
	if err != nil {
		return "", probeUnreadable
	}
	for _, field := range strings.Fields(string(out)) {
		if strings.HasPrefix(field, termProgramPrefix) {
			v := strings.TrimSpace(strings.TrimPrefix(field, termProgramPrefix))
			if v == "" {
				// 変数は在るが空 = 端末の名乗りが無いのと同じ。
				return "", probeNoTerminal
			}
			return v, probeOK
		}
	}
	return "", probeNoTerminal
}

// resolveHarnessWith は「飛ぶ先を決めるための端末の種類」を確定する (e-7261)。
//
// 名乗りが端末を特定していれば **それを信じる** (別マシンのセッションや、
// プロセスを覗けない場合でも効く唯一の材料)。特定していないときだけ、pid の
// プロセス環境に訊いて `terminal:<TERM_PROGRAM>` の生の形を組む。生の形の解決は
// 既存の rawNames 経路がそのまま受けるので、**語彙は 1 つも増えない**
// (TERM_PROGRAM を小文字にした綴りが rawNames そのもの: apple_terminal /
// iterm.app)。導出もできなければ名乗りをそのまま返す = 不明のまま、
// jumpVerdict が unsupported-terminal として正直に落とす。
func resolveHarnessWith(run probeRunner, pid int, declared string) (string, probeResult) {
	if !unknownHarness(declared) {
		// 名乗りで足りているので訊いていない。「読めた」とは言わない。
		return declared, probeOK
	}
	tp, res := termProgramForPIDWith(run, pid)
	if res == probeOK {
		return rawTerminalPrefix + tp, probeOK
	}
	return declared, res
}

// driverForHarness は harness に対応する driver を返す (無ければ nil)。
//
// 生の形は rawNames との完全一致でだけ受け、kinds の別名には当てない (逆も同じ)。
// 2 つの照合面を混ぜると、未知の端末の生の名乗りが不明値 ("" / "terminal") に
// 吸着して既知端末に化ける (PR#820 独立 AX レビューが実測した破れ)。
func driverForHarness(harness string) *terminalDriver {
	raw, k := splitHarnessKind(harness)
	for i := range terminalDrivers {
		names := terminalDrivers[i].kinds
		if raw {
			names = terminalDrivers[i].rawNames
		}
		for _, want := range names {
			if k == want {
				return &terminalDrivers[i]
			}
		}
	}
	return nil
}

// jumpableHarness は「端末へ飛ぶ」に対応した端末かどうか (前面化できるか)。
// 対応外は fallback (パスのコピー) に回す。frontend には Go が計算した
// SessionOverview.Jumpable を渡す (JS 側に許可集合を複製しない = ドリフト防止)。
func jumpableHarness(harness string) bool {
	return driverForHarness(harness) != nil
}

// --- 飛べるかの最終判定 (e-6427) -------------------------------------------
//
// 飛べない理由の閉じた enum。画面はこの値を読むだけで、理由を再推論しない。
// 以前は画面側が jump_available && pid && jumpable の 3 値 AND を再構成しており、
// 「Go が唯一の正典」の宣言が最後の AND で破れ、別マシンと未対応端末が同じ
// false に潰れて区別できなかった (#744 独立レビュー)。
const (
	// JumpBlockedExposed は外部公開・非 loopback で飛べない (この盤からはどの
	// セッションにも飛べない)。--expose を外して loopback で開き直せば直る。
	JumpBlockedExposed = "exposed"
	// JumpBlockedUnsupportedOS はこの盤の OS が前面化に未対応 (現状 macOS のみ)。
	// exposed と違い、起動の仕方を変えても直らない (#756 独立 AX レビューで
	// 「exposed に潰すと OS 起因を公開ポリシーの問題と誤診する」指摘を受け分離)。
	JumpBlockedUnsupportedOS = "unsupported-os"
	// JumpBlockedRemoteMachine は別マシンのセッション。そのマシンの端末は
	// この盤からは前面化できない。
	JumpBlockedRemoteMachine = "remote-machine"
	// JumpBlockedNoPID はプロセス番号が取れていない (端末を辿る起点が無い)。
	JumpBlockedNoPID = "no-pid"
	// JumpBlockedUnsupportedTerminal は前面化に未対応の端末 (kitty / VS Code 等)。
	// 画面はこの値のときだけ fallback (パスのコピー) を出す。
	JumpBlockedUnsupportedTerminal = "unsupported-terminal"
	// JumpBlockedBackgroundSession は端末の中で動いていないセッション
	// (Claude Code の台帳で kind="bg")。前面化する窓が **原理的に存在しない**。
	//
	// unsupported-terminal と分ける理由 (e-7263): 両者は対処が真逆になる。未対応
	// 端末は「受け付け表に足せば飛べる」が、バックグラウンド実行は何を足しても
	// 飛べない。潰すと読み手は「kitty でも使っているのか」「対応を増やせばいいのか」
	// と受け取り、延々と直らないものを直そうとする。JumpBlockedUnsupportedOS を
	// exposed から分離したのと同じ理由 (#756 独立 AX レビュー)。
	//
	// 実測 (2026-11-02): 台帳は kind を区別して持っているのに viewer が読んで
	// 捨てていたため、バックグラウンドのセッション 1 件が「この端末は前面化に
	// 未対応です」と表示されていた。
	JumpBlockedBackgroundSession = "background-session"
	// JumpBlockedProcessUnreadable はプロセスを読めなかった (権限不足 / 呼び出しの
	// 間に終了 / ps の失敗・タイムアウト)。**端末の種類の話ではない。**
	//
	// unsupported-terminal と分ける理由 (e-7265): 対処が真逆になる。未対応端末は
	// 「受け付け表に足せば飛べる」が、読めないのは待てば変わる / 別の原因を調べる
	// 話で、表に足しても何も起きない。e-7261 で導出を入れたとき、私は ps の失敗を
	// unsupported-terminal に合流させていた —— e-7263 でまさにその型を切り出した
	// 直後に、同じ PR で新しい合流を作っていた (独立 AX レビューが ps を差し替えて
	// 実測し指摘)。
	JumpBlockedProcessUnreadable = "process-unreadable"
	// JumpBlockedTerminalUnnamed はプロセスは読めたが、端末を名乗っていなかった
	// (TERM_PROGRAM が無い)。**端末の種類の話でも、読めなかった話でもない。**
	//
	// 3 つ目の合流を切り出した理由 (独立レビュー AX / 保守性が 2 体とも同じ場所を
	// 指した)。導出 (probeResult) は最初から 3 値 — 読めた / 読めたが端末の外 /
	// 読めなかった —— を持っていたのに、行の印へ写すところで probeNoTerminal が
	// 「読めた」と同じ空文字に畳まれ、判定まで届いていなかった。その結果この値は
	// unsupported-terminal に落ち、画面は「この端末は前面化に未対応です」= 受け付け
	// 表に足せば直る、と表示していた。足しても直らない (名乗る端末が居ない)。
	//
	// background-session にも畳まない: あちらは台帳が kind="bg" と **宣言している**
	// 確定事実だが、こちらは「名乗りが無い」という観測で、端末の外に居るのか、
	// 名乗りを渡さない起動の仕方なのかを区別できない。同じファイルの
	// jumpVerdictKind が既に「『分からない』を bg 扱いにするのは、知らないことを
	// 根拠に機能を止めることになる」と定めているので、その規則に従う。
	JumpBlockedTerminalUnnamed = "terminal-unnamed"
)

// AllJumpBlockedReasons は飛べない理由の全値 (= 閉じた enum の唯一の一覧)。
//
// 試験と画面の突合がここを回すので、理由を足したら**手写しの列挙を直す必要が無い**
// — 逆に、足したのに画面の対応表 (JUMP_BLOCKED_REASON) に無ければ
// TestJumpBlockedLiteralMatchesPage が赤くなる。以前は試験側に 5 値を手で並べて
// いたため、6 値目を足しても試験が気付かない形だった (e-7263 で気付いて直した)。
var AllJumpBlockedReasons = []string{
	JumpBlockedExposed,
	JumpBlockedUnsupportedOS,
	JumpBlockedRemoteMachine,
	JumpBlockedNoPID,
	JumpBlockedBackgroundSession,
	JumpBlockedProcessUnreadable,
	JumpBlockedTerminalUnnamed,
	JumpBlockedUnsupportedTerminal,
}

// jumpVerdict は「このセッションへ飛べるか」の最終 1 値と、飛べない理由を返す
// (純関数)。判定の真実源はここ 1 つで、画面は結果を読むだけ。
// envBlocked はこの盤の環境ブロック (jumpEnvBlocked の結果、空 = 環境は OK)。
//
// **理由は影響範囲の広い順に選ぶ。いまの順序は 7 段階** (e-7266 で表を現在の
// switch に合わせた。以前は 4 段階のままで、新しい理由をどこに挿すか読み手が
// switch の物理順を自分で読むしかなかった):
//
//  1. exposed / unsupported-os  … 盤全体の話 (この盤からはどこへも飛べない)
//  2. remote-machine            … そのマシンの端末はこの盤から前面化できない
//  3. no-pid                    … 端末を辿る起点が無い
//  4. background-session        … 端末の中で動いていない (窓が原理的に無い)
//  5. process-unreadable        … プロセスを読めなかった (待てば変わりうる)
//  6. terminal-unnamed          … 読めたが端末を名乗っていない
//  7. unsupported-terminal      … 読めて名乗ったが受け付け表に無い端末
//
// 4 が 5 より先なのは、素性が分かっているほうが確定した事実だから。5 と 6 が 7 より
// 先なのは、どちらも「端末種別の話」ではないのに、そう言うと対処を誤らせるから
// (受け付け表に足しても直らない)。5 が 6 より先なのは、読めていないほうが分かって
// いることが少ないから。
// **AllJumpBlockedReasons の並びもこの順序**にしてある。
//
// この順序契約は switch を持つ jumpVerdictKind の契約でもある (jumpVerdict は
// kind / probe を空で渡す薄い委譲)。7 つ目を足したときは、こちらと
// jumpVerdictKind の両方を読むこと — 独立保守性レビューが「契約の本文が switch を
// 持たない関数の上に在る」と指摘した箇所。
// **本番からの呼び出しは無い** (受け口は applyJumpVerdicts 経由で
// jumpVerdictKind を直接呼ぶ)。これは kind / probe を空で渡す旧署名の試験用の
// 包み。独立保守性レビューが「順序契約の本文が、switch を持たないこの関数の上に
// 在る」と指摘したので、契約の読み先を上のコメント末尾で jumpVerdictKind に
// 向けてある。順序を変えるときに直すのは jumpVerdictKind の switch。
func jumpVerdict(envBlocked string, remote bool, pid int, harness string) (bool, string) {
	return jumpVerdictKind(envBlocked, remote, pid, harness, "", "")
}

// jumpVerdictKind は素性 (interactive / bg) も見る版 (e-7263)。
//
// 素性は端末種別より **前** に見る: バックグラウンド実行は端末を持たないので、
// 端末種別が何であろうと飛べない。順序を逆にすると、bg のセッションに
// 「この端末は未対応」という直せそうな理由が付き、対処を誤らせる。
//
// kind が空 (= 素性が分からない道具) のときは従来どおり端末種別だけで判定する。
// 「分からない」を bg 扱いにして飛べなくするのは、知らないことを根拠に機能を
// 止めることになる。
func jumpVerdictKind(envBlocked string, remote bool, pid int,
	harness, kind, probe string) (bool, string) {
	switch {
	case envBlocked != "":
		return false, envBlocked
	case remote:
		return false, JumpBlockedRemoteMachine
	case pid <= 0:
		return false, JumpBlockedNoPID
	case kind == sessionKindBackground:
		return false, JumpBlockedBackgroundSession
	case probe == harnessProbeUnreadable && !jumpableHarness(harness):
		// 読めなかった **かつ** 名乗りでも解決しない。名乗りで解決するなら
		// 読めなかったことは問題にならないので、そちらを優先する。
		return false, JumpBlockedProcessUnreadable
	case probe == harnessProbeUnnamed && !jumpableHarness(harness):
		// 読めたが端末を名乗っていない。上と同じ形: 名乗りで解決するならそちらが
		// 優先。表に足しても直らないので unsupported-terminal には落とさない。
		return false, JumpBlockedTerminalUnnamed
	case !jumpableHarness(harness):
		return false, JumpBlockedUnsupportedTerminal
	}
	return true, ""
}

// sessionKindBackground は Claude Code の台帳が「端末の中で動いていない」
// セッションに付ける素性。綴りは台帳 (~/.claude/sessions/<pid>.json の kind) の
// 値そのもの — tests/... ではなく viewer/local_sessions.go が読み元。
const sessionKindBackground = "bg"

// resolveHarnessesWith は一覧の各行の端末種別を、名乗りが特定していない行だけ
// プロセスに訊いて埋める (e-7261)。**判定の前に走らせる**: jumpVerdict は
// 「対応端末か」で最終 1 値を決めるので、埋める前に判定すると、名乗りが無い行が
// 一律で落ちる (または以前のように既定値で嘘の true になる)。
//
// 訊くのは **別マシンでなく pid が在る行だけ**。別マシンのプロセスはこの機械の
// ps で覗けないので、訊いても必ず空が返る = 無駄な ps を人数分走らせることになる。
// 名乗りが特定済みの行も訊かない (resolveHarnessWith が先に返す)。実測では
// 11 行中 4 行だけが対象。
func resolveHarnessesWith(run probeRunner, sessions []SessionOverview) {
	// **キャッシュ無しは、キャッシュ有りの薄い包み** (e-7266)。
	//
	// 以前は同じループ (別マシン / pid 無しを飛ばす選り分けと、読めなかった印の
	// 付け方) が 2 箇所に在った。本番が呼ぶのは Cached 側だけで、試験は非本番側を
	// 固定していたので、**選り分けを変えると本番の退行が試験に出ない**形だった
	// (独立保守性レビュー)。resolve は c == nil を既に扱えるので、包みで足りる。
	resolveHarnessesCached(run, nil, sessions)
}

// harnessProbeUnreadable は「プロセスを読めなかった」を表す行の印 (e-7265)。
const harnessProbeUnreadable = "unreadable"

// harnessProbeUnnamed は「読めたが端末を名乗っていなかった」を表す行の印。
// unreadable と別の印にしてある: 対処が違う (あちらは待てば変わりうる、こちらは
// 起動の仕方か、端末の外に居る)。空 = 読めて名乗った、または訊く必要が無かった。
const harnessProbeUnnamed = "unnamed"

// rowMark は導出結果を行の印に写す (e-7266)。
//
// int enum (probeResult) と行の印 (string) の変換が 2 つのループに手書きされて
// いたので 1 箇所に寄せた。4 値目を足したとき、変換を直し忘れても型が通って
// しまう形だった (独立保守性レビュー)。
//
// **3 値すべてに case を書く** (独立レビュー AX / 保守性が 2 体とも指摘)。以前は
// probeUnreadable だけを見て、残り 2 値 (読めた / 読めたが端末の外) を同じ空文字に
// 畳んでいた。probeNoTerminal は定義も試験も在ったのに、ここで消えるので判定には
// 届かず、画面は「この端末は未対応」と誤診していた —— 畳んではならないと 3 箇所の
// コメントで言いながら、変換の 1 箇所で畳んでいた。case を省略した値が黙って空に
// なるのを防ぐため、既定は「新しい値は未配線」と名指しして落とす形にしてある。
func (r probeResult) rowMark() string {
	switch r {
	case probeOK:
		return ""
	case probeNoTerminal:
		return harnessProbeUnnamed
	case probeUnreadable:
		return harnessProbeUnreadable
	}
	// 4 値目を足して case を書き忘れたとき、空 (= 読めた) に化けるのが最も危ない
	// (飛べるように見える)。印としては読めなかった側に倒す — 飛べないほうへ倒せば
	// 誤って飛ぶことはなく、理由の文面で気付ける。
	return harnessProbeUnreadable
}

// applyJumpVerdicts は一覧の全行に最終判定を書き込む。envBlocked はこの盤 (サーバ)
// の環境ブロック理由 (jumpEnvBlocked の結果、空 = 環境は OK)。組み立て
// (assembleSessions) はサーバの環境を知らない純関数なので、環境を知っている
// 受け口側がここで確定させる。
func applyJumpVerdicts(sessions []SessionOverview, envBlocked string) {
	for i := range sessions {
		sessions[i].Jumpable, sessions[i].JumpBlocked = jumpVerdictKind(
			envBlocked, sessions[i].Remote, sessions[i].PID,
			sessions[i].Harness, sessions[i].Kind, sessions[i].HarnessProbe)
	}
}

// jumpableHarnessKinds は対応端末の別名を全部並べる (テスト / ドキュメント用)。
// テストはこれを回して網羅するので、テーブルに足した別名がテスト漏れしない。
func jumpableHarnessKinds() []string {
	var out []string
	for _, d := range terminalDrivers {
		out = append(out, d.kinds...)
	}
	return out
}

// terminalAppName は前面化対象の端末アプリの表示名 (見つからない時のメッセージ用)。
// これは表示名であって harness の値ではない (harness に "Terminal.app" は渡せない)。
func terminalAppName(harness string) string {
	if d := driverForHarness(harness); d != nil {
		return d.appName
	}
	return "Terminal.app"
}

// iTerm2JumpScript は tty を持つ iTerm2 のタブを前面化する AppleScript を組む
// (純関数)。iTerm2 は window → tab → session の 3 段で、session が tty を持つ。
//
// **2 段構成にしてある理由** (e-7219): 以前は windows をなめている最中に `select w`
// していた。`select` は窓を前面に出す = windows の並びを変えるので、進行中の `w`
// (= `window 3` のような相対参照) が無効になり「正しくないインデックスです
// (-1719)」で落ちる。詳しい経緯は appleTerminalJumpScript を参照 (同じ型の破れ)。
//
// 第 1 段は**読むだけ**で窓の id (整数) とセッションの id (UUID、安定) を拾い、
// ループを抜ける。第 2 段はその id の**絶対参照**で解決し直してから選択する。
// 窓の選択は最後に 1 回だけ行う。
//
// **第 2 段のタブ側も、持ち回った参照を使わない** (独立 AX レビュー指摘、e-7219)。
// 以前は `select t` の直後に `select s` としていたが、`s` は `t` 相対の参照なので
// 「タブの選択はタブの並びを変えない」という前提に寄りかかっていた。その前提は
// 実測では成り立つ (iTerm2 3.7.6、3 タブの窓でタブ 2 → タブ 3 を選択しても
// `tabs` の順序は不変) が、**版が変われば崩れうる前提をコードの正しさの根拠に
// しない**。いまは `select t` の後に `current tab of tw` から id で引き直すので、
// タブが並び替わっても必ず同じセッションを指す。
//
// `select t` 自体は省けない (実測: セッションだけを `select` してもタブは
// 切り替わらず、current session は元のままだった)。だから「タブを選ぶ」と
// 「セッションを選ぶ」の 2 手は必要で、その間に隙を作らないのがこの形。
func iTerm2JumpScript(tty string) string {
	dev := "/dev/" + tty
	return fmt.Sprintf(`tell application "iTerm2"
  -- 第 1 段: 読むだけ。ここで select すると windows の並びが変わり、
  -- なめている最中の相対参照が無効になって -1719 で落ちる (e-7219)。
  set wid to missing value
  set sid to ""
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is %[1]q then
          set wid to id of w
          set sid to id of s
          exit repeat
        end if
      end repeat
      if sid is not "" then exit repeat
    end repeat
    if sid is not "" then exit repeat
  end repeat
  if sid is "" then return false
  -- 第 2 段: id の絶対参照で解決し直してから選択する。窓の選択 (= 並び替えを
  -- 起こす唯一の操作) は最後に 1 回だけ。
  set tw to (first window whose id is wid)
  repeat with t in tabs of tw
    set hit to false
    repeat with s in sessions of t
      if id of s is sid then
        select t
        select (first session of (current tab of tw) whose id is sid)
        set hit to true
        exit repeat
      end if
    end repeat
    if hit then exit repeat
  end repeat
  select tw
  activate
  return true
end tell`, dev)
}

// jumpScriptBuilder は端末種別に応じた AppleScript 組み立て関数を返す。
// 未対応の種別は fallback として理由付きエラーを返す (SPEC 方針4: 落とさない)。
func jumpScriptBuilder(harness string) (func(tty string) string, error) {
	if d := driverForHarness(harness); d != nil {
		return d.script, nil
	}
	return nil, fmt.Errorf(
		"この端末 (%s) への前面化は未対応です — 作業フォルダを手で開いてください", harness)
}

// appleScriptIndexErrorCode は AppleScript の「正しくないインデックスです」の
// エラー番号。窓やタブをなめている最中に並びが変わったときに出るので、利用者には
// 「並び順によって飛べる時と飛べない時がある」と見える (e-7219 の症状そのもの)。
const appleScriptIndexErrorCode = "-1719"

// osascriptFailureDetail は osascript の失敗から、画面に出して意味のある文面を作る。
//
// **なぜ必要か** (e-7219): `exec.ExitError` を `%v` で出すと "exit status 1" しか
// 得られない。AppleScript が何に困ったかは標準エラーに書かれているので、それを
// 拾わないと画面からは原因が一切辿れない。実際、並び順起因の失敗が 500 として
// 出ていたが、文面からは並び順の問題だと読み取れなかった。
//
// さらに -1719 だけは**平叙文の説明を添える**。番号とローカライズされた
// 「正しくないインデックスです」だけでは、読み手が「窓やタブの並びが変わったせい」
// までは辿れない。前面化は 2 段構成にして並び替えと衝突しないようにしたが、別の
// 端末アプリや将来の版で再発したときに、原因が文面から分かる状態を残しておく。
func osascriptFailureDetail(err error) string {
	detail := err.Error()
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if d := strings.TrimSpace(string(ee.Stderr)); d != "" {
			detail = d
		}
	}
	if strings.Contains(detail, appleScriptIndexErrorCode) {
		detail += " — 端末の窓やタブの並びが、探している最中に変わったようです" +
			" (前面化の途中で並び替えが起きた)。もう一度試すと通ることがあります"
	}
	return detail
}

// jumpFailure は前面化に失敗した理由と、HTTP でどう伝えるべきかの status。
//
// 400 は「入力を直せば通るかもしれない」。それ以外は status 自体が
// 「入力を直しても無駄」を伝える: 501 = 環境起因 (OS / 端末種別が未対応)、
// 404 = 指した実体が見つからない (プロセス / タブ)、500 = 実行失敗 (e-6429)。
type jumpFailure struct {
	Status int
	Msg    string
}

func (e *jumpFailure) Error() string { return e.Msg }

// jumpToTerminal は pid のセッションが動いている端末ウィンドウを前面化する。
//
// harness は端末の種類。未対応の種類は **エラーで落とさず理由を返す** (SPEC 方針4)。
func jumpToTerminal(pid int, harness string) error {
	return jumpToTerminalWith(defaultRunner, execProbeRunner, runtime.GOOS, pid, harness)
}

// jumpToTerminalWith は実行器と OS を差し替え可能にした本体 (テスト可能)。
// 失敗はすべて *jumpFailure で返し、受け口が HTTP status に写せるようにする。
func jumpToTerminalWith(run commandRunner, probe probeRunner,
	goos string, pid int, harness string) error {
	if goos != "darwin" {
		return &jumpFailure{http.StatusNotImplemented,
			"端末の前面化はこの OS では未対応です (現状 macOS のみ)"}
	}
	// 端末種別を確定させてからスクリプトを選ぶ (e-7261)。画面は一覧で配った
	// harness をそのまま送ってくるので、**一覧側で導出していても**ここで再確定
	// する: 古いタブ / API を直接叩く呼び出し元という別の入口が残っており、
	// 片方だけ塞ぐと「どの経路から来たか」で挙動が割れる。
	// プローブは **専用の継ぎ目** で呼ぶ。`run` (osascript / tty 用) を渡すと型が
	// 合わないので、一覧経路とジャンプ経路で上限の有無が割れることが起きない
	// (e-7266 — それが実際に起きていた)。
	harness, probeRes := resolveHarnessWith(probe, pid, harness)

	// **プローブの結果を捨てない** (e-7267)。
	//
	// 一覧の判定には process-unreadable を足したのに、押したときのこの経路では
	// `harness, _ =` と結果を投げ捨てていた。その結果、プロセスが読めなかった
	// (たとえば押す直前にそのセッションが閉じられた) ときにも
	// 「この端末 () への前面化は未対応です」という **端末種別の話** が返っていた。
	//
	// 実測 (2026-11-03): 一覧に居た pid がその後終了していて、押したらこの文が
	// 出た。一覧側で分けた区別が、人が実際に操作する経路で使われていない
	// —— 「1 経路だけ塞いで構造を閉じたと言う」の、同じ日に 4 度目の再発。
	if probeRes == probeUnreadable && !jumpableHarness(harness) {
		return &jumpFailure{http.StatusNotFound, fmt.Sprintf(
			"プロセス %d を読めませんでした (すでに終了した / 権限が無い / "+
				"確認が時間内に終わらなかった)。少し待って一覧を読み直すと"+
				"変わることがあります", pid)}
	}
	// 未対応端末は環境起因 = 501。
	build, err := jumpScriptBuilder(harness)
	if err != nil {
		return &jumpFailure{http.StatusNotImplemented, err.Error()}
	}
	tty, err := resolveTTYWith(run, pid)
	if err != nil {
		// 無効 pid (入力起因 = 400) は resolveTTYWith が *jumpFailure で返す
		// (ガードはあちらが唯一)。それ以外 = プロセスが消えている / 制御端末を
		// 持たない = 指した実体が見つからない (404)。
		var jf *jumpFailure
		if errors.As(err, &jf) {
			return jf
		}
		return &jumpFailure{http.StatusNotFound, err.Error()}
	}
	out, err := run("osascript", "-e", build(tty))
	if err != nil {
		return &jumpFailure{http.StatusInternalServerError,
			fmt.Sprintf("端末の前面化に失敗しました: %s", osascriptFailureDetail(err))}
	}
	if strings.TrimSpace(string(out)) != "true" {
		return &jumpFailure{http.StatusNotFound, fmt.Sprintf(
			"その端末 (tty %s) を持つ %s のタブが見つかりませんでした "+
				"(別の端末アプリで開いている可能性)", tty, terminalAppName(harness))}
	}
	return nil
}
