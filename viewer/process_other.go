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
