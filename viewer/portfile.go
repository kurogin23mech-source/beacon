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
		Path string `json:"path"`
		CWD  string `json:"cwd"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		return ""
	}
	if !sameBoardPath(state.Path, wantPath) {
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
