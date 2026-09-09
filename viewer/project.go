// Package main — Beacon の盤を単体で表示するビューワー (ms-170)。
//
// このファイルはデータ層のうち **ローカル読み取り** を担う。Beacon が入っていない
// PC でも盤を見られるようにする、というのがこのビューワーの存在理由なので、
// beacon の CLI や Python には一切依存しない。
//
// 読み取り専用。このビューワーは Beacon のデータを書き換えない。
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	_ "modernc.org/sqlite" // 純 Go の SQLite (cgo 不要 = 各 OS 向けビルドが素直)
)

// Project は Beacon のプロジェクト 1 件分。必要な部分だけを型にし、残りは
// 触らずに持つ。ビューワーは読むだけなので、知らない項目を落としても害はない。
type Project struct {
	Name       string      `json:"name"`
	Objective  string      `json:"objective"`
	Summary    string      `json:"summary"`
	Profession string      `json:"profession"`
	Milestones []Milestone `json:"milestones"`

	// 記述子で定義された対象クラス。Go 版は投影しないが、**在ることは把握する**
	// (黙って省略しないため。doc lM9gHMOAm8VK2xZGKfEe)。
	TargetClasses []TargetClass `json:"target_classes"`
}

// TargetClass は記述子で定義された対象クラス (Beacon 側 ms-122)。
// このビューワーは投影しないので、名乗りに必要な分だけ読む。
type TargetClass struct {
	Kind  string `json:"kind"`
	Label string `json:"label"`
}

// Milestone は開発職種の対象 1 件。
type Milestone struct {
	ID         string  `json:"id"`
	Label      string  `json:"label"`
	Title      string  `json:"title"` // 旧い呼び名 (label が無いとき使う)
	Name       string  `json:"name"`  // さらに旧い呼び名
	Status     string  `json:"status"`
	Progress   int     `json:"progress"`
	// 元データには target_date が null の対象がある。空文字に丸めると Python 側の
	// 出力 (null のまま) と食い違うため、有無を保てる形で持つ。
	TargetDate *string `json:"target_date"`
	Priority   string  `json:"priority"`
	Entries    []Entry `json:"entries"`
}

// Entry はマイルストーン配下の記録 1 件 (タスク / コミット / PR / 保存など)。
// 入れ子になるため、自分と同じ型の子を持つ。
type Entry struct {
	ID          string  `json:"id"`
	Type        string  `json:"type"`
	Status      string  `json:"status"`
	Description string  `json:"description"`
	Priority    string  `json:"priority"`
	CreatedAt   string  `json:"created_at"`
	Entries     []Entry `json:"entries"`
}

// LocalSource はローカルの Beacon フォルダ。
type LocalSource struct {
	// BeaconDir は .beacon フォルダの場所。
	BeaconDir string
	// Kind は実際に読んだ先 ("sqlite" か "json")。画面には出さないが、
	// 読めなかったときの説明に使う。
	Kind string
}

// OpenLocal は与えられた場所から .beacon を見つける。
//
// 引数はプロジェクトの根 (.beacon を含むフォルダ) でも、.beacon そのものでもよい。
// 受け取った人が「どっちを渡せばいいか」で迷わないため。
func OpenLocal(path string) (*LocalSource, error) {
	if path == "" {
		path = "."
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	// .beacon そのものを渡された場合と、それを含むフォルダを渡された場合の両対応。
	// さらに、そこから上へ順にさかのぼって探す。プロジェクトの奥のフォルダで実行しても、
	// また実行ファイルを置いた場所から起動しても、その上に .beacon があれば見つかる。
	// (エクスプローラーからダブルクリックすると起動場所が実行ファイルの場所になるため、
	//  上へ探さないと「見つかりません」で終わってしまう)
	for dir := abs; ; {
		if hasProjectData(dir) {
			return &LocalSource{BeaconDir: dir}, nil
		}
		beacon := filepath.Join(dir, ".beacon")
		if st, err := os.Stat(beacon); err == nil && st.IsDir() && hasProjectData(beacon) {
			return &LocalSource{BeaconDir: beacon}, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break // 根まで来た
		}
		dir = parent
	}
	return nil, fmt.Errorf("%s とその上のフォルダに Beacon のデータが見つかりません。\n"+
		"  Beacon プロジェクトの場所を指定してください:\n"+
		"    viewer --path <.beacon があるフォルダ>", abs)
}

func hasProjectData(dir string) bool {
	for _, name := range []string{"project.db", "project.json"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err == nil {
			return true
		}
	}
	return false
}

// IsCloudLinked は、このフォルダがクラウドに結び付けられているかを返す。
//
// Beacon 本体はこの有無だけでローカルかクラウドかを決めている (lib/store.py)。
// ビューワーも同じ判定に従い、独自の規則を作らない。
func (s *LocalSource) IsCloudLinked() bool {
	_, err := os.Stat(filepath.Join(s.BeaconDir, "cloud.json"))
	return err == nil
}

// Load はプロジェクトを読み込む。SQLite を先に見て、無ければ旧い JSON を読む。
//
// 順序は Beacon 本体に合わせてある。ms-148 でローカルの真値が SQLite に移った
// ため、両方ある場合は SQLite が正しい。
func (s *LocalSource) Load() (*Project, error) {
	dbPath := filepath.Join(s.BeaconDir, "project.db")
	if _, err := os.Stat(dbPath); err == nil {
		p, err := loadFromSqlite(dbPath)
		if err == nil {
			s.Kind = "sqlite"
			return p, nil
		}
		// SQLite が読めなかった場合でも、旧い JSON があるなら見に行く。
		// 「盤が出ない」より「古いかもしれないが出る」ほうがましなため。
		jsonPath := filepath.Join(s.BeaconDir, "project.json")
		if _, statErr := os.Stat(jsonPath); statErr != nil {
			return nil, err
		}
	}
	p, err := loadFromJSON(filepath.Join(s.BeaconDir, "project.json"))
	if err != nil {
		return nil, err
	}
	s.Kind = "json"
	return p, nil
}

func loadFromJSON(path string) (*Project, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var p Project
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("%s を読めません: %w", path, err)
	}
	return &p, nil
}

