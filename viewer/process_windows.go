//go:build windows

package main

import (
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

// processRunning は、その名前のプロセスが動いているかを返す。
//
// 「いま動いているか」は、記録の更新時刻だけでは分からない (数分前に終わった
// セッションと、開いたまま止まっているセッションが区別できない)。
//
// 一覧の取得は毎回やると重いので、短い間だけ覚えておく。
func processRunning(name string) bool {
	names := runningProcessNames()
	return names[strings.ToLower(name)]
}

var (
	procMu     sync.Mutex
	procCache  map[string]bool
	procCached time.Time
)

func runningProcessNames() map[string]bool {
	procMu.Lock()
	defer procMu.Unlock()
	if procCache != nil && time.Since(procCached) < 5*time.Second {
		return procCache
	}
	out := map[string]bool{}
	// tasklist は Windows に標準で入っている。追加の部品を持ち込まない。
	raw, err := exec.Command("tasklist", "/fo", "csv", "/nh").Output()
	if err == nil {
		for _, line := range strings.Split(string(raw), "\n") {
			line = strings.TrimSpace(line)
			if !strings.HasPrefix(line, `"`) {
				continue
			}
			name := strings.Trim(strings.SplitN(line, `","`, 2)[0], `"`)
			name = strings.TrimSuffix(strings.ToLower(name), ".exe")
			out[name] = true
		}
	}
	procCache = out
	procCached = time.Now()
	return out
}

// processAlive は、その番号のプロセスが動いているかを返す。
//
// 台帳に載っているセッションが本当に生きているかを見る。台帳は終了時に必ず
// 消えるとは限らないので、載っていることだけを根拠にすると「もう居ないもの」を
// 動作中として並べてしまう。
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	alivePidsMu.Lock()
	defer alivePidsMu.Unlock()
	if alivePids == nil || time.Since(alivePidsAt) >= 5*time.Second {
		alivePids = map[int]bool{}
		raw, err := exec.Command("tasklist", "/fo", "csv", "/nh").Output()
		if err == nil {
			for _, line := range strings.Split(string(raw), "\n") {
				parts := strings.Split(strings.TrimSpace(line), `","`)
				if len(parts) < 2 {
					continue
				}
				if n, err := strconv.Atoi(strings.Trim(parts[1], `"`)); err == nil {
					alivePids[n] = true
				}
			}
		}
		alivePidsAt = time.Now()
	}
	return alivePids[pid]
}

var (
	alivePidsMu sync.Mutex
	alivePids   map[int]bool
	alivePidsAt time.Time
)

// boardProcessPIDs は、この機械で動いている beacon-view の番号を返す
// (自分自身は含まない)。非 Windows 版と同じ役目 — 意図は process_other.go の
// 同名関数の説明を参照。
func boardProcessPIDs(selfPID int) []int {
	// tasklist は Windows に標準で入っている。追加の部品を持ち込まない。
	raw, err := exec.Command("tasklist", "/fo", "csv", "/nh").Output()
	if err != nil {
		return nil
	}
	// CSV は `"名前","番号",...` の並びなので、前 2 列を取り出して
	// 共通の解析 (parseBoardPIDs) が読める `番号 名前` の形に直す。
	var b strings.Builder
	for _, line := range strings.Split(raw2s(raw), "\n") {
		cols := splitCSV2(strings.TrimSpace(line))
		if len(cols) < 2 {
			continue
		}
		b.WriteString(cols[1] + " " + cols[0] + "\n")
	}
	return parseBoardPIDs(b.String(), selfPID)
}

func raw2s(raw []byte) string { return string(raw) }

// splitCSV2 は tasklist の行から前 2 列 (名前, 番号) を取り出す。
// 引用符で囲まれた単純な CSV なので、専用の部品は持ち込まない。
func splitCSV2(line string) []string {
	var out []string
	for _, part := range strings.Split(line, `","`) {
		out = append(out, strings.Trim(part, `"`))
		if len(out) == 2 {
			break
		}
	}
	return out
}
