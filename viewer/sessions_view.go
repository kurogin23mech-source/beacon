// 全セッション横断の一覧 (ms-171)。
//
// 盤はプロジェクト 1 つを見る面だが、こちらは逆で **このマシンで動いている作業を
// 全部並べて、それぞれがどのプロジェクトの何をやっているかを見る面**。
//
// 各セッションについて、次の順に辿る:
//
//	作業フォルダ → 上へさかのぼって .beacon を探す → どのプロジェクトか
//	ブランチ名 / 直近のコミット件名        → どの対象 (マイルストーン) か
//	直近のコミット件名                     → どのタスクか
//
// **どれも推測を含む。** Beacon に名乗っているセッションはサーバが担当を持って
// いるので確かだが、名乗っていないものは手元の手がかりから推し量るしかない。
// 何を根拠にそう言っているかを必ず添えて、確度が分かるようにする。
//
// 読み取り専用。
package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// ProjectRef はセッションが属するプロジェクト。
type ProjectRef struct {
	Name      string `json:"name"`
	Dir       string `json:"dir"`
	ProjectID string `json:"project_id"` // クラウドに結び付いていれば入る
}

// Attribution は「何をやっているか」1 件と、その根拠。
//
// Source は次のいずれか:
//
//	beacon … Beacon に名乗っており、サーバが持っている担当 (確か)
//	branch … ブランチ名からの推測
//	commit … 直近のコミット件名からの推測
type Attribution struct {
	ID     string `json:"id"`
	Label  string `json:"label"`
	Source string `json:"source"`
}

// SessionOverview は一覧の 1 行。
type SessionOverview struct {
	Tool       string       `json:"tool"`
	Name       string       `json:"name"`
	State      string       `json:"state"`
	Title      string       `json:"title"`
	Directory  string       `json:"directory"`
	Branch     string       `json:"branch"`
	LastActive string       `json:"last_active"`
	// Running は「このセッションは生きているか」。**真値源が 2 つある** (ms-171 e-6431):
	// 名乗りの主 (claimer) の行では transport の live (サーバ判定、ローカルのプロセス
	// 検出を上書き)、それ以外の行では手元のプロセス生存 (processAlive)。消費側
	// (NeedsAttention / page.html の停止判定) はこの合成後の値を読む。
	// **「ローカルにプロセスがある」の意味だけで局所推論しないこと** — 別マシン /
	// クラウドの名乗り行も true になりうる。出自の分離 (別フィールド化) は follow-up。
	Running     bool `json:"running"`
	ToolRunning bool `json:"tool_running"`
	Project    *ProjectRef  `json:"project,omitempty"`
	Target     *Attribution `json:"target,omitempty"`
	Task       *Attribution `json:"task,omitempty"`
	// Activity は「今このセッションが何をしているか」の要約 (ms-159 が produce する)。
	// **このビューワーは作らない。消費するだけ。** 空は「分からない」であって
	// 「何もしていない」ではないので、運用室では空欄として描く (でっち上げない)。
	Activity string `json:"activity,omitempty"`
	// Named は Beacon に名乗っているか。
	//
	// **送れるのはこれが真のものだけ。** 名乗っていないセッションには、外から
	// 文面を渡す口が無い (または非公開・認証で塞がっている)。
	Named bool `json:"named"`
	// SessionID は名乗っているセッションの識別子 (送信の宛先)。
	SessionID string `json:"session_id,omitempty"`
	// PID はこのマシンで動いているセッションのプロセス番号 (分かる場合)。
	//
	// **端末へ飛ぶ (jump-to-terminal) の起点。** 手元で拾ったセッションだけが持つ。
	// 別マシンのセッション (Remote) は 0 で、そのマシンの端末は前面化できない。
	PID int `json:"pid,omitempty"`
	// Harness は端末の種類。どの端末アプリを前面化するかの分岐に使う。名乗っている
	// セッションだけが持つ (サーバが解決)。受け付ける値は jump.go の terminalDrivers が
	// 唯一の正典 (apple-terminal / terminal / apple_terminal / iterm2 / iterm / iterm.app)。
	// 空は不明 = Terminal.app 既定扱い。ここに無い値は前面化に未対応 (Jumpable=false)。
	Harness string `json:"harness,omitempty"`
	// Jumpable は「端末へ飛ぶ」に対応した端末か (= Go が harness から計算した判定)。
	//
	// **画面はこの値を読むだけ。** 対応端末の許可集合を JS 側に複製すると Go と
	// ドリフトするため、判定は Go (jump.go) を唯一の正典にして結果だけを渡す。
	Jumpable bool `json:"jumpable"`
	// Machine は動いている機械 (名乗っているセッションのみ分かる)。
	Machine string `json:"machine,omitempty"`
	// Who は動かしている人 (名乗っているセッションのみ分かる)。
	Who string `json:"who,omitempty"`
	// Remote は **このマシン以外** で動いているか。
	//
	// 名乗っているセッションはどのマシンのものも名簿に載るので、手元の記録には
	// 無いものが出てくる。手元のものと混ぜると「このマシンで動いている」と
	// 誤解させるため、区別できるようにする。
	Remote bool `json:"remote"`
}