// SQLite の表は kv(pk, sk, data) の 1 枚だけで、data は JSON。
// 種別はこの 3 つ (lib/store_sqlite.py と同じ)。
// 値は lib/store_sqlite.py の _PK_META / _PK_MILESTONE / _PK_ENTRY と一致させる。
// ここを取り違えると 1 行も拾えず、エラーも出ないまま「対象 0 件」になる。
const (
	pkMeta      = "meta"
	pkMilestone = "milestone"
	pkEntry     = "entry"
)

// loadFromSqlite は kv の行を読み、プロジェクトの形に組み立て直す。
//
// 組み立ての規則は lib/v3_schema.py assemble と同じ:
//   - META はプロジェクトの基本情報
//   - MILESTONE は sk がマイルストーンの ID
//   - ENTRY は sk が "{マイルストーンID}#{記録ID}"
//
// 開くのは読み取り専用。ビューワーが盤を書き換えないことを、接続の時点で保証する。
func loadFromSqlite(dbPath string) (*Project, error) {
	dsn := fmt.Sprintf("file:%s?mode=ro", filepath.ToSlash(dbPath))
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	rows, err := db.Query("SELECT pk, sk, data FROM kv")
	if err != nil {
		return nil, fmt.Errorf("%s を読めません: %w", dbPath, err)
	}
	defer rows.Close()

	var (
		metaRaw    []byte
		msRaw      = map[string][]byte{}
		msOrder    []string
		entriesRaw = map[string][][]byte{}
	)
	for rows.Next() {
		var pk, sk, data string
		if err := rows.Scan(&pk, &sk, &data); err != nil {
			return nil, err
		}
		switch pk {
		case pkMeta:
			metaRaw = []byte(data)
		case pkMilestone:
			if _, seen := msRaw[sk]; !seen {
				msOrder = append(msOrder, sk)
			}
			msRaw[sk] = []byte(data)
		case pkEntry:
			msID, _, found := strings.Cut(sk, "#")
			if !found || msID == "" {
				continue
			}
			entriesRaw[msID] = append(entriesRaw[msID], []byte(data))
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	var p Project
	if len(metaRaw) > 0 {
		if err := json.Unmarshal(metaRaw, &p); err != nil {
			return nil, fmt.Errorf("プロジェクトの基本情報を読めません: %w", err)
		}
	}
	// META に紛れ込んだ milestones は捨てる (組み立て直したものを正とする)。
	p.Milestones = nil

	// 並び順は組み立て直しなので、Python 側 (_ms_sort_key) と同じ規則で並べる。
	// 単純な文字列順にすると ms-1, ms-10, ms-100 の順になり、読む先によって
	// 盤の並びが変わってしまう。
	sortMilestoneIDs(msOrder)
	for _, msID := range msOrder {
		var ms Milestone
		if err := json.Unmarshal(msRaw[msID], &ms); err != nil {
			continue // 1 件読めなくても盤全体は出す
		}
		ms.Entries = nil // 行側に入っている残骸を捨てる
		for _, raw := range entriesRaw[msID] {
			var e Entry
			if err := json.Unmarshal(raw, &e); err != nil {
				continue
			}
			ms.Entries = append(ms.Entries, e)
		}
		sortEntries(ms.Entries)
		p.Milestones = append(p.Milestones, ms)
	}
	return &p, nil
}


var msIDPattern = regexp.MustCompile(`^ms-(\d+)$`)

// sortMilestoneIDs は ms-1, ms-2, ms-10 の順に並べる (Python の _ms_sort_key と同じ)。
// 数字として読めない ID は後ろにまとめ、その中では文字列順にする。
func sortMilestoneIDs(ids []string) {
	sort.SliceStable(ids, func(i, j int) bool {
		gi, ni := msIDOrder(ids[i])
		gj, nj := msIDOrder(ids[j])
		if gi != gj {
			return gi < gj
		}
		if gi == 0 {
			return ni < nj
		}
		return ids[i] < ids[j]
	})
}

func msIDOrder(id string) (group, num int) {
	if m := msIDPattern.FindStringSubmatch(id); m != nil {
		n, err := strconv.Atoi(m[1])
		if err == nil {
			return 0, n
		}
	}
	return 1, 0
}

// sortEntries は記録を作成時刻の順に並べる (Python の _entry_sort_key と同じ)。
// 作成時刻を持たないものは末尾へ送り、同着は ID で決める。
func sortEntries(entries []Entry) {
	sort.SliceStable(entries, func(i, j int) bool {
		ti, tj := entries[i].CreatedAt, entries[j].CreatedAt
		if ti == "" {
			ti = "￿" // Python 側が使う「一番後ろ」の目印と揃える
		}
		if tj == "" {
			tj = "￿"
		}
		if ti != tj {
			return ti < tj
		}
		return entries[i].ID < entries[j].ID
	})
}
