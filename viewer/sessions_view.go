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
	Running     bool `json:"running"`
	ToolRunning bool `json:"tool_running"`
	Project    *ProjectRef  `json:"project,omitempty"`
	Target     *Attribution `json:"target,omitempty"`
	Task       *Attribution `json:"task,omitempty"`
	// Named は Beacon に名乗っているか。
	//
	// **送れるのはこれが真のものだけ。** 名乗っていないセッションには、外から
	// 文面を渡す口が無い (または非公開・認証で塞がっている)。
	Named bool `json:"named"`
	// SessionID は名乗っているセッションの識別子 (送信の宛先)。
	SessionID string `json:"session_id,omitempty"`
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
}

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

	// 全プロジェクト横断なので、場所では絞らない。古いものだけ落とす。
	cutoff := now.Add(-since)
	kept := []LocalSessionRow{}
	for _, r := range rows {
		if r.Directory == "" {
			continue
		}
		if t, err := time.Parse(time.RFC3339, r.LastActive); err == nil {
			if t.Before(cutoff) {
				continue
			}
		}
		kept = append(kept, r)
	}
	sort.SliceStable(kept, func(i, j int) bool {
		return kept[i].LastActive > kept[j].LastActive
	})

	// 名簿を「作業フォルダ + 道具」で引けるようにする。
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

	lookup := newProjectLookup()
	// 手元の記録と突き合わせ済みの名簿を覚えておく (二重に並べないため)。
	seenNamed := map[string]bool{}
	out := SessionsView{Sessions: []SessionOverview{}}
	for _, r := range kept {
		o := SessionOverview{
			Tool: r.Tool, Name: r.Name, State: r.State, Title: r.Title,
			Directory: r.Directory, LastActive: r.LastActive,
			Running: r.Running, ToolRunning: r.ToolRunning,
			Branch: gitBranch(r.Directory),
		}

		proj := lookup.forDir(r.Directory)
		o.Project = proj
		if proj == nil {
			out.NoProject++
		}

		subject := gitHeadSubject(r.Directory)

		// 名乗っているセッションかどうかは、担当の有無とは別に記録する。
		n, isNamed := namedByDir[namedKey(r.Directory, r.Tool)]
		o.Named = isNamed
		if isNamed {
			o.SessionID = n.ID
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
		}

		// 分かった ID に、読める名前を与える。
		lookup.decorate(proj, &o)
		out.Sessions = append(out.Sessions, o)
		if isNamed {
			seenNamed[namedKey(r.Directory, r.Tool)] = true
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
			Remote:     true,
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
