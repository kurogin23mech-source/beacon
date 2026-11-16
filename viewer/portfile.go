package main

// 同じプロジェクトの盤を二重に立てないための控え (ms-170 e-7147)。
//
// **なぜ要るのか**: `listen` は希望の口が塞がっていれば黙って別の空き口を借りる。
// そのため `beacon view` を呼ぶたびに新しいサーバーが増え、ランダムな口に散る。
// 2026-10-25 に 11 本・約 207MB が常駐しているのが見つかり、最古は 2 日 23 時間
// 動き続けていた。2026-11-02 に測り直しても、3 回開くと 3 本増えた (50701 /
// 50708 / 50713)。使うたびに積み上がるので、長く開発する人ほど端末が重くなる。
//
// **置き場所をプロジェクトの中にしない理由**: `.beacon/` は書き込めない場所に
// 置かれていることがあり (e-6369)、そこはプロジェクトのデータを置く場所でもある。
// 控えは「この機械でいま何が動いているか」という手元の状態なので、一時置き場に
// プロジェクトの道のハッシュで鍵を付けて置く (`invert_harness.lock_path_for` と
// 同じ作法)。
//
// **控えだけでは足りない理由**: プロセスが落ちても控えは残る (stale)。さらに、
// その口が別の何かに再利用されていることもある。だから控えを読んだあと必ず
// **実際に問い合わせて、同じプロジェクトを配っているか**を確かめる。番号が生きて
// いるかだけでは、別人の口を自分の盤と見間違える。

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// boardRecord は「この道のための盤が、この口と番号で動いている」という控え。
type boardRecord struct {
	Port      int    `json:"port"`
	PID       int    `json:"pid"`
	Host      string `json:"host"`
	Path      string `json:"path"`       // 配っているプロジェクトの道 (.beacon の親)
	StartedAt string `json:"started_at"` // 人が読むため (判定には使わない)
}

