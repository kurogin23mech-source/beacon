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

echo "[ci-strict-drift-guards] 成功表示が書き込みより先に出ていないか (ms-160 e-6688)…"
# `beacon task done e-5981` が stdout に "Done: [e-5981] ..." を出しつつ、stderr に
# 並行書き込みガードの中止を出して exit 1 し、タスクは todo のまま残った (2026-09-29)。
# save_project は正しく中止していて、欠陥は順序だった。ターミナルでは 2 つの流れが
# 混ざるので、読み手 (人間でも AI でも) に残るのは成功行の方になる。
# 順序はコメントでは保てないので機械で止める。
python3 "$ROOT/scripts/check-print-before-save.py"

echo "[ci-strict-drift-guards] all strict drift guards passed."