// SessionsView は一覧 1 面分。
type SessionsView struct {
	Sessions []SessionOverview `json:"sessions"`
	// NoProject は、どのプロジェクトにも紐づけられなかった数。
	// **黙って落とさない** ため、数だけでも伝える。
	NoProject int `json:"no_project"`
	// Scope は実際に適用した表示範囲 (self / attention / all)。
	// **絞り込みが起きたことを応答自身が名乗る** ため常に載せる。これが無いと、
	// 呼び出し側 (画面・AI・自動化) は「全部返ってきた」のか「self に絞られた」のかを
	// 区別できず、他マシン / 他人のセッションを『存在しない』と誤読する (e-6401 レビュー)。
	Scope string `json:"scope"`
	// Total は絞り込み前の総数、Shown は絞り込み後に返した数。
	// 差があれば「隠したものがある」と呼び出し側が機械的に検知できる。
	Total int `json:"total"`
	Shown int `json:"shown"`
	// RosterStatus は名簿 (= 名乗っているセッションの一覧、生存の真値) を取れたか。
	//
	// **取得失敗を空名簿と混同してはいけない** (AX review high, 2026-09-14)。名簿は
	// ms-171 で「生存の真値 + 24h カットオフ免除の根拠」に昇格したので、取得に失敗
	// (未 login / token 失効 / 通信断) すると、bus-live なセッションが無信号で一覧から
	// 消える (カットオフに落ち、稼働表示も失う) — この MS が直した cairn-sales 消失の
	// 別経路での再発。値は:
	//   ok          … 名簿を取得できた (空でも「誰も名乗っていない」で正しい)
	//   unavailable … 取得に失敗した (名乗っているセッションが抜けている可能性)
	//   n/a         … このプロジェクトはクラウドに結び付いておらず名簿が原理的に無い
	// 画面は unavailable を空一覧と別表示にして、見えない事実を隠さない。
	RosterStatus string `json:"roster_status"`
}

// roster status の値 (真実源はここ 1 つ)。
const (
	RosterOK          = "ok"
	RosterUnavailable = "unavailable"
	RosterNA          = "n/a"
)

// AllSessions は、このマシンで動いている作業セッションを全部集めて紐づける。
//
// ``named`` は Beacon に名乗っているセッションの名簿 (取れていれば渡す)。
// 名簿と突き合わせて、確かな担当が分かるものはそちらを優先する。
func AllSessions(since time.Duration, now time.Time,
	named []SessionRow) SessionsView {

	rows := []LocalSessionRow{}
	home, err := os.UserHomeDir()
	if err != nil {
		return SessionsView{Sessions: []SessionOverview{}}
	}
	rows = append(rows, claudeSessions(home)...)
	rows = append(rows, opencodeSessions(home)...)
	rows = append(rows, codexSessions(home)...)
	return assembleSessions(rows, named, since, now)
}