// resolvePath は道を 1 つの綴りに揃える (絶対化 + symlink の解決)。
//
// **存在しない道も揃える**のが要点。`EvalSymlinks` は無い道では失敗するので、
// 素朴に書くと「存在する側だけ解決され、存在しない側は元のまま」になり、同じ
// 場所が別物に見える。macOS の一時置き場 (`/var/folders` → `/private/var/...`)
// で実際に起きた: 盤が配っている `.beacon` は実在するので解決され、こちらが
// 組み立てた道は解決されず、再利用の判定が外れて盤がもう 1 本立つ —— 直そうと
// している「使うたびに増える」に戻る形。
//
// そこで **実在する一番深い祖先まで遡って解決し、残りを繋ぎ直す**。
func resolvePath(p string) string {
	if p == "" {
		return ""
	}
	if abs, err := filepath.Abs(p); err == nil {
		p = abs
	}
	p = filepath.Clean(p)
	rest := ""
	cur := p
	for {
		if real, err := filepath.EvalSymlinks(cur); err == nil {
			return filepath.Clean(filepath.Join(real, rest))
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			// 根まで遡っても解決できない (= その木が無い)。絶対化だけで返す。
			return p
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
}

// portfilePath は、その道のための控えの置き場所を返す。
//
// 道を揃えてからハッシュを取る。同じ場所を別の綴り (symlink / 相対) で開いた
// ときに別の盤が立つと、再利用の意味が無くなる。
func portfilePath(projectPath string) string {
	resolved := resolvePath(projectPath)
	sum := sha1.Sum([]byte(resolved))
	key := hex.EncodeToString(sum[:])[:12]
	return filepath.Join(os.TempDir(), "beacon-view-"+key+".json")
}

// readPortfile は控えを読む。無い / 壊れていれば nil を返す (どちらも「控えなし」)。
func readPortfile(path string) *boardRecord {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var rec boardRecord
	if err := json.Unmarshal(raw, &rec); err != nil {
		return nil
	}
	if rec.Port <= 0 {
		return nil
	}
	return &rec
}

// writePortfile は控えを置く。置けなくても盤は成立するので、失敗は返すだけで
// 呼び出し側は止めない (控えが無い状態は「再利用できない」であって「壊れた」では
// ない)。
func writePortfile(path string, rec boardRecord) error {
	raw, err := json.MarshalIndent(rec, "", " ")
	if err != nil {
		return err
	}
	// 同じ場所に書く途中で読まれても壊れた JSON を見せないよう、別名で書いて差し替える。
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// probeTimeout は問い合わせの待ち時間。手元の口なので短くてよい。長く待つと、
// 死んだ控えが残っているときに起動が目に見えて遅くなる。
var probeTimeout = 1500 * time.Millisecond

// liveBoardURL は控えの盤が **いま生きていて同じプロジェクトを配っているか** を
// 確かめ、生きていればその住所を返す。使えなければ空文字列。
//
// 確かめる順に意味がある:
//  1. 番号が生きているか — 安い。落ちていればここで終わる。
//  2. 口が応えるか — 番号が生きていても別物に入れ替わっていることがある。
//  3. 配っている道が同じか — ここを見ないと、同じ機械の別プロジェクトの盤を
//     自分の盤と見間違える (手元で複数のプロジェクトを開くのは普通のこと)。
func liveBoardURL(rec *boardRecord, wantPath string) string {
	if rec == nil {
		return ""
	}
	if rec.PID > 0 && !processAlive(rec.PID) {
		return ""
	}
	host := rec.Host
	if host == "" {
		host = loopbackHost
	}
	addr := net.JoinHostPort(host, strconv.Itoa(rec.Port))
	client := &http.Client{Timeout: probeTimeout}
	resp, err := client.Get("http://" + addr + "/api/state")
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	var state struct {
		Path     string `json:"path"`
		CWD      string `json:"cwd"`
		ReuseKey string `json:"reuse_key"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		return ""
	}
	// 席 (reuse_key) と、いま配っている道 (path) の **両方** を見る。
	//
	// `path` は取得元をクラウドに切り替えると空になるので、それだけで判定すると
	// 「どの席でもない」と読んでしまう (ms-170 思想 review medium)。だから席を足した。
	//
	// **ただし席だけを信じてはいけない** (ms-170 AX review high)。盤には
	// `/api/open` という、動いたまま **ローカルの別プロジェクトに切り替える**
	// 経路が前から在る。席は起動時に決めて動かないので、切り替えた後の盤は
	// 「席 = projA / 配っている道 = projB」になる。席だけを見ると、projA を
	// 求めた相手に **projB を配っている盤** を成功扱いで渡してしまう。私の変更前は
	// `path` 比較でこれを検出できていたので、席を無条件に優先させたのは回帰だった。
	//
	// そこで **食い違ったら一致と見なさない** (fail closed)。
	//   - 席だけ在る (= クラウドに切り替えた盤): 席で判定する
	//   - 両方在る: 両方が一致していなければ使わない
	//   - 席が無い (= 古い盤): `path` で判定する
	// 使わないと判断した側は新しい盤が立つだけなので、害は「1 本増える」に留まる。
	// 逆に誤って渡すと、求めたのと違うプロジェクトのデータを見せることになる。
	if state.ReuseKey != "" && !sameBoardPath(state.ReuseKey, wantPath) {
		return ""
	}
	if state.Path != "" && !sameBoardPath(state.Path, wantPath) {
		return ""
	}
	if state.ReuseKey == "" && state.Path == "" {
		// どちらも名乗らない盤は、どの席のものか分からない。
		return ""
	}
	return fmt.Sprintf("http://%s:%d/", host, rec.Port)
}

// sameBoardPath は「配っている先が同じか」を判定する。
//
// `/api/state` の `path` は `.beacon` ディレクトリを指し、こちらが知っているのは
// その親 (プロジェクトの道) のこともあるので、どちらの綴りでも一致と見なす。
// 空同士は一致にしない —— データの無い場所で開いた盤を、別の場所で開こうとした
// 盤と同一視すると、見たい場所と違う盤に繋がる。
func sameBoardPath(got, want string) bool {
	if got == "" || want == "" {
		return false
	}
	g, w := resolvePath(got), resolvePath(want)
	if g == w {
		return true
	}
	// 片方が .beacon を含む形のときは、その親とも突き合わせる。
	if filepath.Base(g) == ".beacon" && filepath.Dir(g) == w {
		return true
	}
	if filepath.Base(w) == ".beacon" && filepath.Dir(w) == g {
		return true
	}
	return false
}

// parseBoardPIDs は `番号 名前` の並び (ps / tasklist を揃えた形) から
// beacon-view の番号を拾う (純関数、両 OS で同じ規則を使う)。
//
// 名前は前方一致で見る。配布物は `beacon-view-darwin-arm64` /
// `beacon-view.exe` / `beacon-view` と揃っていないので、完全一致にすると
// 環境によって数えられない。自分自身は除く (数えると常に 1 本居ることになる)。
func parseBoardPIDs(psOutput string, selfPID int) []int {
	var out []int
	for _, line := range strings.Split(psOutput, "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		if len(fields) < 2 {
			continue
		}
		pid, err := strconv.Atoi(fields[0])
		if err != nil || pid == selfPID {
			continue
		}
		name := fields[len(fields)-1]
		if i := strings.LastIndexAny(name, `/\`); i >= 0 {
			name = name[i+1:]
		}
		if strings.HasPrefix(strings.ToLower(name), "beacon-view") {
			out = append(out, pid)
		}
	}
	return out
}

// reuseDecision は「既に動いている盤を使うか」の判定 (純関数)。
//
// **main() から出した理由** (e-7147 保守性 review high): 判定が `main()` の中に
// あると、どの試験も通らない。`--no-reuse` を無効化する変異を当てても
// `go test ./...` は緑のままだった (独立レビューが実測)。部品 (`liveBoardURL` /
// `portfilePath`) は個別に試験されていても、**それをどう組み合わせて呼ぶか**という
// 配線はどこにも固定されていなかった。idle.go の `shouldClose` と同じく、判定を
// 純関数にして時計や入出力を注入する形に揃える。
type reuseDecision struct {
	URL    string // 空でなければ、この住所の盤を使う
	Reason string // なぜ使わないと決めたか (人向け。使う場合は空)
}

// decideReuse は、再利用してよいなら住所を入れた判定を返す。
//
// `portExplicit` は「利用者が口を明示したか」。**値が既定と同じかどうかでは
// 近似しない** (e-7147 保守性 review medium): `--port 7377` と既定値を明示的に
// 渡した人も「そこに立てたい」と言っているので、値比較だと意思を取り違える。
// 明示されたかは `flag.Visit` でしか分からないので、呼び出し側が渡す。
func decideReuse(noReuse, portExplicit bool, reuseKey, portfile string,
	readRec func(string) *boardRecord, liveURL func(*boardRecord, string) string,
) reuseDecision {
	if noReuse {
		return reuseDecision{Reason: "--no-reuse が指定されました"}
	}
	if portExplicit {
		return reuseDecision{Reason: "--port が指定されました (そこに立てる意思表示)"}
	}
	url := liveURL(readRec(portfile), reuseKey)
	if url == "" {
		return reuseDecision{Reason: "使える盤が見つかりませんでした"}
	}
	return reuseDecision{URL: url}
}

// flagWasSet は、その旗が実際にコマンドラインで渡されたかを返す。
// `flag.Visit` は **渡された旗だけ** を巡るので、既定値と同じ値を明示された
// 場合も正しく「渡された」と分かる。
func flagWasSet(visit func(func(*flag.Flag)), name string) bool {
	found := false
	visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
}

// shouldWritePortfile は、控えを書き換えてよいかを返す (純関数)。
//
// **なぜ要るのか** (e-7147 AX review high): 以前は無条件に書いていた。`--no-reuse`
// や `--port` 明示で再利用を飛ばした盤も書くので、**生きている盤の控えを上書き**
// してしまう。上書きされた盤は再利用の仕組みから永久に見えなくなり、次に開いた人
// は新しい盤を立てる —— つまり「使うたびに増える」が戻る。この PR が閉じようと
// している状態そのもの。しかも検証手順 (scripts/verify-on-running-board.sh) が
// `--no-reuse` を使うので、確かめるたびに本番の盤を孤児にしていた。
//
// 規則は「席を取っている生きた盤が自分でないなら、触らない」。最後に書いた者が
// 勝つ形をやめる。
func shouldWritePortfile(existing *boardRecord, reuseKey string, selfPID int,
	liveURL func(*boardRecord, string) string) (bool, string) {
	if existing == nil {
		return true, ""
	}
	if existing.PID == selfPID {
		return true, ""
	}
	if liveURL(existing, reuseKey) == "" {
		// 死んでいる / 別物に入れ替わっている / 別プロジェクトを配っている。
		// どれも席が空いているのと同じなので、自分の控えに書き換えてよい。
		return true, ""
	}
	return false, fmt.Sprintf(
		"このプロジェクトの盤は既に %d 番が配っているので、控えはそのままにします "+
			"(上書きすると、その盤が再利用から見えなくなります)", existing.PID)
}

// trackedBoardPIDs は、控えに載っていて **いま生きている** 盤の番号を集める。
//
// 置き場所を総なめするので、別プロジェクトの盤も入る。居残り (= 誰の控えにも
// 載っていない盤) を見分けるために使う (e-7147 AX review misleading)。
func trackedBoardPIDs(dir string, liveURL func(*boardRecord, string) string) map[int]string {
	out := map[int]string{}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "beacon-view-") || !strings.HasSuffix(name, ".json") {
			continue
		}
		rec := readPortfile(filepath.Join(dir, name))
		if rec == nil || rec.PID <= 0 {
			continue
		}
		if liveURL(rec, rec.Path) != "" {
			out[rec.PID] = rec.Path
		}
	}
	return out
}
