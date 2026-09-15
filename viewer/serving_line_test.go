package main

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// The Python launcher (scripts/open-board.py) reads the board URL out of
// `beacon view`'s stdout by anchoring on the fixed serving-line prefix
// "盤を開きました: ". The Go viewer and the Python fallback (lib/cmd_view.py)
// must print that exact prefix or the launcher silently degrades to
// "URL unconfirmed". This is the Go half of the cross-language contract pinned
// in tests/test_session_start_extracted_scripts.py (PR #748 review, high).
//
// This is a source-level assertion (it does not modify main.go) so it stays
// conflict-free with concurrent work in viewer/.

const servingPrefix = "盤を開きました: "

func TestServingLinePrefixIsStable(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatalf("read main.go: %v", err)
	}
	// main.go must print the serving line with the shared prefix + a URL.
	if !strings.Contains(string(src), `fmt.Printf("`+servingPrefix+`%s\n"`) {
		t.Fatalf("main.go serving line drifted from the launcher's anchor %q; "+
			"scripts/open-board.py._URL_RE and lib/cmd_view.py must be updated together",
			servingPrefix)
	}
	// A line built from that prefix must satisfy the launcher's anchored regex
	// (mirror of scripts/open-board.py: r"盤を開きました:\s*(https?://\S+)").
	anchor := regexp.MustCompile(`盤を開きました:\s*(https?://\S+)`)
	line := servingPrefix + "http://127.0.0.1:8080/"
	m := anchor.FindStringSubmatch(line)
	if m == nil || m[1] != "http://127.0.0.1:8080/" {
		t.Fatalf("serving line %q not matched by launcher anchor", line)
	}
}