// assembleSessions は、集めたローカルの記録と名簿を紐づけて一覧を組み立てる純関数。
//
// ディスク走査 (AllSessions) から切り離してあるのは、**生存判定 (Gate A) を注入した
// 入力で機械的に確かめられるようにするため** (ms-171 e-6431)。手元の記録は環境依存で
// 実際の ~/.claude を用意しないと出せないので、ここを純関数にして試験可能にする。
func assembleSessions(rows []LocalSessionRow, named []SessionRow,
	since time.Duration, now time.Time) SessionsView {

	// 名簿を「作業フォルダ + 道具」で引けるようにする。**カットオフより先に作る** —
	// bus-live かどうかを、古いものを落とす前に知る必要があるため (下記 Gate A)。
	//
	// **フォルダだけで引いてはいけない。** 同じフォルダで Codex や OpenCode を
	// 何本も動かしていると、その全部が bclaude セッションの識別子を貰ってしまい、
	// 名乗っていないセッションが名乗っているように見える。送信先にも選べてしまい、
	// 押すと無関係のセッション宛に飛ぶ (2026-09-11 に観測)。
	namedByDir := map[string]SessionRow{}
	for _, n := range named {
		if n.Cwd != "" {
			namedByDir[namedKey(n.Cwd, n.Agent)] = n
		}
	}

	// 古いものを落とす。ただし **生存の真値は transport の live** (ms-171 e-6431)。
	//
	// 生存 (= 名簿の live) は liveness の transport 次元 = `ws_live` (期限内の WebSocket
	// 接続) OR `poll_health.healthy` (bridge の polling が新鮮) の union で、サーバが
	// session ごとに判定する (CORE doc liveness-three-dimensions §1)。名簿に載る =
	// transport live なので、手元の会話ログが何時間前でも「生きている」。会話が止まって
	// いるだけの armed / 受信待ちのセッションを、会話時刻の 24h カットオフで取りこぼして
	// はいけない (cairn-sales が運用室に出なかった実バグ、2026-09-13)。
	//
	// **「heartbeat」は生存の真値ではない**。`heartbeat_fresh` は attention 次元 (人/AI が
	// 能動的に見ているか) の弱いシグナルで、live には畳み込まない (同 doc §3、回帰防止)。
	// ここでの生存判定は transport の live 一本で、heartbeat_fresh は使わない。
	//
	// 名乗っていないセッションには transport live が無いので、会話時刻が唯一の生存の
	// 手がかり。そちらにだけ 24h カットオフを当てる (痕跡ログで溢れさせない)。
	//
	// **新しい順に先に並べてから畳む** — 突合キー namedKey(dir, tool) はフォルダ + 道具
	// 粒度なので、同じフォルダで同じ道具を複数本動かすと 1 つの live 名簿に複数のローカル
	// 痕跡が当たる。全部を live 扱いすると、同フォルダの死んだ痕跡まで「稼働中」に見える
	// 偽稼働が生じる (原典が嫌う silent 非機能の鏡像、思想レビュー finding 2026-09-14)。
	// session 単位の identity が手元に無い環境では、**キーあたり最新 1 行だけ** を live
	// 名簿の主 (claimer) とし、残りの痕跡は名乗り扱いにしない (= 通常のカットオフ + 実際の
	// プロセス生存で扱う)。
	sort.SliceStable(rows, func(i, j int) bool {
		return rows[i].LastActive > rows[j].LastActive
	})
	cutoff := now.Add(-since)
	kept := []LocalSessionRow{}
	claimedCutoff := map[string]bool{}
	for _, r := range rows {
		if r.Directory == "" {
			continue
		}
		key := namedKey(r.Directory, r.Tool)
		_, inRoster := namedByDir[key]
		// live 名簿の主になれるのは、そのキーで最初に来た (= 最新の) 1 行だけ。
		claimer := inRoster && !claimedCutoff[key]
		if claimer {
			claimedCutoff[key] = true
		}
		if !claimer {
			// 名乗りの主でない行 (= 未名乗り、または同フォルダの古い重複) は
			// 会話時刻のカットオフに従う。
			if t, err := time.Parse(time.RFC3339, r.LastActive); err == nil {
				if t.Before(cutoff) {
					continue
				}
			}
		}
		kept = append(kept, r)
	}

	lookup := newProjectLookup()
	// 手元の記録と突き合わせ済みの名簿を覚えておく (二重に並べないため)。
	seenNamed := map[string]bool{}
	// 名乗りの主 (claimer) を、キーあたり最新 1 行に限る (cutoff loop と同じ規則)。
	// kept は新しい順なので、各キーで最初に出会う行が主。
	claimedNamed := map[string]bool{}
	out := SessionsView{Sessions: []SessionOverview{}}
	for _, r := range kept {
		o := SessionOverview{
			Tool: r.Tool, Name: r.Name, State: r.State, Title: r.Title,
			Directory: r.Directory, LastActive: r.LastActive,
			Running: r.Running, ToolRunning: r.ToolRunning,
			Branch: gitBranch(r.Directory),
			PID:    r.PID, // 手元で拾ったセッションのプロセス番号 (端末へ飛ぶ起点)
		}

		proj := lookup.forDir(r.Directory)
		o.Project = proj
		if proj == nil {
			out.NoProject++
		}

		subject := gitHeadSubject(r.Directory)

		// 名乗っているセッションかどうかは、担当の有無とは別に記録する。
		// **名乗りの主はキーあたり最新 1 行だけ** — 同フォルダの古い重複に live の
		// 識別子や稼働を漏らさない (思想レビュー finding 2026-09-14、上の claimer 規則)。
		key := namedKey(r.Directory, r.Tool)
		n, inRoster := namedByDir[key]
		isNamed := inRoster && !claimedNamed[key]
		o.Named = isNamed
		if isNamed {
			claimedNamed[key] = true
			o.SessionID = n.ID
			// 名簿に居る = transport が live (ws_live OR poll_health) = 確かに生きて
			// いる (ms-171 e-6431 / CORE doc liveness-three-dimensions §1)。手元の
			// プロセス検出が拾い漏れても、transport live が生存の真値。ここで確定させ
			// ないと、画面が「停止」と判定して既定の隠しに巻き込まれ、救った行がまた
			// 消える。**主 (claimer) の 1 行にだけ与える** ので、死んだ重複には漏れない。
			o.Running = true
			// DM の宛先はそのセッション自身のプロジェクト (e-6396)。名簿が持つ
			// ProjectID を権威として載せる — 手元の .beacon から引けなかったり、
			// 別プロジェクトのフォルダで作業していても、正しいプロジェクト宛に送れる。
			if n.ProjectID != "" {
				if o.Project == nil {
					// 手元の .beacon から引けなかった (別マシン由来のフォルダ等)。
					// 宛先に要る ProjectID だけでも載せる (名前は不明のまま)。
					o.Project = &ProjectRef{ProjectID: n.ProjectID}
				} else if o.Project.ProjectID == "" {
					o.Project.ProjectID = n.ProjectID
				}
			}
		}

		// 担当は、サーバが解決した値があるときだけ「確か」として扱う。
		//
		// **プロジェクトの進行中マイルストーン (focus) を担当にしてはいけない。**
		// 全セッションが同じ値になり、別の対象で作業していても正しく見えてしまう。
		// Beacon 自身も lib/working_target.py でそう戒めている。
		if isNamed && n.Target != "" {
			o.Target = &Attribution{
				ID: n.Target, Label: n.TargetLabel, Source: "beacon"}
		} else if id := targetFromBranch(o.Branch); id != "" {
			o.Target = &Attribution{ID: id, Source: "branch"}
		} else if id := targetFromBranch(subject); id != "" {
			// ブランチが main のままでも、コミット件名に対象が入っていることがある。
			o.Target = &Attribution{ID: id, Source: "commit"}
		}

		// タスクはコミット件名からしか辿れない (名簿も持っていないことが多い)。
		if id := taskFromSubject(subject); id != "" {
			o.Task = &Attribution{ID: id, Source: "commit"}
		}

		if isNamed {
			o.Machine = n.Machine
			o.Who = n.Who
			o.Activity = n.Activity
			o.Harness = n.Harness
		}

		// 端末へ飛べるかを Go 側で確定させる (画面は結果を読むだけ)。
		o.Jumpable = jumpableHarness(o.Harness)
		// 分かった ID に、読める名前を与える。
		lookup.decorate(proj, &o)
		out.Sessions = append(out.Sessions, o)
		if isNamed {
			seenNamed[key] = true
		}
	}

	// 名乗っているセッションのうち、手元の記録に無いものを足す。
	//
	// **これが他のマシンで動いている bclaude セッション。** 名簿には載るが、この
	// マシンには痕跡が無いので、手元の記録を並べるだけでは一覧から丸ごと抜ける。
	for _, n := range named {
		if seenNamed[namedKey(n.Cwd, n.Agent)] {
			continue
		}
		o := SessionOverview{
			Tool:       n.Agent,
			Name:       n.Who,
			Directory:  n.Cwd,
			Branch:     n.Branch,
			LastActive: n.LastActive,
			Running:    n.Live, // 名簿の稼働はサーバの心拍なので確か
			Named:      true,
			SessionID:  n.ID,
			Machine:    n.Machine,
			Who:        n.Who,
			Activity:   n.Activity,
			Harness:    n.Harness,
			Remote:     true,
		}
		// 別マシンの名乗りセッションにも、宛先ルーティング用に自分のプロジェクトを
		// 載せる (e-6396)。手元に痕跡が無いので名前は引けないが、ProjectID は名簿が
		// 持っている。これが無いと DM が「今見ているプロジェクト」に誤ルートする。
		if n.ProjectID != "" {
			o.Project = &ProjectRef{ProjectID: n.ProjectID}
		}
		if n.Target != "" {
			o.Target = &Attribution{
				ID: n.Target, Label: n.TargetLabel, Source: "beacon"}
		}
		out.Sessions = append(out.Sessions, o)
	}

	// 新しい順に並べ直す (足した分が末尾に付いたままにならないように)。
	sort.SliceStable(out.Sessions, func(i, j int) bool {
		return out.Sessions[i].LastActive > out.Sessions[j].LastActive
	})
	return out
}

