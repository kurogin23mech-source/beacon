// 画面を配る受け口 — バイナリ 1 つで完結させるため、画面は実行ファイルに同梱する。
//
// 待ち受けは既定で自分の機械の中だけ。外に開く場合は明示を要求する (盤には
// プロジェクトの全体像が載るのに、この受け口は認証を持たないため)。
package main

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
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
	cloud    *CloudSource // クラウドを見ているときだけ入る (ローカルとは排他)
	login    *LoginStart  // ログイン待ちの合図
	ln       net.Listener
	URL      string
	Host     string
	startDir string // 起動した場所。どこを探したかを画面で伝えるのに使う。
}

// hasSource は、盤を出せる状態かどうか。
func (s *Server) hasSource() bool { return s.src != nil || s.cloud != nil }

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
			"cloud_available": true,
			"cloud_signed_in": cloudEmail(),
			// 選択ダイアログは **このビューワーが動いている機械** に開く。
			// サーバに設置した場合、見ている人の手元ではなくサーバ側に出てしまい
			// 何も起きないように見えるので、その場合は入口ごと出さない。
			"picker_available": isLoopback(s.Host),
		})
	})

	// フォルダ選択ダイアログを出して、選ばれた場所を返す。
	mux.HandleFunc("/api/pick-folder", func(w http.ResponseWriter, r *http.Request) {
		if !isLoopback(s.Host) {
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "この盤はサーバで動いているため、ダイアログはお使いの機械に出せません",
			})
			return
		}
		path, err := PickFolder(s.beaconDir())
		if err != nil {
			writeJSONError(w, err)
			return
		}
		// 選ばずに閉じた場合は空。これは失敗ではないので、そのまま返す。
		writeJSON(w, map[string]string{"path": path})
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
		s.cloud = nil // 取得元は 1 つに保つ
		writeJSON(w, map[string]any{"ok": true, "path": src.BeaconDir})
	})

	// クラウドへのログインを始める。合図の符号と、承認してもらう住所を返す。
	mux.HandleFunc("/api/cloud/login-start", func(w http.ResponseWriter, r *http.Request) {
		start, err := StartLogin(DefaultAPI)
		if err != nil {
			writeJSONError(w, err)
			return
		}
		s.login = start
		// 承認の画面はこの機械で開く。手元で立ち上げているときは自分の画面に
		// 出るので、そのまま承認できる。
		if isLoopback(s.Host) {
			OpenBrowser(start.URL)
		}
		writeJSON(w, start)
	})

	// 承認されたかを確認する。まだなら pending を返す (これは失敗ではない)。
	mux.HandleFunc("/api/cloud/login-poll", func(w http.ResponseWriter, r *http.Request) {
		if s.login == nil {
			writeJSONError(w, errors.New("ログインが始まっていません"))
			return
		}
		creds, err := PollLogin(DefaultAPI, s.login.Code)
		if err != nil {
			writeJSONError(w, err)
			return
		}
		if creds == nil {
			writeJSON(w, map[string]string{"status": "pending"})
			return
		}
		if err := SaveCredentials(creds); err != nil {
			writeJSONError(w, err)
			return
		}
		s.login = nil
		writeJSON(w, map[string]string{"status": "approved", "email": creds.Email})
	})

	// 参加しているプロジェクトの一覧。
	mux.HandleFunc("/api/cloud/projects", func(w http.ResponseWriter, r *http.Request) {
		creds := LoadCredentials()
		if creds == nil || creds.Expired(time.Now()) {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "ログインしていません",
			})
			return
		}
		c := &CloudSource{API: DefaultAPI, Token: creds.Token}
		list, err := c.ListProjects()
		if err != nil {
			writeJSONError(w, err)
			return
		}
		writeJSON(w, list)
	})

	// クラウドのプロジェクトを開く。
	mux.HandleFunc("/api/cloud/open", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST してください", http.StatusMethodNotAllowed)
			return
		}
		var body struct {
			ProjectID string `json:"project_id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSONError(w, err)
			return
		}
		creds := LoadCredentials()
		if creds == nil || creds.Expired(time.Now()) {
			writeJSONError(w, errors.New("ログインしていません"))
			return
		}
		c := &CloudSource{
			API: DefaultAPI, Token: creds.Token, ProjectID: body.ProjectID}
		if _, err := c.Load(); err != nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		// 開けたときだけ差し替える。失敗しても、それまで見ていた盤は壊さない。
		s.cloud = c
		s.src = nil
		writeJSON(w, map[string]any{"ok": true, "project_id": body.ProjectID})
	})

	// 名乗っているセッションに文面を送る (ms-171)。
	//
	// **自分の機械の中に開いているときだけ**。この受け口は認証を持たないので、
	// 外に開いた状態で送れると、その口に届く誰もがあなたの名前で他のセッションを
	// 動かせてしまう (フォルダ選択ダイアログと同じ扱い)。
	mux.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST してください", http.StatusMethodNotAllowed)
			return
		}
		if !isLoopback(s.Host) {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "外に開いている盤からは送れません (この受け口は認証を持たないため)",
			})
			return
		}
		var body struct {
			SessionID string `json:"session_id"`
			ProjectID string `json:"project_id"`
			Text      string `json:"text"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSONError(w, err)
			return
		}
		creds := LoadCredentials()
		if creds == nil || creds.Expired(time.Now()) {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "ログインしていません",
			})
			return
		}
		pid := body.ProjectID
		if pid == "" && s.cloud != nil {
			pid = s.cloud.ProjectID
		}
		if pid == "" && s.src != nil {
			pid = s.src.CloudProjectID()
		}
		if pid == "" {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "どのプロジェクト宛か分かりません",
			})
			return
		}
		c := &CloudSource{API: DefaultAPI, Token: creds.Token, ProjectID: pid}

		// 誰として送るかを決める。名乗れないなら送らない。
		// 名簿が引けたときは名簿を正とする (手元の記録はずれることがある)。
		beaconDir := ""
		if s.src != nil {
			beaconDir = s.src.BeaconDir
		}
		roster, rosterErr := c.Sessions()
		sender, err := resolveSender(beaconDir, s.localRoot(), roster, rosterErr == nil)
		if err != nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}

		res, err := c.SendPrompt(body.SessionID, body.Text, sender)
		if err != nil {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		// 送っただけで終わらせず、届いたかまで確かめて返す。
		writeJSON(w, c.waitForReceipt(res.EventID))
	})

	// 全セッション横断の一覧 (ms-171)。盤とは逆に、プロジェクトを跨いで
	// 「このマシンで何が動いているか」を並べる。
	mux.HandleFunc("/api/sessions", func(w http.ResponseWriter, r *http.Request) {
		// 名乗っている名簿が取れるなら渡す。名乗っているものは担当が確かなので
		// 手元の推測より優先される。
		var named []SessionRow
		if s.cloud != nil {
			named, _ = s.cloud.Sessions()
		} else if s.src != nil {
			named = s.rosterForLocal()
		}
		writeJSON(w, AllSessions(24*time.Hour, time.Now(), named))
	})

	mux.HandleFunc("/api/board", func(w http.ResponseWriter, r *http.Request) {
		if !s.hasSource() {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "まだプロジェクトが選ばれていません",
			})
			return
		}
		board, err := s.buildBoard()
		if err != nil {
			writeJSONError(w, err)
			return
		}
		writeJSON(w, board)
	})

	mux.HandleFunc("/api/target/", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/api/target/")
		if id == "" || !s.hasSource() {
			http.NotFound(w, r)
			return
		}
		p, err := s.loadProject()
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

// loadProject は、いま見ている先からプロジェクトを読む。
// **ここが取得元を尋ねる唯一の場所**。これより先には取得元を持ち込まない。
func (s *Server) loadProject() (*Project, error) {
	if s.cloud != nil {
		return s.cloud.Load()
	}
	return s.src.Load()
}

// buildBoard は盤を 1 面組み立てる。取得元による違いはここで吸収し切る。
func (s *Server) buildBoard() (*Board, error) {
	p, err := s.loadProject()
	if err != nil {
		return nil, err
	}
	var board *Board
	if s.cloud != nil {
		// クラウドに繋いだときだけ、Beacon に名乗っているセッションの名簿が取れる。
		// 名簿もドキュメントも、取れなくても盤は出す (見えないことより出ないことの
		// ほうが困る)。
		sessions, _ := s.cloud.Sessions()
		docs, _ := s.cloud.Documents()
		board = BuildBoard(p, SourceCloud, s.cloud.ProjectID, docs, sessions)
	} else {
		// ローカルで開いていても、クラウドに結び付いていてログイン済みなら、
		// 名乗っているセッションの名簿は取れる。盤の中身は手元から読んでいるので
		// 取得元はローカルのまま。
		//
		// ここを取りに行かないと「クラウドのセッションが出ない」ように見える
		// (2026-09-10 の指摘)。名簿の在り処は取得元とは別の話。
		board = BuildBoard(p, SourceLocal, "", localDocuments(s.src.BeaconDir),
			s.rosterForLocal())
	}

	// このマシンで動いているセッションは **どちらの取得元でも添える**。
	//
	// クラウドに切り替えた瞬間にこれが消えると、「自分のセッションが居なくなった」
	// ように見える (2026-09-10 の指摘)。名乗っているセッションの名簿とは別物で、
	// 取得元とは関係なく「この機械で何が動いているか」を表すもの。
	// 取れなくても盤は出す。
	if root := s.localRoot(); root != "" {
		board.LocalSessions = LocalSessions(root, 24*time.Hour, time.Now())
	}
	return board, nil
}

// rosterForLocal は、ローカルで開いているプロジェクトの名簿をクラウドから取る。
//
// 結び付いていない / 未ログイン / 繋がらない、のいずれでも空を返す。名簿が
// 取れなくても盤は出す (見えないことより出ないことのほうが困る)。
func (s *Server) rosterForLocal() []SessionRow {
	pid := s.src.CloudProjectID()
	if pid == "" {
		return nil
	}
	creds := LoadCredentials()
	if creds == nil || creds.Expired(time.Now()) {
		return nil
	}
	rows, err := (&CloudSource{
		API: DefaultAPI, Token: creds.Token, ProjectID: pid}).Sessions()
	if err != nil {
		return nil
	}
	return rows
}

// localRoot は、このマシンのセッションを探す起点。
//
// ローカルを開いていればその場所、クラウドを見ているときは **起動した場所** を使う。
// クラウドの盤を見ていても、手元で動いているセッションは起動した場所のものだから。
func (s *Server) localRoot() string {
	if s.src != nil {
		return filepath.Dir(s.src.BeaconDir)
	}
	return s.startDir
}

// cloudEmail は保存済みの認証情報の持ち主を返す。未ログインなら空。
func cloudEmail() string {
	if c := LoadCredentials(); c != nil && !c.Expired(time.Now()) {
		return c.Email
	}
	return ""
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
