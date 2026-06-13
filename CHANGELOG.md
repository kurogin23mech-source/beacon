# Changelog

All notable changes to Beacon are documented here. See [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for format.

## [v0.34.0] - 2026-06-13

- refactor: enforce beacon vs beacon-cloud separation principle
- feat(ms-64): apply_operation routes to DynamoDB when backend=dynamodb (e-1631)
- feat(ms-64): prompt profile choice at init / first push when multi-login (e-1633)
- Merge pull request #150 from kurogin23mech-source/ms-64-e1628-spa-cognito
- feat(ms-64): SPA に Cognito Hosted UI redirect flow を追加 (e-1628)
- Merge pull request #149 from kurogin23mech-source/ms-64-cloud-json-profile-auto-switch
- feat(ms-64): cwd cloud.json.profile で自動 profile 切替 (e-1627 follow-up)
- Merge pull request #148 from kurogin23mech-source/ms-64-load-project-consistent-dynamodb
- fix(ms-64): load_project_consistent も DynamoDB に対応 (e-1627 follow-up)
- Merge pull request #147 from kurogin23mech-source/ms-64-e1627-replace-project-dynamodb
- fix(ms-64): cloud push (replace_project) を DynamoDB バックエンドに対応 + auth profile precedence (e-1627 follow-up)
- Merge pull request #146 from kurogin23mech-source/ms-64-e1627-cli-cognito-login
- feat(ms-64): CLI Cognito login flow + /api/auth/config provider switch (e-1627)
- Merge pull request #145 from kurogin23mech-source/ms-64-e1545-cognito-auth
- Merge pull request #144 from kurogin23mech-source/ms-64-e1544-phase2-documents-retros
- feat(ms-64): Cognito User Pool JWT verification path (e-1545)
- feat(ms-64): DynamoDB bus + sessions + envelopes CRUD (e-1544 Phase 3+4)
- feat(ms-64): DynamoDB documents + retros CRUD (e-1544 Phase 2)
- Merge pull request #143 from kurogin23mech-source/ms-64-e1618-server-lib-drift-check
- feat(ms-64): server/ ↔ lib/ name collision drift check (e-1618)
- Merge pull request #142 from kurogin23mech-source/ms-64-e1544-phase1-projects-users
- Merge branch 'main' into ms-64-e1544-phase1-projects-users
- Merge pull request #141 from kurogin23mech-source/hotfix-ms-64-store-name-collision
- hotfix(ms-64): rename server/store.py to store_router (e-1544 follow-up)
- feat(ms-64): DynamoDB projects + users CRUD (e-1544 Phase 1)
- Merge pull request #140 from kurogin23mech-source/ms-64-e1544-store-router-scaffold
- feat(ms-64): store.py router + dynamodb_client skeleton (e-1544 Phase 0)
- Merge pull request #139 from kurogin23mech-source/ms-64-e1542-lambda-entry
- feat(ms-64): Lambda entry point + zip build script (e-1542 Phase A)
- Merge pull request #138 from kurogin23mech-source/feat/ms-64-e1458-profile-url-token-unification
- feat(ms-64): profile-aware desktop + Web UI launch URL + profile list CLI (e-1461)
- feat(ms-64): route api_url + token through profile resolver (e-1458)
- docs(release): update README/CHANGELOG for v0.33.0
- chore(release): bump formula to 0.33.0

## [v0.33.0] - 2026-06-12

- Merge pull request #137 from kurogin23mech-source/feat/ms-54-e1587-cross-project-sessions
- feat(ms-54): cross-project session directory (e-1587)
- Merge pull request #134 from kurogin23mech-source/ms-61-fork-4aa06a
- Merge pull request #135 from kurogin23mech-source/fix/gitignore-iac-private-handoff
- Merge pull request #136 from kurogin23mech-source/feat/ms-64-e1459-bus-profile-aware
- fix(ms-61): e-1573 memo の 3 件 drift を解消、e-1363 を吸収 (e-1574)
- feat(ms-64): channel/bus.mjs を profile-aware に書き換え + Python ↔ Node cross-lang probe test (e-1459)
- feat(ms-61): /beacon-drift-check Skill 新規作成 (e-1572)
- Merge remote-tracking branch 'origin/main' into ms-61-fork-4aa06a
- feat(ms-61): beacon doctor に .beacon/project.json staleness check 追加 (e-1571)
- chore(ms-64): gitignore で iac/ と terraform 作業ファイルを除外 (private 分離)
- feat(ms-61): beacon doctor に Skill ↔ CLI 整合性 check 追加 (e-1570)
- Merge pull request #133 from kurogin23mech-source/ms-64-auth-profile-aware
- feat(ms-64): lib/auth.py profile-aware path resolution (e-1457)
- Merge pull request #132 from kurogin23mech-source/fix/ms-67-fork-stale-cache
- fix(ms-67): force-refresh project cache in forked worktree (e-1554 follow-up)
- Merge pull request #131 from kurogin23mech-source/fix/ms-67-fork-project-json-symlink
- fix(ms-67): symlink .beacon/project.json into forked worktree (e-1554)
- Merge pull request #130 from kurogin23mech-source/ms-67-bclaude-1-worktree-fork-skill-beacon
- feat(ms-67): fork list CLI + /beacon-session-merge-back Skill (e-1553 + e-1552)
- feat(ms-67): session-start surfaces parent info from .beacon/fork.json (e-1551)
- feat(ms-67): /beacon-session-fork Skill + README usage (e-1550)
- feat(ms-67): beacon session fork — 1-command parallel worktree setup (e-1549)
- Merge pull request #129 from kurogin23mech-source/ms-64-cli-skill-profile-ga-aws-byoc
- Merge pull request #128 from kurogin23mech-source/fix/ms-54-bus-heartbeat-shutdown-clear
- fix(ms-54): bus heartbeat body must always include shutdown:false (e-1518)
- Merge pull request #127 from kurogin23mech-source/ms-62-cwd-dm-dm-discover
- fix(ms-62): expose beacon-find-root as a Python console_script (e-1512 follow-up)
- chore(ms-62): mint_fresh_session / find_my_bridge_claim に deprecation warning を追加 (e-1511 phase 1)
- test(ms-62): E2E in-process tests for cloud-first identity (e-1506)
- feat(ms-62): bclaude bootstrap + lib/session.py に server-first 経路を追加 (e-1510)
- feat(ms-62): dm_discover server-first refactor + picker project annotation (e-1502, e-1504)
- feat(ms-62): server identity endpoints (/api/me/projects, /machine, /heartbeat) (e-1509)
- feat(ms-62): Skill 群の .beacon/project.json チェックを walk-up に統一 (e-1512)
- Merge pull request #126 from kurogin23mech-source/fix/ms-43-dm-discover-bus-mjs-needle
- fix(ms-43): dm_discover dual-needle for cross-project bridge discovery (e-1498)
- feat(ms-65): branch/workspace 乖離 warning を session-start と pre-commit に追加 (e-1481)
- feat(ms-65): /beacon-dispatch に Task Mode を追加 (e-1480、e-1221 吸収)
- feat(ms-65): cwd-aware milestone start: main root → worktree, in worktree → in-place (e-1477)
- feat(ms-65): worktree creation helper in lib/worktree.py (e-1476)
- Merge pull request #125 from kurogin23mech-source/fix/ms-43-webui-milestones-disappear-v2-snapshot
- fix(ms-43): rehydrate milestones from subcollection in _on_snapshot v2 broadcast (e-1473)
- docs(release): update README/CHANGELOG for v0.32.0
- chore(release): bump formula to 0.32.0
- feat(ms-64): profile resolver + silent migration (e-1456)

## [v0.32.0] - 2026-06-11

- Merge pull request #124 from kurogin23mech-source/fix/ms-57-e1460-per-bclaude-session-id
- fix(ms-57): per-bclaude session_id via bridges/<sid>.json + pid-tree resolver (e-1460 Phase 1)
- Merge pull request #123 from kurogin23mech-source/feat/ms-60-bus-process-title-e1466
- feat(ms-60): tag bus.mjs process.title by cwd for cross-project pkill safety (e-1466)
- feat(ms-54): bus ack --event + Skill auto-ack forcing function (e-1423)
- fix(ms-54): bridge writes last_active to local session.json (e-1424)
- Merge branch 'main' of https://github.com/kurogin23mech-source/beacon
- feat(ms-60): emit AUTONOMOUS imperative on MCP push route (e-1417 prototype)
- fix(ms-55): doc CLI 2 bugs found in PE cross-project dogfood (e-1413 follow-up)
- docs(release): update README/CHANGELOG for v0.31.0
- chore(release): bump formula to 0.31.0

## [v0.31.0] - 2026-06-10

- docs(ms-54): /beacon-dm-send receipt sleep 短縮 + heredoc 規約展開 (e-1400 + e-1401)
- feat(ms-54): slim ping MCP <channel> notification, defer full body to inbox-hook (e-1403)
- docs(release): update README/CHANGELOG for v0.30.0
- chore(release): bump formula to 0.30.0

## [v0.30.0] - 2026-06-10

- feat(ms-54): unify /beacon-dm-send and /beacon-dm-reply into single Skill
- feat(ms-60): server-side enforce envelope on 5 high-risk endpoints (e-1344)
- feat(ms-61): bus send --to dead-session live-check gate (e-1402)
- docs(release): update README/CHANGELOG for v0.29.0
- chore(release): bump formula to 0.29.0

## [v0.29.0] - 2026-06-10

- fix(ms-60): separate bridge watermark from inbox cursor (autonomous loop hand-off bug)
- fix(ms-60): mirror bus_auto_execute_channels into local project.json (e-1396)
- feat(ms-60): bus.mjs bridge integrated Operation scheduler (e-1390 Phase 1)
- fix(ms-60): mint T2 envelope in operation trigger bus push (e-1393)
- docs(release): update README/CHANGELOG for v0.28.0
- chore(release): bump formula to 0.28.0

## [v0.28.0] - 2026-06-09

- Merge pull request #122 from kurogin23mech-source/ms-60-tier-2-envelope-spec-trigger-awake
- feat(ms-54): structural cross-project DM pre-flight in `bus send` (ms-60 follow-up)
- fix(ms-17): /beacon-dispatch subagent permission preflight (e-1221)
- feat(ms-60): inbox autonomous action block + Skill budget gate (e-1384 / e-1340 Phase B)
- feat(ms-60): operation autonomous wake + execute foundation (e-1340 Phase A)
- docs(ms-60): document operation approve/revoke in README + cmd_help_json (e-1339 drift follow-up)
- feat(ms-60): CLI for operation approve/revoke + show envelope section (e-1339 step 3+4)
- feat(ms-60): server endpoints for operation envelope approve/revoke/list (e-1339 step 2)
- feat(ms-60): approved_actions syntax module + tier-aware envelope wildcards (e-1339 step 1)
- fix(ms-54): align CLI session_id with bus.mjs bridge via local claim file (e-1331 quick fix)
- chore(ms-53): bump TrailNode capability to 2026-06-09.2 (v0.27.0)
- docs(release): update README/CHANGELOG for v0.27.0
- chore(release): bump formula to 0.27.0

## [v0.27.0] - 2026-06-09

- feat(ms-54): session transparency in 4 + 1 layers (e-1369)
- chore(ms-53): bump TrailNode capability version to 2026-06-09.1 (v0.26.0)
- docs(release): update README/CHANGELOG for v0.26.0
- chore(release): bump formula to 0.26.0

## [v0.26.0] - 2026-06-09

- fix(ms-43): Skill drift fixes — session log read, summary path, dm-send PYTHONPATH (e-1360 + e-1364 + e-1362 short-term)
- feat(ms-54): DM read receipt + directory email attribution (e-1348 + e-1349)
- Merge pull request #121 from kurogin23mech-source/worktree-agent-acac8c87f3d98c8b0
- Merge remote-tracking branch 'origin/main' into worktree-agent-acac8c87f3d98c8b0
- Merge pull request #120 from kurogin23mech-source/worktree-agent-a423a1477ff2c7512
- Merge remote-tracking branch 'origin/main' into worktree-agent-a423a1477ff2c7512
- Merge pull request #119 from kurogin23mech-source/worktree-agent-aded8b6de59e28147
- Merge remote-tracking branch 'origin/main' into worktree-agent-aded8b6de59e28147
- Merge pull request #118 from kurogin23mech-source/worktree-agent-aa74255d527ef97b3
- Merge remote-tracking branch 'origin/main' into worktree-agent-aa74255d527ef97b3
- Merge pull request #117 from kurogin23mech-source/worktree-agent-a7444d64d05ceba72
- Merge remote-tracking branch 'origin/main' into worktree-agent-a7444d64d05ceba72
- Merge pull request #116 from kurogin23mech-source/worktree-agent-ad8a083d851515c3c
- Merge remote-tracking branch 'origin/main' into worktree-agent-ad8a083d851515c3c
- Merge pull request #115 from kurogin23mech-source/worktree-agent-a230f49e94281f3fc
- Merge remote-tracking branch 'origin/main' into worktree-agent-a230f49e94281f3fc
- Merge pull request #114 from kurogin23mech-source/worktree-agent-a4f9d65ebf4a08f18
- Merge remote-tracking branch 'origin/main' into worktree-agent-a4f9d65ebf4a08f18
- Merge pull request #113 from kurogin23mech-source/worktree-agent-abc3b4842f58001d7
- Merge remote-tracking branch 'origin/main' into worktree-agent-abc3b4842f58001d7
- Merge pull request #112 from kurogin23mech-source/worktree-agent-a33a6f112dd4333fc
- Merge remote-tracking branch 'origin/main' into worktree-agent-a33a6f112dd4333fc
- refactor(ms-54): heartbeat responsibility separation post Option C (e-1319)
- refactor(ms-54): T5 allowlist drift prevention via cross-lang probe test (e-1306)
- feat(ms-52): record version on release/deploy/push entries (e-1274)
- chore(ms-4): archive legacy tmux/curses dashboard (e-764)
- feat(ms-43): expose Beacon server version in /health + UI display (e-1273)
- feat(ms-53): rewrite manifest to TrailNode declarative install schema (e-1329 a+b)
- refactor(ms-39): unify auth checks via _require_project_role (e-1257)
- feat(ms-44): pip-installable bclaude entry-point for Win/pip paths (e-1328)
- fix(ms-43): reset search state on project switch (e-1023)
- feat(ms-57): Tauri Rust binding for cloud_list_session_logs (e-1073)
- feat(ms-43): add retro/report to Documents scope filter (e-1277)
- chore(trailnode): bump capability version to 2026-06-08.3 (v0.25.0)
- docs(release): update README/CHANGELOG for v0.25.0
- chore(release): bump formula to 0.25.0

## [v0.25.0] - 2026-06-09

- feat(ms-54): cross-project DM discovery + local-mode friendly error (e-1330)
- chore(trailnode): bump capability version to 2026-06-08.2 (v0.24.0)
- docs(release): update README/CHANGELOG for v0.24.0
- chore(release): bump formula to 0.24.0

## [v0.24.0] - 2026-06-09

- Merge pull request #111 from kurogin23mech-source/feat/ms-54-true-heartbeat-poll-gated
- Merge pull request #110 from kurogin23mech-source/feat/ms-54-beacon-dm-skills
- Merge pull request #109 from kurogin23mech-source/fix/ms-44-windows-bash-delegation
- feat(ms-54): /beacon-dm-send + /beacon-dm-reply Skills for stable DM UX
- feat(ms-54): poll-gated heartbeat for true session liveness (e-1318)
- fix(ms-44): skip bash delegation on Windows to avoid mojibake (e-1311)
- chore(trailnode): bump capability version to 2026-06-08.1 + add .trailnodeignore
- docs(release): update README/CHANGELOG for v0.23.0
- chore(release): bump formula to 0.23.0

## [v0.23.0] - 2026-06-08

- Merge pull request #108 from kurogin23mech-source/feat/ms-54-persistence-poisoning-defense
- Merge pull request #107 from kurogin23mech-source/feat/ms-54-bus-envelope-client-adoption
- Merge pull request #106 from kurogin23mech-source/fix/ms-54-envelope-secret-hardening
- Merge pull request #105 from kurogin23mech-source/fix/ms-54-chain-depth-invariant
- feat(ms-54): persistence poisoning defense via --bus-origin flag (e-1293)
- feat(ms-54): bus envelope client-side adoption (e-1290)
- fix(ms-54): refuse-to-start when BEACON_ENVELOPE_SECRET unset in production (e-1291)
- fix(ms-54): tighten chain_depth invariant to 0 <= depth <= limit (e-1292)
- Merge pull request #104 from kurogin23mech-source/feat/ms-54-bus-injection-defense-phase1
- Merge pull request #103 from kurogin23mech-source/fix/ms-17-prepare-worktree-head
- Merge pull request #102 from kurogin23mech-source/fix/ms-43-doc-add-scope
- Merge pull request #101 from kurogin23mech-source/fix/ms-43-copy-link-project-id
- test(ms-54): end-to-end envelope integration tests (e-1155)
- feat(ms-54): wire envelope verify + audit into bus receive path (e-1155)
- fix(ms-17): beacon log --prepare resolves worktree HEAD correctly (e-1227)
- feat(ms-54): bus envelope schema + 9-step verify pipeline (e-1155)
- fix(ms-43): doc add --scope accepts report and retro (e-1222)
- fix(ms-43): Web UI project switch updates URL projectId (e-1275)
- docs(release): update README/CHANGELOG for v0.22.0
- chore(release): bump formula to 0.22.0

## [v0.22.0] - 2026-06-08

- Merge pull request #100 from kurogin23mech-source/ms-54/work
- feat(ms-54): bclaude wrapper with opt-out gate (e-1167)
- feat(ms-54): auto-install with opt-out gate (e-1238 部分)
- feat(ms-54): channel uninstall + opt-out + status (e-1266)
- Merge pull request #99 from kurogin23mech-source/chore/test-suite-state-pollution-and-stale-tests
- chore(test): 状態汚染と stale テストを解消、全 suite を green に戻す
- docs(release): update README/CHANGELOG for v0.21.6
- chore(release): bump formula to 0.21.6

## [v0.21.6] - 2026-06-08

- Merge pull request #98 from kurogin23mech-source/ms-39/work
- Merge pull request #97 from kurogin23mech-source/ms-46/work
- Merge pull request #96 from kurogin23mech-source/ms-43/work
- fix(ms-46): Tauri 検索の local fallback で entry-id 完全一致を拾う (e-1228)
- fix(ms-39): WS endpoint で他人のプロジェクトが読める穴を塞ぐ + 認可ヘルパー統合 (e-1252 + e-1254)
- fix(ms-43): empty state から Existing Project セクションを削除 (e-1253)
- docs(release): update README/CHANGELOG for v0.21.5
- chore(release): bump formula to 0.21.5

## [v0.21.5] - 2026-06-08

- Merge pull request #95 from kurogin23mech-source/fix/ms-44-e1250-isTokenExpired-b64url
- fix(ms-44): isTokenExpired と admin.html も base64url 対応に統一 (e-1250)
- docs(release): update README/CHANGELOG for v0.21.4
- chore(release): bump formula to 0.21.4

## [v0.21.4] - 2026-06-08

- Merge pull request #94 from kurogin23mech-source/fix/ms-44-e1247-base64url-jwt
- fix(ms-44): JWT payload を base64url 対応で decode する (e-1248)
- docs(release): update README/CHANGELOG for v0.21.3
- chore(release): bump formula to 0.21.3

## [v0.21.3] - 2026-06-08

- Merge pull request #93 from kurogin23mech-source/fix/ms-44-e1246-coop-signin
- fix(ms-44): COOP ヘッダで新規ユーザーの Web サインインを通す (e-1246)
- docs(release): update README/CHANGELOG for v0.21.2
- chore(release): bump formula to 0.21.2

## [v0.21.2] - 2026-06-08

- Merge pull request #92 from kurogin23mech-source/fix/ms-43-e1243-empty-state-ui
- chore(ms-43): add join-by-id to WEB_ONLY_ACTIONS allowlist
- fix(ms-43): 新規ユーザーの sign-in 後 empty state UI (e-1243)
- docs(release): update README/CHANGELOG for v0.21.1
- chore(release): bump formula to 0.21.1

## [v0.21.1] - 2026-06-08

- Merge pull request #91 from kurogin23mech-source/fix/ms-44-e1240-oauth-verification
- fix(ms-44): Google OAuth verification 通過の前提整備 (e-1240)
- docs(release): update README/CHANGELOG for v0.21.0
- chore(release): bump formula to 0.21.0

## [v0.21.0] - 2026-06-08

- Merge pull request #90 from kurogin23mech-source/feat/writing-principle-all-skills
- feat(ms-43): 「平易に書く」を Beacon 全体哲学として 21 Skill に直接埋め込み
- Merge pull request #86 from kurogin23mech-source/fix/ms-54-bus-guardrails-1145-1193
- Merge remote-tracking branch 'origin/main' into fix/ms-54-bus-guardrails-1145-1193
- Merge pull request #89 from kurogin23mech-source/ms-43-e-809-documents-ws-push
- Merge pull request #88 from kurogin23mech-source/fix/ms-46-e-750-retros-render
- Merge pull request #87 from kurogin23mech-source/fix/complete-summary-deprecation
- feat(ms-43): Documents タブを WebSocket push で reactive 化 (e-809)
- feat(ms-43): entry-writing-principle CORE doc + 4 Skill prompt 参照を追加
- fix(ms-46): Tauri Documents タブで W22 retro が表示されない問題を修正 (e-750)
- feat(ms-57): complete summary deprecation — Skill / CLI / API / docs (e-1040)
- fix(ms-54): bus guardrail を server/CLI/MCP の 3 経路で揃える (e-1145 + e-1193)
- Merge pull request #83 from kurogin23mech-source/ms-54-dm-server-side-routing
- Merge pull request #84 from kurogin23mech-source/fix/ms-54-bus-directory-heartbeat-e1189
- Merge pull request #82 from kurogin23mech-source/fix-e855-local-lock-deadlock-retry
- Merge pull request #85 from kurogin23mech-source/ms-44/e-854-context-monitor-python-port
- feat(ms-44): context-usage-monitor を Python port、Windows 環境で動作可能に (e-854)
- fix(ms-54): bus directory heartbeat を PostToolUse hook で周期化 (e-1189)
- fix(ms-54): bus DM の 3 層欠陥を server-side enforce で塞ぐ (e-1209)
- fix(ms-44): local mode file lock の高並行 deadlock を解消 (e-855)
- Merge pull request #81 from kurogin23mech-source/ms-59-firestore-subcollection-migration-cli
- feat(ms-44): release.yml fan-out に PyPI publish step を追加 (e-1190)
- fix(ms-53): TrailNode push の version race を Firestore transaction で塞ぐ (e-1163)
- Merge pull request #80 from kurogin23mech-source/ms-tbd-firestore-1mib-unblock
- feat(server): v1→v2 migration endpoint + replace_project v2 hardening (Firestore 1MiB unblock)
- feat(ms-54): bus subcommand を Python dispatch に port + --project flag で cross-project DM/directory 成立 (e-1151)
- fix(ms-57): セッションノートを「意思決定の記録」中心に書き換え (e-1195)
- chore(release): align manifest.json to v0.20.1
- docs(release): update README/CHANGELOG for v0.20.1
- chore(release): bump formula to 0.20.1

## [v0.20.1] - 2026-06-07

- Merge pull request #79 from kurogin23mech-source/ms-44-v0.21.0-bugfix
- fix(ms-44): Win の beacon task add でフィールド永続化、env var 経由パスを修復 (e-1192)
- fix(ms-44): hook command を install-location 非依存に + doctor で shadow 検知 (e-1170)
- fix(ms-54): Win subprocess の npm 解決 + bus.mjs ログ fail-soft 化 (e-1191)
- fix(ms-44): manifest.json recipe を python3 -m pip に + version 0.21.0 同期
- docs(release): update README/CHANGELOG for v0.20.0
- chore(release): bump formula to 0.20.0

## [v0.20.0] - 2026-06-07

- Merge pull request #78 from kurogin23mech-source/ms-54-e1159-mcp-json-os-path-final
- Merge pull request #77 from kurogin23mech-source/ms-54-e1169-channel-packaging
- Merge pull request #76 from kurogin23mech-source/ms-44-e1171-dispatch-channel-parity
- Merge pull request #75 from kurogin23mech-source/ms-54-e1173-session-start-mcp-detect
- feat(ms-54): .mcp.json を OS detect で生成、Windows 経路を成立させる (e-1159)
- feat(ms-54): channel/ packaging for brew + pypi + install-aware path resolution (e-1169)
- feat(ms-44): Python dispatch parity for session id / channel install + drift lint (e-1171)
- feat(ms-54): session-start で beacon-bus channel 未 install を検出 (e-1173)
- docs(release): update README/CHANGELOG for v0.19.0
- chore(release): bump formula to 0.19.0

## [v0.19.0] - 2026-06-07

- Merge pull request #66 from kurogin23mech-source/ms-8-trunk-branch-auto-version
- docs(release): update README/CHANGELOG for v0.18.0
- chore(release): bump formula to 0.18.0
- feat(trailnode/ms-8): auto-version + trunk/branch routing at push time (e-85)

## [v0.18.0] - 2026-06-07

- Merge pull request #74 from kurogin23mech-source/ms-54-trigger-bus-dogfood
- feat(ms-54): beacon channel install + beacon session id + session-start heartbeat (e-1150, e-1152)
- feat(ms-54): beacon-bus Claude Code Channel MCP server (e-1152)
- docs(release): update README/CHANGELOG for v0.17.0
- chore(release): bump formula to 0.17.0

## [v0.17.0] - 2026-06-07

- Merge pull request #73 from kurogin23mech-source/ms-54-trigger-bus-dogfood
- feat(ms-54): mirror trigger fires to the bus — UC2 dogfood (e-1136)
- Merge pull request #72 from kurogin23mech-source/ms-54-bus-armed-skill
- feat(ms-54): /beacon-bus-armed Skill — autonomous DM mode setup
- docs(release): update README/CHANGELOG for v0.16.0
- chore(release): bump formula to 0.16.0

## [v0.16.0] - 2026-06-07

- Merge pull request #71 from kurogin23mech-source/ms-54-bus-budget
- feat(ms-54): bus budget gate — replies require human approval by default (e-1000)
- docs(release): update README/CHANGELOG for v0.15.0
- chore(release): bump formula to 0.15.0

## [v0.15.0] - 2026-06-07

- Merge pull request #70 from kurogin23mech-source/ms-54-bus-inbox
- feat(ms-54): bus inbox hook — auto-surface unread DMs in AI context (e-1140)
- docs(release): update README/CHANGELOG for v0.14.2
- chore(release): bump formula to 0.14.2

## [v0.14.2] - 2026-06-07

- Merge pull request #69 from kurogin23mech-source/ms-54-bus-index-fix
- fix(ms-54): drop Firestore composite-index requirement for bus channel filter
- docs(release): update README/CHANGELOG for v0.14.1
- chore(release): bump formula to 0.14.1

## [v0.14.1] - 2026-06-07

- Merge pull request #68 from kurogin23mech-source/ms-54-directory-fix
- fix(ms-54): surface session_id in directory query response (e-1134 hotfix)
- docs(release): update README/CHANGELOG for v0.14.0
- chore(release): bump formula to 0.14.0

## [v0.14.0] - 2026-06-07

- Merge pull request #67 from kurogin23mech-source/ms-54-directory
- Merge pull request #65 from kurogin23mech-source/ms-54-rendezvous
- Merge pull request #64 from kurogin23mech-source/ms-54-ws-push
- feat(ms-54): bus directory query — pick DM target by user/machine/agent (e-1134)
- feat(ms-54): rendezvous CLI + delivery field (e-999 + e-1135 minimal)
- feat(ms-54): per-recipient bus cursor — at-least-once + forward-only ack (e-998)
- feat(ms-54): WS push for /bus events (e-997)
- feat(ms-54): minimal /bus transport — POST + append-only + cursor read (e-996)
- docs(release): update README/CHANGELOG for v0.13.0
- chore(release): bump formula to 0.13.0

## [v0.13.0] - 2026-06-06

- Merge pull request #63 from kurogin23mech-source/ms-58-beacon-trailnode-dogfood
- Merge pull request #62 from kurogin23mech-source/ms-57-e2e-hermetic-fix
- fix(ms-57): make e2e script hermetic vs CLAUDE_CODE_SESSION_ID (e-1042)
- feat(ms-58): add TrailNode manifest.json for org-internal distribution (e-1128)
- Merge pull request #61 from kurogin23mech-source/fix/auto-note-content-enrichment
- fix(monitor): auto-enrich threshold notes so they aren't empty templates
- Merge pull request #60 from kurogin23mech-source/ms-57-notes-session-log-summary
- test(ms-57): fix Windows teardown — restore CWD before TemporaryDirectory cleanup
- merge: integrate Windows ms-57 e-1035 part 1 (env-var-first session id)
- test(ms-57): single-host e2e script for session log flow (e-1042 partial)
- feat(ms-57): Session tab with Notes/Session Log toggle (e-1041)
- feat(ms-57): deprecate beacon summary + drop Session Context banner (e-1040)
- feat(ms-57): session-end + rescue CLI commands (e-1038 + e-1039)
- feat(ms-57): session log collection + aggregation core (e-1037)
- feat(ms-57): tag session notes with session_id (e-1036)
- feat(ms-57): tag commit / PR entries with meta.session_id (e-1062)
- feat(ms-57): cloud session registry + debounced heartbeat sync (e-1063)
- Merge pull request #59 from kurogin23mech-source/ms-5/server-app-type-gate
- feat(ms-57): mint local session_id + heartbeat per CLI exec (e-1035 slice 1)
- feat(trailnode/ms-5): accept 4 capability types in server push gate
- Merge pull request #58 from kurogin23mech-source/trailnode/ms-3-e32-org-authz
- feat(trailnode): manifests endpoint に org 越境防止認可を追加 (ms-3 e-32)
- Merge pull request #57 from kurogin23mech-source/trailnode/ms-3-e31-list-endpoint
- feat(trailnode): manifests 一覧 endpoint で差分同期を提供 (ms-3 e-31)
- Merge pull request #56 from kurogin23mech-source/trailnode/ms-3-pull-timestamp-fix
- fix(trailnode): pull endpoint で updated_at / deleted_at の Timestamp 変換漏れ (ms-3 e-30 hotfix)
- docs(release): update README/CHANGELOG for v0.12.0
- chore(release): bump formula to 0.12.0
- feat(ms-57): session identity primitive (e-1035 part 1)

## [v0.12.0] - 2026-06-05

- Merge pull request #55 from kurogin23mech-source/ms-37-onboarding-polish
- feat(ms-37): onboarding polish — brew caveats / setup output / init defaults (e-519/520/539/540)
- Merge pull request #54 from kurogin23mech-source/ci-drop-macos-x64
- ci: drop macOS-x64 from Desktop build matrix
- Merge pull request #53 from kurogin23mech-source/trailnode/ms-3-manifest-fields-e30
- Merge pull request #51 from kurogin23mech-source/ms-37-windows-hook-path
- fix(ms-37): migrate stale PostCompact hook path too (e-1043 follow-up)
- Merge pull request #52 from kurogin23mech-source/ms-14-purge-owner-only
- fix(ms-37): write bash-safe hook paths on Windows (e-1043)
- feat(ms-14): purge endpoints owner-only — defense against multi-user misops (e-1030)
- feat(trailnode): manifest に updated_at + deleted_at field を追加 (ms-3 e-30)
- Merge pull request #50 from kurogin23mech-source/remove-trash-tab-and-restore
- refactor(ms-14): remove Trash tab + restore endpoints (e-1006/e-1011/e-827 cancel)
- docs(release): update README/CHANGELOG for v0.11.1
- chore(release): bump formula to 0.11.1

## [v0.11.1] - 2026-06-05

- Merge pull request #49 from kurogin23mech-source/fix-ms43-tauri-state-init
- fix(ms-43): Tauri cloud project bounce + WS-open flash (e-1024)
- docs(release): update README/CHANGELOG for v0.11.0
- chore(release): bump formula to 0.11.0

## [v0.11.0] - 2026-06-05

- Merge pull request #48 from kurogin23mech-source/ms-43-entry-id-search
- Merge pull request #45 from kurogin23mech-source/ms-14-changelog-sweep
- Merge pull request #47 from kurogin23mech-source/ms-53/orgs
- feat(ms-43): search q matches entry id / doc_id by number (e-1010)
- feat(trailnode): organization scope — server-side OrgService + capability authz (TrailNode ms-6)
- Merge pull request #44 from kurogin23mech-source/ms-31-stop-hook-fix
- Merge pull request #43 from kurogin23mech-source/ms-14-cloud-trash-restore
- Merge pull request #46 from kurogin23mech-source/ms-39-hook-observability
- fix(ms-39): flatten newlines in bash hook debug log (review nit, e-940)
- feat(ms-39): BEACON_HOOK_DEBUG observability for post-commit hook (e-940)
- feat(ms-14): cloud changelog persistence + 30-day trash sweep (e-825 + e-826/e-991 retention)
- fix(ms-31): Stop hook must use hookSpecificOutput.additionalContext
- feat(ms-14): cloud doc trash/restore + unified Trash tab (e-826 / e-991)
- Merge pull request #42 from kurogin23mech-source/ms-14-doc-trash-restore
- feat(ms-14): doc trash + restore in local mode (e-973)
- Merge pull request #41 from kurogin23mech-source/ms-32-done-reason-required
- feat(ms-32): require --reason on done/observe + Web UI auto-link reasons (e-976)
- Merge pull request #40 from kurogin23mech-source/ms-14-trash-export
- feat(ms-14): trash + restore for milestones and tasks (e-826)
- feat(ms-14): project export/import — full snapshot ZIP backup (e-828)
- docs(release): update README/CHANGELOG for v0.10.0
- chore(release): bump formula to 0.10.0

## [v0.10.0] - 2026-06-05

- Merge pull request #39 from kurogin23mech-source/ms-52-remove-health-assertion
- fix(ms-52): drop /health version assertion from release.yml fan-out (e-960 retro)
- Merge pull request #38 from kurogin23mech-source/ms-53-trailnode-capability-registry-beacon
- feat(ms-53): TrailNode capability registry on beacon-api-prod (e-978/979/980)
- docs(release): update README/CHANGELOG for v0.9.2
- chore(release): bump formula to 0.9.2

## [v0.9.2] - 2026-06-05

- Merge pull request #37 from kurogin23mech-source/ms-52-fix-fanout-perms
- fix(ms-52): release.yml fan-out needs `actions: write` permission (e-960 finding #3)
- docs(release): update README/CHANGELOG for v0.9.1
- chore(release): bump formula to 0.9.1

## [v0.9.1] - 2026-06-05

- Merge pull request #36 from kurogin23mech-source/ms-52-fix-fanout-pyimport
- fix(ms-52): release.yml fan-out + /health import-safe version source (e-960 finding)
- docs(release): update README/CHANGELOG for v0.9.0
- chore(release): bump formula to 0.9.0

## [v0.9.0] - 2026-06-05

- Merge pull request #35 from kurogin23mech-source/ms-52-forcing-function-release-pipeline-ai
- feat(ms-52): auto-derive release-<version> trigger from latest v* tag (e-952)
- feat(ms-52): /beacon-push routes release.yml-first, demotes manual tag (e-959)
- feat(ms-52): release-due trigger fires on feat 3+ or fix 5+ since last v* tag (e-958)
- fix(ms-52): release-build.yml accepts v* tags + release.yml fans out (e-954)
- fix(ms-52): release.yml fans out to deploy-cloud-run.yml + /health version (e-953)
- docs(ms-43): list ms-40/41 skills in README Skills table (e-682)
- chore: gitignore .beacon-design/ (internal pitch deck work area)
- docs(release): update README/CHANGELOG for v0.8.0
- chore(release): bump formula to 0.8.0

## [v0.8.0] - 2026-06-03

## [v0.7.2] - 2026-06-03

## [v0.7.1] - 2026-06-03

## [v0.7.0] - 2026-06-03

## [v0.6.0] - 2026-06-02

- docs(readme): cross-OS install matrix + pipx + Tauri Desktop guidance (ms-44)
- feat(skill,ci): OS-specific Tauri install hint + cross-OS CLI smoke job (ms-44 e-778 e-779)
- feat(cli): wire 'beacon cloud push' / 'cloud pull' on PowerShell (ms-44)
- feat(skill): beacon-log Step 1.9 adds "unverified facets" tag for audit pointers (ms-5)
- fix(windows): UTF-8 follow-up for subprocess + legacy trigger files (ms-44 #19 #21)
- fix(windows): force UTF-8 on stdout + all open() + wire bash-less cloud nav (ms-44 #19 #20 #21)
- fix(windows): cross-platform file locking + symlink-with-copy-fallback for pre-commit hook (ms-44)
- feat(cli): wire pr / issue / member subcommands through PowerShell dispatch (ms-44)
- feat(cli): wire 'beacon auth login|logout|status' through PowerShell dispatch (ms-44)
- Merge pull request #17 from kurogin23mech-source/ms-44/work
- feat(skill): AC-based self-judgment in beacon-log Step 1.9 (ms-5 e-791)
- feat(cli,dispatch): wire 'beacon skill install' for PowerShell + wheel layout (ms-44 e-777 e-813)
- feat(packaging,hooks): bundle skills in wheel + cross-platform Python hooks (ms-44 e-777 e-811 e-812)
- Merge pull request #15 from kurogin23mech-source/ms-44/work
- feat(cli): PowerShell-native dispatch for Day-1 commands (ms-44 e-695)
- Merge pull request #13 from kurogin23mech-source/ms-44/work
- fix(ui): preserve IME composition in the global search box
- fix(ui): partial-render falls back to full render when shell isn't mounted
- feat(ui): move search to shell header, exclusive facet chips, preserve search on result click
- fix(ui): preserve loading state instead of bouncing back to project selector
- Merge pull request #12 from kurogin23mech-source/ms-43/work
- feat(ui,cli): deep-link hashes + milestone done/observe --reason + skill install PostCompact (ms-43 e-618 e-672 e-674)
- feat(skill): session-start/end + retro magic (ms-43 e-564 e-566 e-567 e-568 e-570)
- feat(ui): cross-entity search + Documents tab filter (ms-43 e-616 e-631)
- feat(packaging): scaffold pyproject.toml + Python entry-point for cross-OS install (ms-44 e-695)
- fix(hook): post-commit-hook drops heredoc bodies before pattern match (ms-43 e-613)
- feat(packaging): scaffold cross-platform release pipeline + WinGet/cask manifests (ms-44 e-696)
- docs(install): rewrite INSTALL.md OS-by-OS for cross-platform distribution (ms-44 e-697/e-730)
- fix(cli): task update accepts motivation/ac/behavior/priority (ms-43 e-553)
- docs(deploy): fix gcloud flag — --build-arg → --set-build-env-vars (ms-46)
- fix(arch): task counts disappear in Tauri — 3-layer defense (ms-46 e-755/756/757)
- feat(arch): DataSource adapter — SHARED data fetching through interface (ms-46 e-728)
- feat(arch): renderShell to SHARED — single-source tab-bar/section dispatch (ms-46 e-743)
- feat(arch,skill): Tauri Operations/Notes tab + browser-explicit open (ms-46)
- fix(arch): entry-detail-modal default to display:none in CSS (ms-46)
- feat(arch): frontend drift detector + pre-commit integration (ms-46 e-744)
- feat(tauri): cloud WS live-update client for cloud mode (e-738 / e-723)
- feat(tauri): cloud auth token commands + CSP for WS connect (prep for e-723)
- refactor(arch): move entry-detail-modal into SHARED for Tauri parity (ms-46)
- refactor(arch): handleCommonAction in SHARED + auto-install dev hook (ms-46)
- feat(arch): unify connection indicator + add Tauri rebuild warning hook (ms-46)
- feat(desktop,arch): kill Tauri 2s polling + add pre-commit dist sync hook (ms-46)
- docs(release): update README/CHANGELOG for v0.5.0
- chore(release): bump formula to 0.5.0

## [v0.5.0] - 2026-05-30

- feat(skill,desktop): UC1/UC2 polish — Tauri scroll fix, /beacon-init v2.2, session-start v0.7, single-instance plugin
- feat(skill): /beacon-init v2 — explicit name/objective form, post-confirmation mkdir, opt-in roadmap chain
- feat(skill): align init/roadmap/vision/spec with バイブコーダー Philosophy
- fix(skill): /beacon-spec stops asking confirmation for AI-inferable sections
- fix(skill): /beacon-spec ends at MS-planning question, not task-by-task
- fix(skill): show document save location after vision/spec write (mode-aware)
- fix(skill): /beacon-init adds one terminal confirmation before irreversible init
- feat(skills): polish sweep — "Act first, confirm next-step" across dialog skills
- fix(skill): explain what /beacon-roadmap does instead of just naming the slash command
- feat(skill): default to local mode, cloud opt-in only on explicit mention
- Merge pull request #11 from kurogin23mech-source/ms-42/work
- feat(pr,skill): review_history + /beacon-onboard + push generalisation (e-609,e-627,e-578)
- feat(pr,dispatch,release): commit↔PR link + dynamic dep check + notify trigger (e-610,e-602,e-580)
- feat(deploy,ms,ui): rollback + decision-trail + version badge (e-581,e-630,e-587)
- feat(release): maintainer banner + explicit preview + README/CHANGELOG auto-update (e-577,e-579,e-582)
- feat(member): project members CLI + MS owner/assignee (e-624, e-625)
- feat(pr,dispatch): pr show + dispatch failure/merge protocols (e-608,e-600,e-601)
- feat(skill): /beacon-pr-create + MS auto-inference (e-606, e-607, e-611)
- feat(cli): beacon update — brew upgrade + skill install in one shot (e-576)
- Merge pull request #10 from kurogin23mech-source/ms-40/work
- feat(skill): /beacon-retrospect for AI-driven project history retrospection (e-621)
- feat(search): unified search API across all Beacon entities (e-622)
- fix(cli): use plain var instead of 'local' in top-level cycle dispatch (e-585)
- feat(incident): beacon incident list + auto-escalate proposal (e-591, e-619)
- feat(retro): persistent retro_due trigger + CORE doc promotion (e-574, e-575)
- feat(cycle): lib/cycle.py + cwd-aware Skill (e-551, e-586, e-588, e-598, e-550)
- Merge pull request #9 from kurogin23mech-source/ms-41/work
- feat(spec): promote SPEC creation on milestone add (warning only)
- feat(skill): /beacon-spec for SPEC (要求書/判断軌跡) creation
- fix(skill): correct Web UI URL format (path → query string)
- Merge pull request #8 from kurogin23mech-source/ms-39/work
- feat(skill): structural Incident close-and-report flow (e-595)
- fix(ws): break the auth-fail reconnect loop (e-639)
- feat(hooks): dynamic context limit + PostCompact orientation (e-561, e-565)
- feat(schema): default new projects to v2 β subcollection (e-632 step 1d/1e)
- refactor(api): route all project writes through apply_operation (e-632 step 1c)
- feat(operations): introduce apply_operation layer (e-632 step 1a/1b)
- feat(skill): /beacon-review series + companion file support
- fix(cli): beacon task cancel routing was hitting deprecated delete handler
- chore(release): bump formula to 0.4.0