// namedKey は名簿と手元の記録を突き合わせる鍵。
// 場所が同じでも道具が違えば別のセッション。
func namedKey(dir, tool string) string {
	return normalisePath(dir) + "	" + strings.ToLower(strings.TrimSpace(tool))
}

func normalisePath(p string) string {
	return strings.ToLower(strings.TrimRight(strings.ReplaceAll(p, "\\", "/"), "/"))
}

// taskFromSubject はコミット件名からタスクの番号を拾う。
// この プロジェクトでは "feat(ms-170): … (e-6342)" のように書く慣習がある。
var taskPattern = regexp.MustCompile(`(?i)\b(e-\d+)`)

func taskFromSubject(subject string) string {
	m := taskPattern.FindStringSubmatch(subject)
	if m == nil {
		return ""
	}
	return strings.ToLower(m[1])
}

// gitHeadSubject は直近のコミットの件名を返す。
//
// git が入っていない機械では空を返す (best-effort)。ここが取れなくても一覧は出る。
func gitHeadSubject(dir string) string {
	if _, err := exec.LookPath("git"); err != nil {
		return ""
	}
	cmd := exec.Command("git", "-C", dir, "log", "-1", "--pretty=%s")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// --- プロジェクトの解決 ----------------------------------------------------

// projectLookup は作業フォルダからプロジェクトを引く。
//
// 同じプロジェクトのセッションが何本もあるので、一度読んだら覚えておく。
// プロジェクトのデータは大きい (Beacon 本体で 10MB 近い) ため、繰り返し読むと重い。
type projectLookup struct {
	mu    sync.Mutex
	refs  map[string]*ProjectRef // .beacon の場所 → プロジェクト
	datas map[string]*Project    // .beacon の場所 → 中身 (名前を引くため)
}

func newProjectLookup() *projectLookup {
	return &projectLookup{
		refs:  map[string]*ProjectRef{},
		datas: map[string]*Project{},
	}
}

// forDir は作業フォルダから上へさかのぼって .beacon を探す。
func (l *projectLookup) forDir(dir string) *ProjectRef {
	src, err := OpenLocal(dir)
	if err != nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if ref, ok := l.refs[src.BeaconDir]; ok {
		return ref
	}
	ref := &ProjectRef{
		Dir:       filepath.Dir(src.BeaconDir),
		ProjectID: src.CloudProjectID(),
	}
	data, err := src.Load()
	if err == nil && data != nil {
		ref.Name = data.Name
		l.datas[src.BeaconDir] = data
	}
	if ref.Name == "" {
		// 名前が読めなくても、どこのフォルダかは伝える。
		ref.Name = filepath.Base(ref.Dir)
	}
	l.refs[src.BeaconDir] = ref
	return ref
}

// decorate は、拾った ID に読める名前を与える。
// プロジェクトが分からない / 中身が読めない場合は ID のままにする。
func (l *projectLookup) decorate(ref *ProjectRef, o *SessionOverview) {
	if ref == nil {
		return
	}
	l.mu.Lock()
	data := l.datas[filepath.Join(ref.Dir, ".beacon")]
	l.mu.Unlock()
	if data == nil {
		return
	}
	for _, ms := range data.Milestones {
		if o.Target != nil && o.Target.Label == "" && ms.ID == o.Target.ID {
			o.Target.Label = targetLabel(ms)
		}
		if o.Task != nil && o.Task.Label == "" {
			if label := findEntryLabel(ms.Entries, o.Task.ID); label != "" {
				o.Task.Label = label
			}
		}
	}
}

// --- 運用室の絞り込み (ms-173 e-6401) --------------------------------------
//
// 並列に走るセッションは容易に数十〜数百になる。全部を等しく並べると、いま自分が
// 気にすべきものが埋もれる。そこで表示範囲を絞れるようにする:
//
//	自分のみ (既定) … 他人のセッションを畳んで、自分の並列作業だけを見る
//	要対応のみ       … 生きているのに道具が止まっている = 手が要りそうなものだけ
//	全て             … 他人のものも含め全部
//
// **絞り込みは「隠す」であって「消す」ではない。** 既定を自分にするのは多人数の
// ノイズを畳むためで、他人の作業が存在しないと誤解させないよう、切り替えられる。
// (ms-159 D スライスの「モデルは多ユーザ対応 / 既定表示は自分」に対応。)
//
// プロジェクト (root) 絞りはここには無い。選択肢を全プロジェクト分そろえたまま
// 切り替えたいので画面側 (page.html) の責務にしてある — 絞りの真実源を 1 つに保つ。

// 運用室の表示範囲の許容値。**受理する scope の真実源はここ 1 つ** — 画面・API の
// 検証も doc も、この定数と KnownScope から引く (値を増やすときはここだけ直す)。
const (
	ScopeSelf      = "self"      // 自分のセッションのみ (既定)
	ScopeAttention = "attention" // 生きているのに道具が止まっている = 手が要りそうなもの
	ScopeAll       = "all"       // 他人のものも含め全部
)

// KnownScope は scope が受理できる表示範囲かを返す。
// 空文字は「未指定」で、既定 (self) に倒す側の判断は呼び出し側 (API ハンドラ) が持つ
// ため、ここでは false を返す (= 空 と 既定 self を混同しない)。
func KnownScope(scope string) bool {
	switch scope {
	case ScopeSelf, ScopeAttention, ScopeAll:
		return true
	}
	return false
}

// SessionFilter は運用室の表示範囲の条件。
type SessionFilter struct {
	// Scope は表示範囲。受理値は KnownScope / Scope* 定数を参照 (真実源はそこ)。
	// 空 / 未知値はこの純関数では既定 (self) に倒す — 未知値の拒否は表面 (API) の責務。
	Scope string `json:"scope"`
}

// NeedsAttention は「人の手が要りそう」か。
//
// 生きている (Running) のに道具が動いていない (!ToolRunning) = 待機中で、返事待ちの
// 可能性がある。名乗っているだけの別マシンのセッションは道具の状態が手元に無く、
// 既定の false を「待機中」と取り違えて全部 要対応 に挙げてしまうため、対象外にする。
//
// **これは手がかりであって断定ではない。** ms-159 が activity を出し始めたら、
// もっと確かな「返事待ち」の判定に寄せられる。それまでの近似。
func NeedsAttention(s SessionOverview) bool {
	return s.Running && !s.ToolRunning && !s.Remote
}

// isSelf は自分のセッションか。
//
// 名乗っている (Who が分かる) セッションは、その持ち主が自分かで判定する。
// 名乗っていないセッションはこのマシンで拾ったものなので、定義上いつも自分。
// 自分の identity が分からない (未ログイン等) ときは、このマシンの分だけを自分とみなす。
func isSelf(s SessionOverview, selfEmail string) bool {
	if strings.TrimSpace(s.Who) == "" {
		return true // このマシンで拾った、名乗っていないセッション = 自分
	}
	if strings.TrimSpace(selfEmail) == "" {
		return !s.Remote // identity 不明なら、このマシンの分だけを自分とみなす
	}
	return strings.EqualFold(strings.TrimSpace(s.Who), strings.TrimSpace(selfEmail))
}

// FilterSessions は運用室の表示範囲を適用する (純関数、入力は変更しない、並び順は保つ)。
//
// **"attention" は誰の attention かを問わない** — 生きていて道具が止まっている
// セッションを持ち主に関係なく拾う (自分に絞りたい場合は "self" を選ぶ。self と
// attention の掛け合わせは提供しない = 表示範囲は 1 つ選ぶ形にして語義を単純に保つ)。
func FilterSessions(sessions []SessionOverview, f SessionFilter,
	selfEmail string) []SessionOverview {

	out := make([]SessionOverview, 0, len(sessions))
	for _, s := range sessions {
		switch f.Scope {
		case ScopeAttention:
			if !NeedsAttention(s) {
				continue
			}
		case ScopeAll:
			// 全部通す
		default: // ScopeSelf と空 / 未知値は「自分のみ」に倒す (既定)
			if !isSelf(s, selfEmail) {
				continue
			}
		}
		out = append(out, s)
	}
	return out
}

// findEntryLabel は配下の記録から、その番号の説明を再帰的に探す。
func findEntryLabel(entries []Entry, id string) string {
	for _, e := range entries {
		if strings.EqualFold(e.ID, id) {
			return e.Description
		}
		if label := findEntryLabel(e.Entries, id); label != "" {
			return label
		}
	}
	return ""
}
