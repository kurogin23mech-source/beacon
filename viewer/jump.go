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
	"errors"
	"fmt"
	"net/http"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
)

// commandRunner はコマンド実行を差し替え可能にする継ぎ目 (テストで stub するため)。
// これが無いと ps / osascript を実際に叩くしか検証手段がなく、エラー処理の変更を
// 局所フィードバックで確かめられない。
type commandRunner func(name string, args ...string) ([]byte, error)

// defaultRunner は実プロセスを起動する既定の実行器。
func defaultRunner(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).Output()
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
	// 生の形は **この rawNames との完全一致でだけ** 受ける — kinds の汎用別名
	// ("" / "terminal") や短縮形 ("iterm") には落とさない。未知の端末が偶然
	// `terminal:Terminal` と名乗ったとき、既知端末に化けて前面化を試み、的外れな
	// 回復ヒントを出す経路を閉じるため (PR#820 独立 AX レビュー)。
	rawNames []string
	// script は tty を持つタブを前面化する AppleScript を組む純関数。
	script func(tty string) string
	// appName は見つからない時のメッセージに出す表示名。
	appName string
}

// terminalDrivers は対応端末の一覧 (= 受け付け側の唯一の正典)。空文字 "" は種類不明で、
// macOS の既定端末 Terminal.app とみなす。ここに無い端末 (kitty / VS Code / tmux 等) は
// 前面化に未対応で、画面では fallback (パスのコピー) に回す。
//
// 名乗り側 (channel/bus-harness-kind.mjs の TERM_PROGRAM_KINDS) の各行
// `TERM_PROGRAM → 正典名` は、**同じ 1 つの driver** の rawNames (= 小文字の
// TERM_PROGRAM) と kinds (= 正典名) に対応していなければならない。別名を変える /
// 端末を足すときは両方を同じ変更で直す — tests/test_harness_kind_parity_e7199.py が
// 片方だけの変更や、別の driver への置き間違いを赤にする (e-7199: 片方にだけ iTerm2
// が在って実機で一度も飛べなかった)。
var terminalDrivers = []terminalDriver{
	{
		kinds:    []string{"", "apple-terminal", "terminal", "apple_terminal"},
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

// driverForHarness は harness に対応する driver を返す (無ければ nil)。
//
// 生の形は rawNames との完全一致でだけ受け、kinds の別名には当てない (逆も同じ)。
// 2 つの照合面を混ぜると、未知の端末の生の名乗りが汎用別名 ("" / "terminal") に
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
)

// jumpVerdict は「このセッションへ飛べるか」の最終 1 値と、飛べない理由を返す
// (純関数)。判定の真実源はここ 1 つで、画面は結果を読むだけ。
// envBlocked はこの盤の環境ブロック (jumpEnvBlocked の結果、空 = 環境は OK)。
// 理由は影響範囲の広い順に選ぶ: 環境 (盤全体) → マシン → プロセス → 端末種別。
func jumpVerdict(envBlocked string, remote bool, pid int, harness string) (bool, string) {
	switch {
	case envBlocked != "":
		return false, envBlocked
	case remote:
		return false, JumpBlockedRemoteMachine
	case pid <= 0:
		return false, JumpBlockedNoPID
	case !jumpableHarness(harness):
		return false, JumpBlockedUnsupportedTerminal
	}
	return true, ""
}

// applyJumpVerdicts は一覧の全行に最終判定を書き込む。envBlocked はこの盤 (サーバ)
// の環境ブロック理由 (jumpEnvBlocked の結果、空 = 環境は OK)。組み立て
// (assembleSessions) はサーバの環境を知らない純関数なので、環境を知っている
// 受け口側がここで確定させる。
func applyJumpVerdicts(sessions []SessionOverview, envBlocked string) {
	for i := range sessions {
		sessions[i].Jumpable, sessions[i].JumpBlocked = jumpVerdict(
			envBlocked, sessions[i].Remote, sessions[i].PID, sessions[i].Harness)
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
// 窓の選択は最後に 1 回だけ行う —— 並び替えを起こす操作はそれだけなので、
// タブとセッションの選択を先に済ませておけば並び替えと衝突しない。
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
        select s
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
	return jumpToTerminalWith(defaultRunner, runtime.GOOS, pid, harness)
}

// jumpToTerminalWith は実行器と OS を差し替え可能にした本体 (テスト可能)。
// 失敗はすべて *jumpFailure で返し、受け口が HTTP status に写せるようにする。
func jumpToTerminalWith(run commandRunner, goos string, pid int, harness string) error {
	if goos != "darwin" {
		return &jumpFailure{http.StatusNotImplemented,
			"端末の前面化はこの OS では未対応です (現状 macOS のみ)"}
	}
	// 端末種別で前面化スクリプトを選ぶ。未対応端末は環境起因 = 501。
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
