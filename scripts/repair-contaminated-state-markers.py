#!/usr/bin/env python3
"""ms-173 / e-6582 — 残留した「確認待ち」宣言を一度だけ洗う。

運用室で `確認待ち` (橙) のまま何日も消えない行の正体は、producer 修正 (62766932) より
前の beacon が書いた汚染マーカー: `declared_state=awaiting_human` なのに `state_detail`
がアイドル通知の文言 ("Claude is waiting for your input") になっている。本物の待ちでは
ないので橙にしてはいけないが、宣言を書き換えられるのはセッション自身だけなので、
二度と hook が発火しない放置セッションでは永久に残る (2026-09-19 実測 3 行)。

**なぜ恒久的な実行時ルールでなく修復なのか**: 汚染を作る経路は producer 側で閉じて
あり (62766932 + 3cfc6a0f)、汚染はもう増えない有限の集合。増えない集合のために
「読む側で降格する」恒久ルールを足すと、修正前 beacon では汚染と『本物の待ちが上書き
された行』が完全同形になるため、本物の確認待ちを隠す恐れを永久に抱え込む。有限の
レガシーデータはデータ側で直すのが筋 (ms-173 e-6717 はこの判断で閉じた)。

判定と変換は lib/session_state_hook (producer と同じ 1 箇所) が所管。このスクリプトは
探索と書き込みだけを持つ = 判定が producer と drift しない。

既定は **dry-run**。実際に書くには `--apply` が要る。書く前に必ず退避 (.bak) を作る。

使い方:
    python3 scripts/repair-contaminated-state-markers.py            # 調べるだけ
    python3 scripts/repair-contaminated-state-markers.py --apply    # 直す
    python3 scripts/repair-contaminated-state-markers.py --root <path> [--root <path>]
    python3 scripts/repair-contaminated-state-markers.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "lib",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import session_state_hook  # noqa: E402

MARKER_NAME = "session-state.json"


def _repo_roots(explicit):
    """探索対象の root を決める。

    明示指定が無ければ「このリポジトリ本体 + その worktree 全部」を見る。汚染マーカーは
    worktree ごとの .beacon/ に居るので、本体だけ見ると取りこぼす (fork を多用する運用では
    そこが主な住処)。
    """
    if explicit:
        return [Path(p).expanduser().resolve() for p in explicit]
    roots = []
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            roots.append(Path(top.stdout.strip()).resolve())
        wt = subprocess.run(["git", "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=20)
        if wt.returncode == 0:
            for line in wt.stdout.splitlines():
                if line.startswith("worktree "):
                    roots.append(Path(line[len("worktree "):].strip()).resolve())
    except Exception:
        pass
    if not roots:
        roots.append(Path.cwd().resolve())
    # dedup、存在するものだけ
    seen, out = set(), []
    for r in roots:
        if r in seen or not r.is_dir():
            continue
        seen.add(r)
        out.append(r)
    return out


def _marker_paths(roots):
    for root in roots:
        p = root / ".beacon" / MARKER_NAME
        if p.is_file():
            yield p


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"__unreadable__": str(e)}


def _write_repaired(path: Path, repaired: dict) -> str:
    """退避を取ってから書く。退避先のパスを返す。

    削除は一切しない (元の内容は .bak に残る) ので、取り違えても必ず戻せる。
    """
    backup = path.with_suffix(path.suffix + ".before-e6582-repair")
    n = 0
    while backup.exists():
        n += 1
        backup = path.with_suffix(path.suffix + f".before-e6582-repair.{n}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(repaired, f, ensure_ascii=False)
    os.replace(tmp, path)
    return str(backup)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="残留した「確認待ち」宣言 (汚染マーカー) を idle へ降格する")
    ap.add_argument("--root", action="append", default=[],
                    help="探索する作業フォルダ (複数可)。既定は本体 + 全 worktree")
    ap.add_argument("--apply", action="store_true",
                    help="実際に書き換える (既定は調べるだけ)")
    ap.add_argument("--json", action="store_true", help="結果を JSON で出す")
    args = ap.parse_args(argv)

    roots = _repo_roots(args.root)
    results = []
    for path in _marker_paths(roots):
        marker = _load(path)
        if "__unreadable__" in marker:
            results.append({"path": str(path), "status": "unreadable",
                            "detail": marker["__unreadable__"]})
            continue
        repaired = session_state_hook.repair_contaminated_marker(marker)
        if repaired is None:
            results.append({"path": str(path), "status": "clean",
                            "declared_state": marker.get("declared_state")})
            continue
        row = {"path": str(path), "status": "contaminated",
               "was_detail": marker.get("state_detail"),
               "state_since": marker.get("state_since")}
        if args.apply:
            try:
                row["backup"] = _write_repaired(path, repaired)
                row["status"] = "repaired"
            except Exception as e:
                row["status"] = "repair_failed"
                row["detail"] = str(e)
        results.append(row)

    if args.json:
        print(json.dumps({"roots": [str(r) for r in roots],
                          "applied": bool(args.apply),
                          "results": results}, ensure_ascii=False, indent=1))
        return 0

    bad = [r for r in results if r["status"] in ("contaminated", "repaired")]
    print(f"探索した作業フォルダ: {len(roots)} / マーカー: {len(results)} 件")
    for r in results:
        if r["status"] == "clean":
            continue
        print(f"  [{r['status']}] {r['path']}")
        if r.get("was_detail"):
            print(f"      待機内容だったもの: {r['was_detail']!r}")
        if r.get("backup"):
            print(f"      退避: {r['backup']}")
        if r.get("detail"):
            print(f"      詳細: {r['detail']}")
    if not bad:
        print("汚染マーカーはありません。")
    elif not args.apply:
        print(f"\n汚染 {len(bad)} 件。直すには --apply を付けて実行してください "
              "(書く前に .before-e6582-repair へ退避します)。")
    else:
        print(f"\n{len(bad)} 件を idle へ降格しました。"
              "各セッションの受信プロセスが次の生存報告でこれをクラウドへ運びます。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
