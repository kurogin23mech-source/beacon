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
