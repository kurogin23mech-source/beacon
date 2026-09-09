//go:build windows

package main

import (
	"os/exec"
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
