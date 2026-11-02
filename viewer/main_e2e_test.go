package main

// 実行ファイルを建てて実際に起動し、盤が名乗る内容を確かめる (ms-170 e-7147)。
//
// **なぜ単体試験では足りないか** (保守性 review high の反転で判明): 席
// (reuse_key) を `Server` に直接入れる試験は、`main` が `NewServer` に **何を
// 渡すか**を見られない。実際に `main` が席の代わりに空文字を渡す変異を当てても、
// 単体試験は全件緑のままだった。この PR が機能する根拠は「main が席を渡し、
// 盤がそれを名乗る」という配線そのものなので、そこを通さないと守れない。
//
// ここだけは建てて起こす。遅いが (約 10 秒)、`main` の配線を捕まえる試験は
// 他に無い。起動時に名乗る 1 行もここで確かめる (削っても何も落ちなかった)。

import (
	"bufio"
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

var _servingLine = regexp.MustCompile(`盤を開きました: (https?://\S+)`)

// _buildBinary は、このパッケージを一時置き場に建てて道を返す。
func _buildBinary(t *testing.T) string {
	t.Helper()
	out := filepath.Join(t.TempDir(), "beacon-view-test")
	cmd := exec.Command("go", "build", "-o", out, ".")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if raw, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("建てられない: %v\n%s", err, raw)
	}
	return out
}

// _project は、盤が読める最小のプロジェクトを用意して道を返す。
func _project(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	beacon := filepath.Join(dir, ".beacon")
	if err := os.MkdirAll(beacon, 0o755); err != nil {
		t.Fatalf("用意できない: %v", err)
	}
	body := `{"schema_version":1,"name":"e2e","milestones":[]}`
	if err := os.WriteFile(filepath.Join(beacon, "project.json"),
		[]byte(body), 0o644); err != nil {
		t.Fatalf("用意できない: %v", err)
	}
	return dir
}

// boardProc は起こした盤と、その出力を待つための口。
type boardProc struct {
	URL string

	mu   sync.Mutex
	seen strings.Builder
}

// output は今までに見えた出力 (同期して読む)。
func (b *boardProc) output() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.seen.String()
}

// waitFor は、その文字列が出力に現れるまで待つ (出なければ失敗)。
//
// **寝て覗き見る形にしない**: 最初は「住所が出たら 300ms 寝て、溜まった出力を
// 読む」形で書いた。溜め先を 2 つの goroutine で同期せず触っていたので、
// 最後の 1 行を取りこぼし、しかもローカル側はたまたま通っていた。待つなら
// 「出るまで待つ」と書く。
func (b *boardProc) waitFor(t *testing.T, substr string) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if strings.Contains(b.output(), substr) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("出力に %q が現れなかった。見えた出力:\n%s", substr, b.output())
}

// refuteAfterStartup は、起動の案内が出揃ってからその文字列が無いことを見る。
func (b *boardProc) refuteAfterStartup(t *testing.T, anchor, substr string) {
	t.Helper()
	b.waitFor(t, anchor) // 案内が出揃うまで待つ
	if strings.Contains(b.output(), substr) {
		t.Fatalf("出力に %q があってはいけない。見えた出力:\n%s",
			substr, b.output())
	}
}

// _startBoard は盤を起こし、名乗った住所と出力の口を返す。
//
// **頼んだ口ではなく、盤が名乗った住所を使う**。盤は塞がっている口を避けて別の
// 口を借りるので、頼んだ口を決め打ちすると別物を測りうる。
func _startBoard(t *testing.T, bin string, args ...string) *boardProc {
	t.Helper()
	cmd := exec.Command(bin, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("出力を読めない: %v", err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("起こせない: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	b := &boardProc{}
	urlCh := make(chan string, 1)
	go func() {
		r := bufio.NewReader(stdout)
		for {
			line, err := r.ReadString('\n')
			if line != "" {
				b.mu.Lock()
				b.seen.WriteString(line)
				b.mu.Unlock()
				if m := _servingLine.FindStringSubmatch(line); m != nil {
					select {
					case urlCh <- m[1]:
					default:
					}
				}
			}
			if err != nil {
				return
			}
		}
	}()

	select {
	case url := <-urlCh:
		b.URL = url
		return b
	case <-time.After(20 * time.Second):
		t.Fatalf("盤が住所を名乗らなかった。出力:\n%s", b.output())
		return nil
	}
}

func TestBoardAnnouncesItsSeatEndToEnd(t *testing.T) {
	// **main が席を渡し、盤がそれを名乗る**ところまでを通す。この配線が切れると
	// 取得元を切り替えた盤が席を失い、開くたびに増える状態に戻る。
	bin := _buildBinary(t)
	proj := _project(t)
	board := _startBoard(t, bin, "--path", proj, "--no-open",
		"--no-reuse", "--idle-timeout", "0")

	resp, err := http.Get(strings.TrimSuffix(board.URL, "/") + "/api/state")
	if err != nil {
		t.Fatalf("/api/state を叩けない: %v", err)
	}
	defer resp.Body.Close()
	var state struct {
		ReuseKey string `json:"reuse_key"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		t.Fatalf("応答を読めない: %v", err)
	}
	if state.ReuseKey == "" {
		t.Fatal("盤が席を名乗っていない (main の配線が切れている)")
	}
	if !sameBoardPath(state.ReuseKey, proj) {
		t.Fatalf("席が渡したプロジェクトと違う: got %q want %q",
			state.ReuseKey, proj)
	}
}

func TestLocalBoardAnnouncesThatItWillClose(t *testing.T) {
	// 起動時の案内が実際の挙動と一致していること。削っても何も落ちなかった
	// ので、ここで押さえる (保守性 review low)。
	bin := _buildBinary(t)
	proj := _project(t)
	board := _startBoard(t, bin, "--path", proj, "--no-open", "--no-reuse")
	board.waitFor(t, "無操作での終了")
	board.refuteAfterStartup(t, "無操作での終了", "しません")
}

func TestExposedBoardAnnouncesThatItWillNotClose(t *testing.T) {
	// サーバ設置では畳まない、を名乗ることまで含めて 1 つの振る舞い。
	bin := _buildBinary(t)
	proj := _project(t)
	board := _startBoard(t, bin, "--path", proj, "--no-open", "--no-reuse",
		"--host", "127.0.0.1", "--expose")
	board.waitFor(t, "しません")
}
