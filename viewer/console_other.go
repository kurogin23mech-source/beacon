//go:build !windows

package main

// ownsConsole は Windows 以外では常に false。
//
// 端末から実行するのが普通で、終了しても窓が消えて読めなくなることがないため、
// 待つ必要がない。
func ownsConsole() bool { return false }
