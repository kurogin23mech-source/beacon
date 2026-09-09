// 盤の組み立て — ビュー用スキーマ (doc 4aHU7n92YXQFEGuhuEbj) を作る層。
//
// Python 側 (lib/view_model.py) と **同じ形** を出すことが、この層の唯一の仕事。
// 形が食い違うと、同じ画面を出しているつもりで中身が違う、という一番たちの悪い
// 壊れ方をする。判断の根拠は Python 側の実装に合わせ、ここで独自の解釈を足さない。
//
// 移植したのは組み込みの開発職種の投影だけ (lib/core.py project_targets 相当)。
// 記述子で定義された対象クラスの一般化機構は移植していないが、**在ることは検知して
// 画面に伝える** (doc lM9gHMOAm8VK2xZGKfEe)。黙って省略すると、利用者は見えて
// いないことに気づけない。
package main

// スキーマの版。Python 側 (view_model.SCHEMA_VERSION) と揃える。
const SchemaVersion = 1

// 取得元。表示のためだけのラベルで、他の項目の中身には影響しない。
const (
	SourceLocal = "local"
	SourceCloud = "cloud"
)

// 記録の状態。Python 側 (work_model) と同じ値を使う。
const (
	statusDone      = "done"
	statusCancelled = "cancelled"
)

// 消化数に数える記録の種別。
var countedTypes = map[string]bool{
	"task": true, "commit": true, "pr": true, "save": true,
}

// PR は状態の進み方が他と違い、承認・取り込み・打ち切りも終わりとして数える。
// これを外すと、承認済みの PR がいつまでも進捗に乗らない (Beacon 側 e-2005)。
var prDoneStatuses = map[string]bool{
	"done": true, "cancelled": true,
	"approved": true, "merged": true, "closed": true,
}

// Board はビュー用スキーマ 1 件分。項目名は Python 側と 1 文字も違えない。
type Board struct {
	SchemaVersion int            `json:"schema_version"`
	Project       BoardProject   `json:"project"`
	Progress      Counts         `json:"progress"`
	Targets       []TargetRow    `json:"targets"`
	Deliverables  []any          `json:"deliverables"`
	Documents     []DocumentRow  `json:"documents"`
	Sessions      []SessionRow   `json:"sessions"`
	Source        Source         `json:"source"`
	Unsupported   *Unsupported   `json:"unsupported,omitempty"`
}

type BoardProject struct {
	Name       string `json:"name"`
	Objective  string `json:"objective"`
	Summary    string `json:"summary"`
	Profession string `json:"profession"`
}

type Counts struct {
	Total int `json:"total"`
	Done  int `json:"done"`
	Open  int `json:"open"`
}

type WorkItems struct {
	Total int `json:"total"`
	Done  int `json:"done"`
	Open  int `json:"open"`
}

type TargetRow struct {
	ID        string         `json:"id"`
	Label     string         `json:"label"`
	Status    string         `json:"status"`
	Kind      string         `json:"kind"`
	WorkItems WorkItems      `json:"work_items"`
	IsDone    bool           `json:"is_done"`
	IsOpen    bool           `json:"is_open"`
	Detail    map[string]any `json:"detail"`
}

type DocumentRow struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Scope     string `json:"scope"`
	UpdatedAt string `json:"updated_at"`
	Target    string `json:"target"`
}

type SessionRow struct {
	ID          string `json:"id"`
	Who         string `json:"who"`
	Machine     string `json:"machine"`
	Agent       string `json:"agent"`
	Cwd         string `json:"cwd"`
	Target      string `json:"target"`
	TargetLabel string `json:"target_label"`
	Live        bool   `json:"live"`
	Healthy     bool   `json:"healthy"`
	LastActive  string `json:"last_active"`
	// 画面で「具体的に今何を書いているか」を出すための項目 (doc lM9gHMOAm8VK2xZGKfEe)。
	Branch      string `json:"branch"`
	HeadSubject string `json:"head_subject"`
}

type Source struct {
	Kind      string `json:"kind"`
	ProjectID string `json:"project_id"`
}

// Unsupported は「この版では投影できなかったもの」。**空でないなら必ず画面に出す。**
// 黙って省略すると、利用者は見えていないことに気づけない。
type Unsupported struct {
	TargetClassCount int      `json:"target_class_count"`
	TargetClassNames []string `json:"target_class_names"`
	Reason           string   `json:"reason"`
}

// targetLabel は対象の表示名を寛容に読む (lib/work_model.py target_label と同じ)。
// 正式な label を先に見て、無ければ旧い呼び名に落ちる。
func targetLabel(ms Milestone) string {
	if ms.Label != "" {
		return ms.Label
	}
	if ms.Title != "" {
		return ms.Title
	}
	return ms.Name
}

// countTaskStatus は配下の記録を再帰的に数え、(総数, 終わった数) を返す。
// lib/core.py count_task_status と同じ規則。
func countTaskStatus(entries []Entry) (total, done int) {
	for _, e := range entries {
		if countedTypes[e.Type] {
			total++
			if e.Type == "pr" {
				if prDoneStatuses[e.Status] {
					done++
				}
			} else if e.Status == statusDone || e.Status == statusCancelled {
				done++
			}
		}
		t, d := countTaskStatus(e.Entries)
		total += t
		done += d
	}
	return total, done
}

