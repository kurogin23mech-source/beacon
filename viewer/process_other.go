//go:build !windows

package main

import (
	"os/exec"
	"strings"
	"sync"
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
