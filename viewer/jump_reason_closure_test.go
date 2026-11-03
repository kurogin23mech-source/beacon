package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"sort"
	"strings"
	"testing"
)

// 飛べない理由の enum が、**判定が実際に返す集合**を縛っていること (e-7266)。
//
// ## なぜ構文木で見るか
//
// 既存の 2 つのガード (TestJumpBlockedEnumClosed / TestJumpBlockedLiteralMatchesPage)
// は `AllJumpBlockedReasons` ↔ 手書きの表 ↔ page.html の対応表を突き合わせる。だが
// **判定 (jumpVerdictKind) が実際に返す文字列集合は誰も見ていなかった**。
//
// 独立 AX レビューが実測して示した: 新しい定数と `case` を 1 箇所だけ足して
// `AllJumpBlockedReasons` にも page.html にも登録しないと、`go test` は**全て通る**。
// 「列挙はもう無い、一覧が唯一」という宣言は、switch 側については社会的な規約
// (コメントで『書き写さないこと』と頼む) に留まっていた。
//
// ここは判定関数の `return` を構文木で歩いて、返しうる識別子を全部集める。
// 文字列の部分一致ではなく宣言の構造を読むので、綴りの書き方 (定数名 / リテラル) で
// ガードが firing するか否かが決まらない。
func TestVerdictReturnsOnlyRegisteredReasons(t *testing.T) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "jump.go", nil, 0)
	if err != nil {
		t.Fatalf("jump.go を読めない: %v", err)
	}

	registered := map[string]bool{}
	for _, r := range AllJumpBlockedReasons {
		registered[r] = true
	}

	// 判定関数の中の `return false, X` の X を集める。
	var returned []string
	for _, decl := range f.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "jumpVerdictKind" || fn.Body == nil {
			continue
		}
		ast.Inspect(fn.Body, func(n ast.Node) bool {
			ret, ok := n.(*ast.ReturnStmt)
			if !ok || len(ret.Results) != 2 {
				return true
			}
			switch v := ret.Results[1].(type) {
			case *ast.Ident:
				// 空文字の変数や定数名。`""` のリテラルは BasicLit なのでここには来ない。
				returned = append(returned, v.Name)
			case *ast.BasicLit:
				if v.Kind == token.STRING && v.Value != `""` {
					returned = append(returned, "リテラル "+v.Value)
				}
			}
			return true
		})
	}
	if len(returned) == 0 {
		t.Fatal("jumpVerdictKind の return を 1 つも拾えなかった — " +
			"関数名か返り値の形が変わったなら、この歩き方も直すこと " +
			"(拾えないまま緑になるのが最悪の壊れ方)")
	}

	// 返しうる識別子が、すべて一覧に載っている定数か (または envBlocked の引数) か。
	allowedIdents := map[string]bool{
		// envBlocked は呼び出し元が渡す理由で、その値域は jumpEnvBlocked が決める。
		// そちらは TestJumpBlockedEnumClosed が定数として押さえている。
		"envBlocked": true,
	}
	for name, konst := range reasonConstantsByName(t, f) {
		if registered[konst] {
			allowedIdents[name] = true
		}
	}

	var bad []string
	for _, r := range returned {
		if !allowedIdents[r] {
			bad = append(bad, r)
		}
	}
	sort.Strings(bad)
	if len(bad) > 0 {
		t.Errorf("jumpVerdictKind が、AllJumpBlockedReasons に載っていない理由を"+
			"返しています: %v\n"+
			"  理由を足すときは AllJumpBlockedReasons にも入れること。入れないと "+
			"page.html の対応表との突合 (TestJumpBlockedLiteralMatchesPage) も"+
			"素通りし、画面が生の値をそのまま出す。", bad)
	}
}

// reasonConstantsByName は jump.go の文字列定数を 定数名 → 値 で返す。
func reasonConstantsByName(t *testing.T, f *ast.File) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, decl := range f.Decls {
		gd, ok := decl.(*ast.GenDecl)
		if !ok || gd.Tok != token.CONST {
			continue
		}
		for _, spec := range gd.Specs {
			vs, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			for i, name := range vs.Names {
				if i >= len(vs.Values) {
					continue
				}
				lit, ok := vs.Values[i].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				out[name.Name] = strings.Trim(lit.Value, `"`)
			}
		}
	}
	return out
}
