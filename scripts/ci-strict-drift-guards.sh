#!/usr/bin/env bash
# Single source of the STRICT drift guards that BLOCK both a PR merge and a
# RELEASE (ms-133).
#
# Why one script: these guards used to live only inline in lint-docs.yml, which
# runs on `pull_request` + `push: main`. But a `push: main` run happens AFTER the
# commit already landed (it can't un-push), and a PR run only blocks merge when
# it's a required status check — so drift CAN reach main and, from there, ship in
# a release (release.yml cuts from main). To close the SHIP path, release.yml now
# runs these same guards as an early gate.
#
# Putting the list in ONE script means the "what counts as release-blocking
# drift" set can't itself drift between the two workflow files (that would be an
# ironic meta-drift). Both .github/workflows/lint-docs.yml and release.yml call
# this. Add a new strict guard here and BOTH gates pick it up.
#
# Each check exits non-zero on drift; `set -e` aborts on the first failure so the
# workflow step fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[ci-strict-drift-guards] bash↔Python CLI dispatch + help/README parity…"
python3 "$ROOT/scripts/check-cli-help-drift.py" --strict

echo "[ci-strict-drift-guards] server/ ↔ lib/ name collision…"
python3 "$ROOT/scripts/check-server-lib-collision.py" --strict

echo "[ci-strict-drift-guards] INSTALL.md static validation…"
python3 "$ROOT/scripts/check-install-md.py" --strict

echo "[ci-strict-drift-guards] Skill → CLI 境界 (ms-160 e-5981)…"
# Skill が lib/commands.py を直叩きしていないこと。直叩きは CORE doc
# architecture-tool-skill-separation §2 の層を飛ばし、(1) beacon --help に
# 出ないので発見できない (2) Windows/pipx から到達できない (3) 環境変数名が
# 手順書と実装の 2 箇所に複製され改名で黙って壊れる、を同時に生む。実際に
# 2 動詞が到達不能になっていたので、再発を CI で止める。
python3 "$ROOT/scripts/check-skill-cli-boundary.py"

echo "[ci-strict-drift-guards] 宛先確認の規則 ↔ 手順書 (ms-160 e-6349)…"
# サーバ側の規則 (dm_consent.classify_send_consent) と /beacon-dm-send の記述が
# 食い違わないこと。以前 Skill は「same-project なら宛先確認は不要」と書いていたが
# 規則は project を見ず「宛先が別の人間か」で判定するため、同一プロジェクトの
# 協働者宛に手順どおり送ると 403 で弾かれた。手順書に従うほど失敗する型なので、
# 判定理由の説明漏れと project 軸の文言の復活を CI で止める。
#
# 依存は標準ライブラリだけ: この script は pytest の入っていない lint-docs ジョブ
# からも呼ばれる (最初の版は `python3 -m pytest` を直に書いて CI を落とした)。
# 挙動レベルの契約は tests/test_bus_consent_check_contract_e6349.py が担い、
# それらは test ジョブが走らせる。
python3 "$ROOT/scripts/check-dm-consent-alignment.py"

echo "[ci-strict-drift-guards] capability scope invariant (ms-134 e-4721)…"
# Every CLI verb must classify L0..L4, and no profession-shared (L1/L2)
# capability may reach a profession concrete (core.save_entry /
# find_target_milestone) — it must record through occupation.record_target_entry.
# This is the boundary e-4720 closed for `doc`; blocking it here keeps a
# regression from shipping.
python3 "$ROOT/scripts/check-capability-scope.py"

echo "[ci-strict-drift-guards] Windows-unsafe pid liveness probes (ms-133 e-6591)…"
# Python's os.kill maps EVERY signal — 0 included — onto TerminateProcess on
# Windows, so the POSIX idiom `os.kill(pid, 0)` asks "are you alive?" by killing.
# Four of Beacon's five liveness probes did exactly that (bridge claim, Codex
# daemon pidfile, bcodex wrapper watch, version skew). They now share
# lib/pid_liveness.py. This blocks a re-introduction: the idiom keeps working on
# the Mac/Linux machines where it gets written, so only a machine check catches it.
# The check fails SAFE — it flags every os.kill whose signal it cannot PROVE is
# non-zero, so a `_PROBE = 0` constant or `os.kill(*args)` cannot slip past it
# (both did, before the 2026-09-29 independent AX review caught the hole).
python3 "$ROOT/scripts/check-pid-liveness.py" --strict

echo "[ci-strict-drift-guards] 旗 → env の写像が両フロントで揃っているか (ms-160 e-6674)…"
# 旗の名前を比べる check-cli-help-drift.py はこの級を原理的に見られない。片方の
# フロントに無い旗は「不一致」ではなく「不在」で、両方に無ければ揃っていると
# 数えられる。落ちていたのは argv を環境変数へ写す対応表の方で、それは 2 言語で
# 手書きされている。beacon doc add --force / search --source / cloud list --json
# ほか約 45 件が bash で通り python で落ちる状態だった。
python3 "$ROOT/scripts/check-cli-env-parity.py"

echo "[ci-strict-drift-guards] 成功表示が書き込みより先に出ていないか (ms-160 e-6688)…"
# `beacon task done e-5981` が stdout に "Done: [e-5981] ..." を出しつつ、stderr に
# 並行書き込みガードの中止を出して exit 1 し、タスクは todo のまま残った (2026-09-29)。
# save_project は正しく中止していて、欠陥は順序だった。ターミナルでは 2 つの流れが
# 混ざるので、読み手 (人間でも AI でも) に残るのは成功行の方になる。
# 順序はコメントでは保てないので機械で止める。
python3 "$ROOT/scripts/check-print-before-save.py"

echo "[ci-strict-drift-guards] lib/ への道の持ち主が 1 つか (ms-166 e-6728)…"
# server/_libpath.py の docstring は自分を「lib/ への道を知る唯一の持ち主」と名乗る。
# だが名乗りは守らない。2026-10-08 の独立レビュー 2 体が揃って捕まえたのがこれで、
# _libpath を作って app.py / firestore_client.py を揃えた直後の時点で、
# routers_projects.py の検索ハンドラの中に 3 つ目のコピーが関数の中に残っていた。
# `grep _libpath` では出てこないので、次に lib/ の位置を変える人はそこだけ取り残す。
# 宣言ではなく構文木で数えて止める。
python3 "$ROOT/scripts/check-lib-path-single-owner.py" --strict

echo "[ci-strict-drift-guards] worktree 判定の箇所が台帳どおりか (ms-166 e-6932)…"
# context_monitor.py::_worktree_shared_base の docstring は「git にこの問いを立てて
# いるのは N 箇所で、git の答えの形が変わったら全員が変更対象」と名乗る。だが名乗りは
# 守らない。2026-10-10 の独立レビュー (保守性) が指摘し、ガードを書いた初回実行で
# **宣言が既に誤っていた**ことが判明した: 実際は 4 箇所で、cmd_milestone.py の
# _is_git_project が --git-dir だけを使っており、--git-common-dir を grep する数え方
# では落ちる 4 人目だった。共有ヘルパーに寄せる案はこのフックが lib/ を import せずに
# 動く制約と衝突するので、散在を機械で数える側で閉じる。増えても減っても赤くする。
python3 "$ROOT/scripts/check-worktree-probe-census.py" --strict

echo "[ci-strict-drift-guards] all strict drift guards passed."
