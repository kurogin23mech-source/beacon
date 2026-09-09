// 対象 1 件の詳細 — 画面でトグルを開いたときだけ取りに行くもの。
//
// なぜ盤 (ビュー用スキーマ) に含めないか
// ------------------------------------
// 盤は自動更新で数秒ごとに取り直す。Beacon 本体には記録が 3,900 件あり、これを毎回
// 流すのは無駄が大きい。そして盤の形は Python 版と共有する契約なので、片方だけ項目を
// 足すと突き合わせが壊れる。
//
// 俯瞰は盤、掘り下げは要求されたときだけ、と分ける。これなら共有の契約を変えずに済み、
// 自動更新も軽いままになる。
package main

import "sort"

// WorkItemRow は対象配下の記録 1 件 (タスク / コミット / PR / 保存)。
type WorkItemRow struct {
	ID          string        `json:"id"`
	Type        string        `json:"type"`
	Status      string        `json:"status"`
	Description string        `json:"description"`
	Priority    string        `json:"priority"`
	IsDone      bool          `json:"is_done"`
	Children    []WorkItemRow `json:"children"`
}

// TargetDetail は対象 1 件分の掘り下げ。
type TargetDetail struct {
	ID    string        `json:"id"`
	Label string        `json:"label"`
	Items []WorkItemRow `json:"items"`
	// Counts は盤に出ている消化数と同じ数え方。画面で突き合わせられるように添える。
	Counts WorkItems `json:"counts"`
}

// isDoneEntry は記録が終わっているかを判定する。数え方は countTaskStatus と同じ
// 規則に従う (PR だけ終わり方が違う)。ここで独自の判定を作らない。
func isDoneEntry(e Entry) bool {
	if e.Type == "pr" {
		return prDoneStatuses[e.Status]
	}
	return e.Status == statusDone || e.Status == statusCancelled
}

func workItemRows(entries []Entry) []WorkItemRow {
	rows := make([]WorkItemRow, 0, len(entries))
	for _, e := range entries {
		rows = append(rows, WorkItemRow{
			ID:          e.ID,
			Type:        e.Type,
			Status:      e.Status,
			Description: e.Description,
			Priority:    e.Priority,
			IsDone:      isDoneEntry(e),
			Children:    workItemRows(e.Entries),
		})
	}
	return rows
}

// BuildTargetDetail は対象 1 件の詳細を組み立てる。
// 見つからなければ nil を返す (呼び出し側が「その対象は無い」と答える)。
func BuildTargetDetail(p *Project, id string) *TargetDetail {
	for _, ms := range p.Milestones {
		if ms.ID != id {
			continue
		}
		total, done := countTaskStatus(ms.Entries)
		open := total - done
		if open < 0 {
			open = 0
		}
		items := workItemRows(ms.Entries)
		// 未消化を先に見せる。開いた人が知りたいのは「まだ何が残っているか」で
		// あって、終わったものの一覧ではないため。
		sort.SliceStable(items, func(i, j int) bool {
			return !items[i].IsDone && items[j].IsDone
		})
		return &TargetDetail{
			ID:     ms.ID,
			Label:  targetLabel(ms),
			Items:  items,
			Counts: WorkItems{Total: total, Done: done, Open: open},
		}
	}
	return nil
}
