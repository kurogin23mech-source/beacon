//go:build windows

package main

import (
	"syscall"
	"unsafe"
)

// ownsConsole は、この実行ファイルのためだけにコンソール窓が開かれたかを返す。
//
// エクスプローラーからダブルクリックで起動すると Windows が窓を作り、
// **その窓に居るのはこの実行ファイルだけ**になる。終了すると窓ごと消えるので、
// 出したメッセージが読めない。一方、シェルから実行した場合は窓にシェルも居るので、
// 終了しても窓は残る。
//
// この違いを「窓に紐づくプロセスの数」で見分ける。1 つなら、この窓はこの実行ファイル
// のために開かれたもの。
func ownsConsole() bool {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	proc := kernel32.NewProc("GetConsoleProcessList")
	if proc.Find() != nil {
		return false
	}
	var pids [8]uint32
	n, _, _ := proc.Call(uintptr(unsafe.Pointer(&pids[0])), uintptr(len(pids)))
	return n == 1
}