// projectTargets は開発職種の対象を、職種によらない形に写す
// (lib/core.py project_targets と同じ)。取消済みは既定の見え方に合わせて省く。
func projectTargets(p *Project) []TargetRow {
	rows := make([]TargetRow, 0, len(p.Milestones))
	for _, ms := range p.Milestones {
		if ms.Status == statusCancelled {
			continue
		}
		total, done := countTaskStatus(ms.Entries)
		status := ms.Status
		if status == "" {
			status = "todo"
		}
		open := total - done
		if open < 0 {
			open = 0
		}
		rows = append(rows, TargetRow{
			ID:     ms.ID,
			Label:  targetLabel(ms),
			Status: status,
			Kind:   "milestone",
			WorkItems: WorkItems{Total: total, Done: done, Open: open},
			IsDone: status == statusDone,
			IsOpen: status != statusDone && status != statusCancelled,
			Detail: map[string]any{
				"progress": ms.Progress,
				// null は null のまま渡す (Python 側と 1 バイトも違えないため)。
				"target_date": derefOrNil(ms.TargetDate),
				"priority":    ms.Priority,
			},
		})
	}
	return rows
}

// unsupportedClasses は、この版が投影できない対象クラスを拾う。
// 記述子で定義されたクラス (Beacon 側 ms-122) は移植範囲外なので、
// 「無かったこと」にせず件数と名前を持ち帰る。
func unsupportedClasses(p *Project) *Unsupported {
	if len(p.TargetClasses) == 0 {
		return nil
	}
	names := make([]string, 0, len(p.TargetClasses))
	for _, tc := range p.TargetClasses {
		// 人が読む画面に出すので、表示名を優先し、無ければ種別名で代える。
		if tc.Label != "" {
			names = append(names, tc.Label)
		} else if tc.Kind != "" {
			names = append(names, tc.Kind)
		}
	}
	sortStrings(names)
	return &Unsupported{
		TargetClassCount: len(names),
		TargetClassNames: names,
		Reason: "このビューワーは組み込みの対象クラスのみ表示します。" +
			"記述子で定義された対象クラスは一覧に含まれていません。",
	}
}

// devDeliverables は開発職種が生み出す価値の投影。
//
// Python 側 (occupation.project_deliverables) は採用している対象クラスを歩いて
// 組み立てるが、開発職種では結果が固定なので、ここでは同じ値を直接置く。
//
// **Python 側を変えたらここも変える。** 気づかずズレることを防ぐため、両者の出力を
// 突き合わせる試験 (tests/test_go_viewer_parity_ms170.py) を用意してある。
func devDeliverables() []any {
	return []any{
		map[string]any{
			"target_class": "milestone",
			"kind":         "feature-map",
			"label":        "機能",
			"projector":    "changelog",
			"ref":          "",
		},
	}
}

// BuildBoard は盤を 1 面組み立てる。
//
// 取得元 (source) は表示のためだけのラベルで、**他のどの項目の中身も変えない**。
// 画面が取得元で描き分けたら、それはこの層の失敗 (親 SPEC 受入条件 2)。
func BuildBoard(p *Project, source, projectID string,
	documents []DocumentRow, sessions []SessionRow) *Board {

	rows := projectTargets(p)
	counts := Counts{Total: len(rows)}
	for _, r := range rows {
		if r.IsDone {
			counts.Done++
		}
		if r.IsOpen {
			counts.Open++
		}
	}

	profession := p.Profession
	if profession == "" {
		profession = "dev" // 未設定は開発として扱う (Beacon 本体の既定と揃える)
	}

	// 無いものは空の一覧で渡す。画面に「有るか無いか」の分岐をさせない。
	if documents == nil {
		documents = []DocumentRow{}
	}
	if sessions == nil {
		sessions = []SessionRow{}
	}

	return &Board{
		SchemaVersion: SchemaVersion,
		Project: BoardProject{
			Name:       p.Name,
			Objective:  p.Objective,
			Summary:    p.Summary,
			Profession: profession,
		},
		Progress:     counts,
		Targets:      rows,
		Deliverables: deliverablesFor(profession),
		Documents:    documents,
		Sessions:     sessions,
		Source:       Source{Kind: source, ProjectID: projectID},
		Unsupported:  unsupportedClasses(p),
	}
}

// derefOrNil は文字列の有無をそのまま保つ。値が無ければ null として出す。
// deliverablesFor は職種に応じた「生み出した価値」の投影を返す。
// 移植したのは開発職種だけ。他の職種は空で返し、盤の形は保つ。
func deliverablesFor(profession string) []any {
	if profession == "dev" {
		return devDeliverables()
	}
	return []any{}
}

func derefOrNil(v *string) any {
	if v == nil {
		return nil
	}
	return *v
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}
