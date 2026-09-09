// 画面を配る受け口 — バイナリ 1 つで完結させるため、画面は実行ファイルに同梱する。
//
// 待ち受けは既定で自分の機械の中だけ。外に開く場合は明示を要求する (盤には
// プロジェクトの全体像が載るのに、この受け口は認証を持たないため)。
package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
)

//go:embed page.html
var pageHTML []byte

// 既定の待ち受け口。使用中なら空きを借りる (複数の盤を同時に開けるように)。
const defaultPort = 7377

const loopbackHost = "127.0.0.1"

func isLoopback(host string) bool {
	switch host {
	case "127.0.0.1", "::1", "localhost":
		return true
	}
	return false
}

// Server は盤を配る受け口。
//
// ``src`` は途中で差し替わる。起動時にプロジェクトが見つからなくても受け口は立て、
// 画面から場所を選んでもらって開き直せるようにするため (起動し直させない)。
type Server struct {
	src      *LocalSource
	ln       net.Listener
	URL      string
	Host     string
	startDir string // 起動した場所。どこを探したかを画面で伝えるのに使う。
}

func (s *Server) beaconDir() string {
	if s.src == nil {
		return ""
	}
	return s.src.BeaconDir
}

// NewServer は受け口を立てる。``expose`` が無いまま外向きの host を指定されたら断る。
func NewServer(src *LocalSource, host string, port int, expose bool) (*Server, error) {
	if host == "" {
		host = loopbackHost
	}
	if !isLoopback(host) && !expose {
		return nil, fmt.Errorf(
			"%s に開くと、その口に届く全員が盤を読めます (このビューワーは認証を持ちません)。"+
				"意図した設置なら --expose を付けてください。"+
				"社外に出す場合は認証を持つ前段の後ろに置いてください", host)
	}
	ln, err := listen(host, port)
	if err != nil {
		return nil, err
	}
	cwd, _ := os.Getwd()
	s := &Server{src: src, ln: ln, Host: host, startDir: cwd}
	s.URL = fmt.Sprintf("http://%s/", ln.Addr().String())
	if isLoopback(host) {
		// 表示用の住所は指定された名前を保つ (localhost と書かれたら localhost と出す)。
		_, portStr, _ := net.SplitHostPort(ln.Addr().String())
		s.URL = fmt.Sprintf("http://%s:%s/", host, portStr)
	}
	return s, nil
}

// listen は希望の口を試し、塞がっていれば空きを借りる。
func listen(host string, port int) (net.Listener, error) {
	if port > 0 {
		ln, err := net.Listen("tcp", fmt.Sprintf("%s:%d", host, port))
		if err == nil {
			return ln, nil
		}
	}
	return net.Listen("tcp", net.JoinHostPort(host, "0"))
}

// Port は実際に使っている口を返す。
func (s *Server) Port() int {
	return s.ln.Addr().(*net.TCPAddr).Port
}

func (s *Server) handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && r.URL.Path != "/index.html" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(pageHTML)
	})

	// まだプロジェクトが決まっていない状態を、画面に正直に伝える。
	// 「盤が出ない」だけで終わらせず、次に何をすればよいかを選べるようにするため。
	mux.HandleFunc("/api/state", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{
			"has_project": s.src != nil,
			"path":        s.beaconDir(),
			"cwd":         s.startDir,
			// クラウドから選ぶ経路はまだ無い。**画面に出す前にここで正直に伝える**
			// (使えない入口を並べておくと、押しても何も起きない状態になる)。
			"cloud_available": false,
			"cloud_reason":    "クラウドへの接続はこの版では未対応です (次の段階で対応します)",
		})
	})

	// 場所を渡してプロジェクトを開き直す。起動し直さずに切り替えられるようにする。
	mux.HandleFunc("/api/open", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST してください", http.StatusMethodNotAllowed)
			return
		}
		var body struct {
			Path string `json:"path"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSONError(w, err)
			return
		}
		src, err := OpenLocal(body.Path)
		if err != nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		if _, err := src.Load(); err != nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		s.src = src
		writeJSON(w, map[string]any{"ok": true, "path": src.BeaconDir})
	})

	mux.HandleFunc("/api/board", func(w http.ResponseWriter, r *http.Request) {
		if s.src == nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "まだプロジェクトが選ばれていません",
			})
			return
		}
		p, err := s.src.Load()
		if err != nil {
			writeJSONError(w, err)
			return
		}
		// 盤は開くたびに読み直す。自動更新で最新が出るようにするため。
		writeJSON(w, BuildBoard(p, SourceLocal, "", nil, nil))
	})

	mux.HandleFunc("/api/target/", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/api/target/")
		if id == "" || s.src == nil {
			http.NotFound(w, r)
			return
		}
		p, err := s.src.Load()
		if err != nil {
			writeJSONError(w, err)
			return
		}
		detail := BuildTargetDetail(p, id)
		if detail == nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]string{
				"error": fmt.Sprintf("%s という対象は見つかりません", id),
			})
			return
		}
		writeJSON(w, detail)
	})

	return mux
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.Encode(v)
}

// writeJSONError は読めなかった理由をそのまま画面に届ける。
// 「盤が出ない」だけで理由が分からない状態を作らないため。
func writeJSONError(w http.ResponseWriter, err error) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusInternalServerError)
	json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}

// Serve は受け口を動かし続ける。
func (s *Server) Serve() error {
	return http.Serve(s.ln, s.handler())
}

// Close は受け口を畳む。
func (s *Server) Close() error { return s.ln.Close() }

// OpenBrowser は既定のブラウザで盤を開く。
// 開けなくても盤は住所を打てば見られるので、失敗しても止めない。
func OpenBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}
