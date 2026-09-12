// フォルダ選択ダイアログ — パスを手で貼らずに選べるようにする。
//
// 制約: ダイアログは **このビューワーが動いている機械** に開く。手元で立ち上げて
// いるときは自分の画面に出るので普通に使えるが、サーバに設置した場合は見ている人の
// 手元ではなくサーバ側に開いてしまう。したがって、この入口は自分の機械の中だけに
// 開いているときしか出さない (server.go 側で判定する)。
package main

import (
	"errors"
	"os/exec"
	"runtime"
	"strings"
)

// errNoPicker はこの環境に選択ダイアログが無いこと。貼り付けに誘導するために使う。
var errNoPicker = errors.New("この環境にはフォルダ選択ダイアログがありません")

// PickFolder は OS のフォルダ選択ダイアログを出し、選ばれた場所を返す。
// 選ばずに閉じられた場合は空文字を返す (これは失敗ではない)。
func PickFolder(start string) (string, error) {
	switch runtime.GOOS {
	case "windows":
		return pickFolderWindows(start)
	case "darwin":
		return pickFolderMac(start)
	default:
		return pickFolderLinux(start)
	}
}

func pickFolderWindows(start string) (string, error) {
	// PowerShell の標準ダイアログを使う。追加の部品を持ち込まずに済む。
	script := `
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Beacon プロジェクトのフォルダを選んでください"
$d.ShowNewFolderButton = $false
if ($env:BEACON_PICK_START) { $d.SelectedPath = $env:BEACON_PICK_START }
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write($d.SelectedPath)
}`
	cmd := exec.Command("powershell", "-NoProfile", "-STA", "-Command", script)
	cmd.Env = append(cmd.Environ(), "BEACON_PICK_START="+start)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

func pickFolderMac(start string) (string, error) {
	script := `POSIX path of (choose folder with prompt "Beacon プロジェクトのフォルダを選んでください")`
	out, err := exec.Command("osascript", "-e", script).Output()
	if err != nil {
		// 選ばずに閉じた場合も失敗として返るので、区別せず「選ばれなかった」扱い。
		return "", nil
	}
	return strings.TrimSpace(string(out)), nil
}

func pickFolderLinux(start string) (string, error) {
	// zenity があれば使う。無い環境では「選べない」と正直に返し、貼り付けに誘導する。
	if _, err := exec.LookPath("zenity"); err != nil {
		return "", errNoPicker
	}
	args := []string{"--file-selection", "--directory",
		"--title=Beacon プロジェクトのフォルダを選んでください"}
	if start != "" {
		args = append(args, "--filename="+start+"/")
	}
	out, err := exec.Command("zenity", args...).Output()
	if err != nil {
		return "", nil // 選ばずに閉じた
	}
	return strings.TrimSpace(string(out)), nil
}
