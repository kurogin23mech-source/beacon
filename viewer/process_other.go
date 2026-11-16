//go:build !windows

package main

import (
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// processRunning は、その名前のプロセスが動いているかを返す。
// 取得は短い間だけ覚えておく (毎回一覧を取ると重いため)。
func processRunning(name string) bool {
	procMu.Lock()
	defer procMu.Unlock()
	if procCache == nil || time.Since(procCached) >= 5*time.Second {
		procCache = map[string]bool{}
		raw, err := exec.Command("ps", "-A", "-o", "comm=").Output()
		if err == nil {
			for _, line := range strings.Split(string(raw), "\n") {
				base := strings.ToLower(strings.TrimSpace(line))
				if i := strings.LastIndex(base, "/"); i >= 0 {
					base = base[i+1:]
				}
				if base != "" {
					procCache[base] = true
				}
			}
		}
		procCached = time.Now()
	}
	return procCache[strings.ToLower(name)]
}

var (
	procMu     sync.Mutex
	procCache  map[string]bool
	procCached time.Time
)

// processAlive は、その番号のプロセスが動いているかを返す。
// 合図 0 を送ると、存在するかどうかだけを確かめられる。
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}

// boardProcessPIDs は、この機械で動いている beacon-view の番号を返す
// (自分自身と、動いていないものは含まない)。
//
// **何のためか** (ms-170 e-7147): 控え (portfile.go) が効くのは、控えを置いた
// 盤に対してだけ。控えの仕組みが入る前に立った盤は、ランダムな口で孤児として
// 残り続け、控えからは見えない。再利用で**新しい孤児は生まれなくなる**が、既に
// 居るものは誰も教えてくれないと気づけない。だから起動時に一度数えて伝える。
//
// 名前で拾う。配布物の名前は `beacon-view-darwin-arm64` / `beacon-view` と揺れる
// ので、前方一致で見る (受入条件の確認手段 `pgrep -f beacon-view-darwin` と同じ
// 見方に揃えてある)。
func boardProcessPIDs(selfPID int) []int {
	raw, err := exec.Command("ps", "-A", "-o", "pid=,comm=").Output()
	if err != nil {
		return nil
	}
	return parseBoardPIDs(string(raw), selfPID)
}
