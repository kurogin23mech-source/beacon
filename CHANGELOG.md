# Changelog

All notable changes to Beacon are documented here. See [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for format.

## [v0.52.1] - 2026-07-01

- Merge pull request #312 from kurogin23mech-source/ms-95-fork-705831
- fix(ms-95): e-2755 — bus.mjs 60s trigger check tick を廃止して orphan leak 経路を根絶
- docs(release): update README/CHANGELOG for v0.52.0
- chore(release): bump formula to 0.52.0

## [v0.52.0] - 2026-07-01

- Merge pull request #311 from kurogin23mech-source/feat/ms-97-e2650-slot-done-precondition
- Merge pull request #310 from kurogin23mech-source/feat/ms-95-e2726-task-done-forcing-function
- feat(ms-97): e-2650 — Trek slot done 構造防御 (= project pool 真値源 + AC28 manual 明文化)
- feat(ms-95): e-2726 — task done evidence gate (#8 phantom done forcing function)
- Merge pull request #309 from kurogin23mech-source/feat/ms-97-fresh-joiner-chain-e2636-e2637-e2638
- feat(ms-97): fresh joiner chain e-2636/e-2637/e-2638 — 同 user 別 session 2 件目 join silent no-op 解消 + welcome tick bootstrap
- Merge pull request #308 from kurogin23mech-source/fix/ms-95-e2723-leader-digest-same-user-collapse
- fix(ms-95): e-2723 — leader-digest が同 user の executor session に collapse する病理を解消 (#16)
- Merge pull request #307 from kurogin23mech-source/feat/ms-97-spec-chain-e2711-e2707-e2709
- Merge pull request #306 from kurogin23mech-source/feat/ms-95-e2640-cross-project-scope-entries
- feat(ms-97): SPEC chain Step 3-5 — Level 3 imperative + leader-digest aggregate + Skill idempotent (e-2711/e-2707/e-2709)
- feat(ms-95): e-2640 — cross-project Trek detail scope-entries endpoint (data モデル独立性を API/UI 層まで貫通)
- Merge pull request #305 from kurogin23mech-source/fix/ms-95-e2710-allowlist-invariants-observability
- Merge pull request #304 from kurogin23mech-source/fix/ms-97-e2706-review-trigger-states
- fix(ms-95): e-2710 — bus_auto_execute_channels invariants pin + downgrade diag frame
- fix(ms-97): e-2706 — REVIEW_TRIGGER_STATES で leader_review notify 復活 (5-state migration drift)
- docs(release): update README/CHANGELOG for v0.51.5
- chore(release): bump formula to 0.51.5

## [v0.51.5] - 2026-06-29

- Merge pull request #303 from kurogin23mech-source/fix/trigger-check-runaway
- fix: stop runaway trigger check processes
- docs(release): update README/CHANGELOG for v0.51.4
- chore(release): bump formula to 0.51.4

## [v0.51.4] - 2026-06-28

- Merge pull request #302 from kurogin23mech-source/fix/ms-97-trek-scheduler-import-flat-layout
- fix(ms-97): lib/trek_scheduler.py lazy import for Cloud Run flat layout (= dogfood 全 tick で executor skip 真因)
- docs(release): update README/CHANGELOG for v0.51.3
- chore(release): bump formula to 0.51.3

## [v0.51.3] - 2026-06-28

- Merge pull request #301 from kurogin23mech-source/fix/ms-97-fanout-empty-targets-and-broadcast-delivery
- fix(ms-97): broadcast-fallback DM delivery + executor target diagnostic
- docs(release): update README/CHANGELOG for v0.51.2
- chore(release): bump formula to 0.51.2

## [v0.51.2] - 2026-06-28

- Merge pull request #300 from kurogin23mech-source/fix/ms-97-project-ref-and-session-resolution
- fix(ms-97): project_ref + session_id resolution unification (= dogfood structural fixes)
- docs(release): update README/CHANGELOG for v0.51.1
- chore(release): bump formula to 0.51.1

## [v0.51.1] - 2026-06-28

- Merge pull request #299 from kurogin23mech-source/fix/ms-97-cloud-migration-endpoint
- fix(ms-97): cloud-mode migration endpoint (= live trek migrate without local store)
- docs(release): update README/CHANGELOG for v0.51.0
- chore(release): bump formula to 0.51.0

## [v0.51.0] - 2026-06-28

- Merge pull request #298 from kurogin23mech-source/feat/ms-97-phase7c-blanket-and-logs
- feat(ms-97): Phase 7-C — AC24 blanket approval + AC26/27 structured logs (ms-97 FINAL)
- Merge pull request #297 from kurogin23mech-source/feat/ms-97-phase7b-ac22-succession
- feat(ms-97): Phase 7-B — AC22 auto-succession (priority + threshold + consent + escalation)
- Merge pull request #296 from kurogin23mech-source/feat/ms-97-phase7a-ac20-ac21-completion-ready
- feat(ms-97): Phase 7-A — AC20/AC21/G6 completion_ready + summary_sent + meta seed
- Merge pull request #295 from kurogin23mech-source/feat/ms-97-phase6-ac15-invite-consent
- feat(ms-97): Phase 6 — AC15 invite consent (accident-time leader candidate notice + 1 hop consent hook)
- Merge pull request #294 from kurogin23mech-source/feat/ms-97-phase5-ui-cli-skill
- Merge pull request #293 from kurogin23mech-source/feat/ms-97-phase4-ac10-ac13-ac14-ac32
- feat(ms-97): Phase 5 — UI/CLI/Skill 動線整理 + Manual auto-show + Skill 重複解消
- feat(ms-97): Phase 4 — AC10/AC13/AC14/AC32 (MS slot precedence + leader/executor auth boundaries + halt 完全化)
- Merge pull request #292 from kurogin23mech-source/feat/ms-97-phase3-ac16-fanout-bypass
- feat(ms-97): Phase 3 — AC16/AC18/AC19/G3/G4 — fanout members iterate + DM bypass session-grain
- Merge pull request #291 from kurogin23mech-source/feat/ms-97-phase2-ac7-scope-strict
- feat(ms-97): Phase 2 — AC7/AC8/AC31/AC12 scope narrowing strict (3-layer reject + grandfather warning)
- Merge pull request #290 from kurogin23mech-source/feat/ms-97-phase1-ac6-members-session-keyed
- feat(ms-97): e-2658 Phase 1 — AC6 members[] cutover to session_id keyed (phase-gated dual-mode)
- Merge pull request #289 from kurogin23mech-source/feat/ms-97-e2658-phase0a-remainder
- Merge pull request #288 from kurogin23mech-source/feat/ms-97-e2658-phase0a-scaffolding
- feat(ms-97): e-2658 Phase 0-A remainder + e-2659 test scaffolding (migration script + alarming + red tests for AC6/7/16-19/34)
- feat(ms-97): e-2658 Phase 0-A scaffolding — add members_legacy_backup field + migration_phase tracker
- docs(release): update README/CHANGELOG for v0.50.1
- chore(release): bump formula to 0.50.1

## [v0.50.1] - 2026-06-28

- Merge pull request #287 from kurogin23mech-source/feat/ms-95-dogfood-tick-fixes
- fix(ms-95): e-2644/e-2645/e-2646 dogfood tick fixes (snapshot + narrow + 24h)
- docs(release): update README/CHANGELOG for v0.50.0
- chore(release): bump formula to 0.50.0

## [v0.50.0] - 2026-06-28

- Merge pull request #286 from kurogin23mech-source/feat/ms-95-e2639-tick-via-dm
- feat(ms-95): e-2639 migrate Trek tick to dm channel transport (per-member fanout)
- Merge pull request #284 from kurogin23mech-source/feat/ms-97-e2626-scope-add-pending
- feat(ms-97): e-2626 scope-add pending_user_approval flow (AC23)
- Merge pull request #282 from kurogin23mech-source/feat/ms-97-phase1b-ui
- Merge pull request #283 from kurogin23mech-source/feat/ms-97-phase1b-backend
- feat(ms-97): e-2613 tick fire lazy start (AC33)
- test(ms-97): Phase 1b UI invariants (AC1〜AC8/AC23 を tests で pin)
- feat(ms-97): e-2609 scope-add/remove approval surface (AC23/25)
- feat(ms-97): e-2608 leader_session_id 表示 + project-wide warning (AC5/8/31)
- feat(ms-97): e-2612 halt 中 tick fire 全停止 (AC32)
- feat(ms-97): e-2607 Trek list を user member 全 Trek に切替 (AC2)
- feat(ms-97): e-2606 ハンバーガー復活 + tab bar 削除 (AC1/3/4)
- feat(ms-97): e-2611 scope-remove approval flow (AC25)
- docs(release): update README/CHANGELOG for v0.49.2
- chore(release): bump formula to 0.49.2

## [v0.49.2] - 2026-06-27

## [v0.49.1] - 2026-06-27

## [v0.49.0] - 2026-06-25

- Merge pull request #267 from kurogin23mech-source/fix/ms-95-e2441-project-export-import-sys-modules-leak
- fix(ms-95): e-2441 project_export_import raw sys.modules.pop (-8 test_api)
- Merge pull request #266 from kurogin23mech-source/fix/ms-95-e2441-milestone-purge-ms-start-sys-modules-leak
- fix(ms-95): e-2441 milestone_purge + ms_start raw sys.modules.pop (-8 fails)
- Merge pull request #265 from kurogin23mech-source/fix/ms-95-e2441-env-integration-setup-teardown-delayed
- fix(ms-95): e-2441 env_integration + op_env delayed setup/teardown (-25 fails)
- Merge pull request #264 from kurogin23mech-source/fix/ms-95-e2441-revert-263-env-integration-teardown
- Revert "Merge pull request #263 from kurogin23mech-source/fix/ms-95-e2441-envelope-integration-module-teardown"
- Merge pull request #263 from kurogin23mech-source/fix/ms-95-e2441-envelope-integration-module-teardown
- fix(ms-95): e-2441 envelope_integration module mutations leak (-15 fails)
- Merge pull request #262 from kurogin23mech-source/fix/ms-95-e2441-operation-envelopes-module-teardown
- fix(ms-95): e-2441 operation_envelopes module mutations leak (-14 fails)
- Merge pull request #261 from kurogin23mech-source/fix/ms-95-e2441-trailnode-fixture-real-fc-db
- fix(ms-95): e-2441 trailnode fixture patches wrong firestore_client (-7 fails)
- Merge pull request #260 from kurogin23mech-source/fix/ms-95-e2441-ms73-dm-recipient-session-env
- fix(ms-95): e-2441 ms73 dm dispatch missing recipient_session env (-1 fail)
- Merge pull request #259 from kurogin23mech-source/fix/ms-95-e2441-ws-push-cross-instance-max-3
- fix(ms-95): e-2441 ws_push_cross_instance test pins old max-instances=1 (-1 fail)
- Merge pull request #258 from kurogin23mech-source/fix/ms-95-e2441-treks-tab-test-outdated
- fix(ms-95): e-2441 treks tab tests outdated post-e-2251 / e-2226 (-5 fails)
- Merge pull request #257 from kurogin23mech-source/fix/ms-95-e2441-credentials-reload-profile-class-identity
- fix(ms-95): e-2441 test_credentials reload-of-profile breaks test_profile (-12 fails)
- Merge pull request #256 from kurogin23mech-source/fix/ms-95-e2440-install-hooks-shutil-which-mock
- fix(ms-95): e-2440 install_hooks shutil.which mock — restore CI green (-2 fails)
- Merge pull request #254 from kurogin23mech-source/fix/ms-95-e2438-test-fixture-sys-modules-leak
- Merge pull request #255 from kurogin23mech-source/fix/ms-95-e2448-max-instances-3
- fix(ms-95): e-2448 bump Cloud Run max-instances 1 → 3 to relieve 429 saturation
- fix(ms-95): e-2438 test fixture sys.modules.pop leak (-14 fails)
- Merge pull request #253 from kurogin23mech-source/fix/ms-95-e2446-inbox-hook-cursor-prime
- fix(ms-95): e-2446 inbox-hook cursor priming — stop fork session storm-flood
- Merge pull request #252 from kurogin23mech-source/fix/ms-95-e2407-ws-protocol-redesign
- fix(ms-95): e-2407/e-2437 WS signal-only protocol — redesign 19 stale tests
- Merge pull request #251 from kurogin23mech-source/fix/ms-95-e2407-envelope-invitation
- fix(ms-95): e-2407 test_envelope_integration sys.modules alias (-9 fails)
- Merge pull request #250 from kurogin23mech-source/fix/ms-95-e2407-test-api
- fix(ms-95): e-2407 test_api mirror project mocks on store_router (-17 fails)
- Merge pull request #249 from kurogin23mech-source/fix/ms-95-e2407-bus-transport
- fix(ms-95): e-2407 test_bus_transport sys.modules alias breaks 38-test pollution cascade (-47 fails)
- Merge pull request #248 from kurogin23mech-source/fix/ms-95-e2407-store-router-reexport
- fix(ms-95): e-2407 store_router exposes _db / get_db / COLLECTION (~10 test errors + 1 prod silent failure)
- fix(ms-95): _resolve_session_id を resolve_active_session_id に切替 (e-2419)
- Revert "fix(ms-95): _resolve_session_id を resolve_active_session_id に切替 (e-2419)"
- fix(ms-95): _resolve_session_id を resolve_active_session_id に切替 (e-2419)
- Merge pull request #246 from kurogin23mech-source/fix/ms-95-e2411-cloud-list-owner-email
- feat(ms-95): e-2411 beacon cloud list shows owner email per project
- Merge pull request #245 from kurogin23mech-source/e-746/work
- Merge pull request #244 from kurogin23mech-source/e-2370/work
- Merge pull request #243 from kurogin23mech-source/e-1490/work
- fix(ms-95): e-746 cloud-mode CLI writes API response back to local .beacon/project.json (dead cache fix)
- fix(ms-95): e-2370 prevent install prompt from hallucinating GitHub owner/repo (use git remote, fall back to placeholder)
- fix(ms-95): e-1490 refresh bridges/<sid>.json pid/parent_pid/cwd periodically in bus.mjs poll loop
- Merge pull request #242 from kurogin23mech-source/e-1905/work
- Merge pull request #241 from kurogin23mech-source/e-2305/work
- Merge pull request #240 from kurogin23mech-source/e-2280/work
- test(ms-95): e-1905 mock Firestore in test_bus_directory email stamping cases (local-env pass)
- fix(ms-95): e-2305 session close → live cleanup propagation fix
- feat(ms-95): e-2280 bus send live-check structure + auto-swap for stale session_id
- Merge pull request #239 from kurogin23mech-source/e-2215/work
- Merge pull request #238 from kurogin23mech-source/e-2288/work
- Merge pull request #237 from kurogin23mech-source/e-2405/work
- fix(ms-95): e-2215 WebUI hamburger menu open is now instant (lazy fill pattern)
- fix(ms-95): e-2288 beacon member list shows project owner as first row
- fix(ms-95): e-2405 pre-flight subset check uses project-wide id set (MS move false-positive fix)
- Merge pull request #236 from kurogin23mech-source/e-2348/work
- Merge pull request #235 from kurogin23mech-source/e-2005/work
- feat(ms-61): e-2348 add retro subcommand to Windows beacon_cli/dispatch.py
- feat(ms-61): e-2005 PR lifecycle ↔ MS progress integration forcing function
- Merge pull request #234 from kurogin23mech-source/fix/ms-95-e2320-trek-scope-audit-log
- Merge pull request #233 from kurogin23mech-source/e-1892/work
- Merge pull request #232 from kurogin23mech-source/e-1778/work
- feat(ms-95): e-2320 Trek scope mutation audit log + structural caller boundary pin
- feat(ms-61): e-1892 /beacon-review-apply Skill prompts to activate parent MS after new MS issuance
- docs(ms-61): e-1778 enrich /beacon-cloud push warning with concrete past pathology examples
- Merge pull request #230 from kurogin23mech-source/fix/ms-95-e2308-trek-ttl-subagent-dispatch
- Merge pull request #229 from kurogin23mech-source/e-1843/work
- Merge pull request #228 from kurogin23mech-source/fix/ms-95-e1667-bridge-poll-timeout
- Merge pull request #227 from kurogin23mech-source/e-1825/work
- Merge pull request #226 from kurogin23mech-source/e-1776/work
- Merge pull request #225 from kurogin23mech-source/fix/ms-95-e2406-webui-nonactive-ms-entries
- Merge pull request #231 from kurogin23mech-source/fix/ms-61-e2349-ci-server-requirements
- fix(ms-61): e-2349 also install httpx for starlette TestClient
- fix(ms-61): e-2349 install server/requirements.txt + pyyaml in pytest CI workflow
- feat(ms-95): e-2308 add per-task TTL extension (= leader-side primitive for Agent subagent dispatch)
- test(ms-61): e-1843 verify session-start Operation activation discussion logic across pending states
- fix(ms-95): e-1667 bridge poll loop timeout / watchdog (root cause for 43-min stale heartbeat regression)
- feat(ms-61): e-1825 enforce parallel subagent cap (≤3) in /beacon-dispatch
- feat(ms-61): e-1776 add cloud mode state file consistency check to beacon doctor
- fix(ms-95): e-2406 lazy-fetch entries on expand for non-active milestones
- Merge pull request #223 from kurogin23mech-source/feat/ms-95-e2369-pr-approve-autodone
- Merge pull request #224 from kurogin23mech-source/feat/ms-95-e1454-bus-listen-retry
- Merge pull request #222 from kurogin23mech-source/feat/ms-95-e2007-current-project-id
- feat(ms-95): e-2369 auto-done bound tasks at beacon pr approve time
- fix(ms-95): e-1454 bus listen resilience: exponential backoff on transient network errors
- fix(ms-95): e-2007 pin _current_project_id cloud-mode fallback with tests
- Merge pull request #221 from kurogin23mech-source/feat/ms-84-e2339-migrate-from-local
- Merge pull request #220 from kurogin23mech-source/ms-95-ms-triage
- Merge pull request #219 from kurogin23mech-source/fix/ms-84-bus-poll-10s
- docs(ms-84): e-2339 register `cloud migrate-from-local` in help & drift allowlist
- docs(ms-84): e-2339 add `cloud migrate-from-local` to README CLI tables
- feat(ms-84): e-2339 add `beacon cloud migrate-from-local` to retire orphan project.json
- feat(ms-95): e-1668+e-2350 server-side claim gate for operation fire dedup
- fix(ms-84): e-2366 tone down poll interval bump 10s → 5s for combination fix
- fix(ms-84): e-2366 bump bridge poll interval default 2s → 10s
- Merge pull request #218 from kurogin23mech-source/ms-84-fork-321aff
- Merge pull request #217 from kurogin23mech-source/ms-86-fork-275318
- Merge pull request #216 from kurogin23mech-source/ms-84-fork-9f4ccd
- fix(ms-84): e-2325 disable on_snapshot listener to fix WS over-broadcast
- feat(ms-86): e-2227 add "last activity" column to MEMBERS table
- fix(ms-84): e-2338 collapse reload paint cascade to ≤2 renders
- feat(ms-86): e-2226 rename RECENT ACTIVITY → ACTIVITY, enrich rows + pagination
- Merge pull request #215 from kurogin23mech-source/e-2251/hamburger-trek
- Merge pull request #214 from kurogin23mech-source/e-2337/legacy-fallback
- Merge pull request #213 from kurogin23mech-source/e-1904/pytest-ci
- feat(ms-86): e-2251 promote Trek to top-level menu + all-user list view
- refactor(ms-84): e-2337 drop legacy 'type=project' fallback after signal-only rollover
- feat(ms-61): e-1904 add pytest workflow as PR merge gate
- Merge pull request #212 from kurogin23mech-source/ms-84/e-2326-signal-only-ws
- fix(ms-84): e-2326 third pass — signal-only WS, fetch state via REST
- Merge pull request #211 from kurogin23mech-source/ms-84/e-2326-followup-drop-tab-scoped-arrays
- fix(ms-84): e-2326 follow-up — drop tab-scoped arrays from slim WS broadcast
- Merge pull request #210 from kurogin23mech-source/ms-84/e-2326-ws-slim-broadcast
- fix(ms-84): e-2326 slim WS broadcast to escape 1MiB frame limit
- Merge pull request #209 from kurogin23mech-source/fix/ms-84-e2322-cloud-run-ws-timeout
- Merge pull request #208 from kurogin23mech-source/ms-60-fork-2025d0
- Merge pull request #207 from kurogin23mech-source/feat/ms-93-e2275-dm-payload-visibility
- fix(ms-84): e-2322 extend Cloud Run request timeout to 60 min for WS push live verify
- fix(ms-60): operation_approve 500 → expose private frontmatter helper through store_router (e-2306)
- feat(ms-93): e-2275 enforce DM payload visibility boundary on read endpoints
- Merge pull request #206 from kurogin23mech-source/fix/ms-43-e2304-ui-truncation
- Merge pull request #205 from kurogin23mech-source/fix/ms-84-e2303-ws-push-cloud-run-instances
- Merge pull request #203 from kurogin23mech-source/feat/ms-86-e2253-tauri-parity
- Merge pull request #202 from kurogin23mech-source/feat/ms-86-e2252-mockup-cross-project
- Merge pull request #201 from kurogin23mech-source/feat/ms-86-e2225-trek-session-history
- fix(ms-43): drop 8000px max-height clamp on expanded MS body (e-2304)
- fix(ms-84): e-2303 pin Cloud Run to single instance for WS push fanout
- Merge pull request #200 from kurogin23mech-source/fix/ms-43-e2281-creator-stamping
- Merge pull request #199 from kurogin23mech-source/ms-93-codex-openai-codex-cli-beacon-skill
- Merge pull request #204 from kurogin23mech-source/feat/ms-43-e2298-web-ui-session-30day
- feat(ms-43): e-2298 exchange Firebase id_token for 30-day bcli token in Web UI
- feat(ms-86): Trek session_history persistent join record (e-2225)
- feat(ms-86): Tauri Desktop App parity for Trek cross-project independence (e-2253)
- feat(ms-86): add mockup trek-detail-top-level.html as visual reference for cross-project Trek routing (e-2252)
- feat(ms-43): e-2281 stamp meta.author on MS/task/Operation creates
- fix(ms-93): e-2274 normalize bus send exit code on success path
- Merge pull request #198 from kurogin23mech-source/fix/ms-86-trek-detail-suppress-ws-rerender
- fix(ms-86): suppress WS-driven re-render while Trek detail is open (e-2270)
- Merge pull request #196 from kurogin23mech-source/feat/ms-86-e2250-trek-detail-state-openTrekId-axis
- feat(ms-86): Trek detail axis = state.openTrekId, URL = ?trek=<id>, header brand-only (e-2250)
- Merge pull request #197 from kurogin23mech-source/hotfix/ms-86-e2249-trek-members-scope-shadow-syntax-error
- fix(ms-86): rename _renderTrekMembersTable scope param to avoid shadowing local const (e-2249 hotfix)
- Merge pull request #195 from kurogin23mech-source/feat/ms-86-e2249-trek-lookup-helper-state-project-independent
- feat(ms-86): refactor Trek lookup helpers to be state.project-independent (e-2249)
- Merge pull request #194 from kurogin23mech-source/feat/ms-86-e2248-trek-scope-aggregate-endpoints
- feat(ms-86): add Trek scope aggregate endpoints for milestones/operations/tasks (e-2248)
- Merge pull request #193 from kurogin23mech-source/feat/ms-43-e2246-entry-creator-display
- feat(ms-43): gate entry author column on multi-actor detection (e-2246)
- feat(ms-43): stamp meta.author on milestones and operations (e-2246)
- Merge pull request #192 from kurogin23mech-source/fix/ms-86-agents-sid-column-and-header
- fix(ms-86): ヘッダ project 名漏れ箇所を全 fix (= 2026-06-23 dogfood)
- fix(ms-86): MEMBERS table に raw session_id 列を追加 (= 2026-06-23 dogfood)
- Merge pull request #191 from kurogin23mech-source/fix/ms-86-done-task-visual-grayout
- fix(ms-86): MEMBERS table の leader 行を leader_session_id にフォールバック + done タスク視覚 grayout
- Merge pull request #190 from kurogin23mech-source/fix/ms-86-v2-bugs-and-routing
- Merge pull request #189 from kurogin23mech-source/fix/ms-86-revert-pulse-ack-compliance
- fix(ms-86): show done ボタンを wire-through し done 状態 task を inline 表示する (e-2224)
- fix(ms-86): task leaf 行 / accordion child 行クリックで詳細 modal を開く (e-2223)
- fix(ms-86): TREK TASKS chevron 経由 accordion を user 操作で開閉できるようにする (e-2222)
- fix(ms-86): STOP card 幅を mockup 通り画面半分 (max-width 700px) に絞る (e-2221)
- fix(ms-43): Trek detail を開いた状態から project 戻り経路を復活させる (e-2220)
- fix(ms-86): Trek detail page ヘッダから project 名 / project tag を隠す (e-2219)
- revert(ms-86): PULSE-ACK COMPLIANCE section を Trek detail page から撤去
- Merge pull request #188 from kurogin23mech-source/feat/ms-86-v2-and-ms-88-phase4
- feat(ms-86): re-apply e-2133 s-N session badge on v2 layout (= cherry-pick recovery)
- feat(ms-86): Trek detail v2 — post-approve refinement (= e-2126 AC 完全充足)
- feat(ms-86): Trek detail page v2 layout reflow (= e-2126 mockup 通り実装)
- feat(ms-88): Phase 4 Trek UI pulse-ack compliance dashboard (= e-2108)
- docs(ms-86): Trek detail page wireframe mockup (= e-2126 visual reference)
- Merge pull request #187 from kurogin23mech-source/fix/skill-cli-drift-trek-execute
- Merge pull request #184 from kurogin23mech-source/ms-92-fork-aa2848
- feat(ms-92): Trek 終結 merge UX + CORE doc pr-review-autonomy-boundary (e-2169)
- Merge pull request #186 from kurogin23mech-source/ms-84/cloud-store
- Merge pull request #185 from kurogin23mech-source/ms-86-trek-ui-ms-83-dogfood-server-side-ui
- feat(ms-92): leader stance Skill 統合 + CORE doc 新設 (e-2166)
- feat(ms-92): leader-digest server push channel (e-2164)
- feat(ms-92): pulse-ack 構造化 payload schema 拡張 (e-2165)
- feat(ms-92): trek join consent gate 文面を 4 セクション化 (e-2182)
- feat(ms-92): cross-project task add via Trek scope (e-2141)
- feat(ms-84): Phase 5 retire project-stale doctor check + skill cleanup (e-2039)
- feat(ms-84): Phase 4 remove cloud push / pull / force-pull CLI (e-2038)
- feat(ms-92): /beacon-dm-send 確認 step を 1 prompt に集約 (e-2181)
- feat(ms-84): Phase 3 cut over local project.json in cloud mode (e-2037)
- fix(skills): beacon-trek-execute の skill-cli-drift を解消
- feat(ms-86): Trek 担当 session 列を local s-N 番号化 (= e-2133 AC6 of e-2126)
- docs(ms-84): Phase 3 write path audit (e-2037)
- Merge pull request #183 from kurogin23mech-source/fix/lint-docs-drift-trek-kickoff-reconcile
- fix(docs): bin/beacon usage + README CLI tables に trek kickoff / reconcile を追加 (= lint-docs drift fix)
- docs(release): update README/CHANGELOG for v0.48.0
- chore(release): bump formula to 0.48.0

## [v0.48.0] - 2026-06-22

- Merge pull request #182 from kurogin23mech-source/fix/ms-88-server-bugs-A
- fix(ms-88): server-side scheduler / task-state の 3 件の構造修正 (= 2026-06-19 dogfood で発見)
- Merge pull request #181 from kurogin23mech-source/feat/ms-88-trek-kickoff-cli
- Merge pull request #180 from kurogin23mech-source/feat/ms-88-pulse-picker-narrative
- fix(ms-88): peer 列挙 helper を kickoff_status keys 経由に修正 (= leader review)
- feat(ms-88): beacon trek kickoff CLI wrapper (= e-2139 残作業 #1)
- feat(ms-88): pulse picker narrative 4→5 択 + bus-autonomous-content 整流 (= e-2139 残作業)
- Merge pull request #178 from kurogin23mech-source/feat/ms-88-five-choice-picker
- Merge pull request #179 from kurogin23mech-source/feat/ms-88-kickoff-skill-side
- feat(ms-88): Trek Kickoff Ritual Skill side + coordinator norm narrative (= e-2138 / e-2140)
- feat(ms-88): 5-choice executor picker — 'dm-peer' を追加 (= e-2139)
- Merge pull request #177 from kurogin23mech-source/feat/ms-88-kickoff-server-side
- feat(ms-88): Trek Kickoff Ritual server-side (= e-2138 server part)
- docs(release): update README/CHANGELOG for v0.47.0
- chore(release): bump formula to 0.47.0

## [v0.47.0] - 2026-06-19

- Merge pull request #176 from kurogin23mech-source/deploy/ms-88-server-tick-v0.47
- chore: register beacon trek take-over in cmd_help_json + README (= cli-drift CI fix)
- fix(ms-61): credentials.json から identity を auto-read (= e-2132)
- fix(ms-43): explicit WS broadcast after every project write (= e-2128)
- feat(ms-88): Phase 3 state machine + TTL 12min + per-session fanout filter (= e-2107 / e-2109)
- feat(ms-88): Phase 1+2 Trek autonomy harness — consent gate / take-over / pulse-ack (= e-2090 / e-2089 / e-2105 / e-2106)
- docs(release): update README/CHANGELOG for v0.46.0
- chore(release): bump formula to 0.46.0

## [v0.46.0] - 2026-06-19

- Merge pull request #175 from kurogin23mech-source/ms-86-trek-ui-ms-83-dogfood-server-side-ui
- feat(ms-86): live session 表示を他 member + user-stop state まで拡張 (e-2045 step 3+4)
- feat(ms-86): self の live session を working/idle 別に内訳表示 (e-2045 step 2)
- Merge pull request #174 from kurogin23mech-source/ms-84/cloud-store
- feat(ms-86): self の live session 数を member row に小さく表示 (e-2045 step 1)
- refactor(ms-84): _load_session_logs を Store.list_session_logs 経由化 (e-2036)
- refactor(ms-84): cmd_cycle_status の document fetch を Store 経由化 (e-2036)
- refactor(ms-84): cmd_doc_image_upload と project_export source_mode を Store.is_cloud() 経由化 (e-2036)
- refactor(ms-84): cmd_trek_list を Store.list_treks 経由に統一 (e-2036)
- refactor(ms-84): cmd_trek_show を Store.get_trek 経由に統一 (e-2036)
- refactor(ms-84): cmd_doc_add / cmd_doc_update の read 経路を Store 経由化 (e-2036)
- refactor(ms-84): cmd_doc_list / cmd_doc_show を Store 経由に統一 (e-2036)
- refactor(ms-84): trek + document の read 経路を Store 経由に統一 (e-2036)
- docs(release): update README/CHANGELOG for v0.45.0
- chore(release): bump formula to 0.45.0
- feat(ms-86): aggregate state pill + review-pending banner (e-2058 #3+#4)
- feat(ms-86): Trek state の realtime 反映経路 — project frame piggyback (e-2022)
- refactor(ms-84): session log list / show を Store 経由に統一 (e-2036)
- feat(ms-86): NEXT FIRE 時刻 + RECENT ACTIVITY timeline 実装 (e-2019)
- feat(ms-86): halt UI を実 API call で wire-through (e-2018)
- feat(ms-86): 参加 session row 再構成 + task state badge 統合 (e-2017 + e-2058 #1+#2)
- refactor(ms-84): _aggregate_and_persist の session log fetch を Store 経由化 (e-2036)
- refactor(ms-84): _list_other_session_ids を Store 経由に統一 (e-2036)
- refactor(ms-84): session log push を Store 経由に統一 (e-2036)
- refactor(ms-84): cmd_operation_purge を Store 経由に統一 (e-2036)
- refactor(ms-84): cmd_entry_purge を Store 経由に統一 (e-2036)
- refactor(ms-84): cmd_milestone_purge を Store 経由に統一 (e-2036)
- feat(ms-86): Trek scope 行に drill-down (= 展開) 機構を追加 (e-2016)
- feat(ms-84): Phase 2 prep — Store.purge_milestone abstraction (e-2036)
- feat(ms-86): Trek detail page 骨格を 7 ブロック構造に書き換え (e-2015)
- feat(ms-84): Phase 1 — Store.get_milestone fine-grained read (e-2035)

## [v0.45.0] - 2026-06-19

- Merge pull request #164 from kurogin23mech-source/feat/local-cloud-docker-compose
- Merge pull request #173 from kurogin23mech-source/feat/ms-75-trek-autonomous-execution-completion
- feat(ms-75): Trek scope 内 DM の budget gate bypass + bus budget show に bypass 数を可視化 (= e-2044)
- feat(ms-75): beacon trek join に auto-arm を default 化 + --no-arm opt-out + session-start not-armed 警告 (= e-2047)
- feat(ms-75): server-side TTL safety net で working state の silent silence を auto-stall (= e-2067)
- feat(ms-75): bus.mjs CHANNEL_TO_SKILL hardcode で trek 系 channel を AI compliance 介さず自律起動 (= e-2069)
- docs(release): update README/CHANGELOG for v0.44.0
- chore(release): bump formula to 0.44.0
- feat(cli): ローカル開発ログインの口を CLI に追加 (ms-12 e-2041)
- feat(server): ローカルクラウドを DynamoDB Local で永続化 (ms-12 e-1987)
- docs(compose): note Firestore emulator non-persistence + DynamoDB plan (ms-12)
- feat(server): IdP不要のローカル開発ログイン (account分離対応) (ms-12)
- feat(server): add local docker-compose stack for cloud server (ms-12)

## [v0.44.0] - 2026-06-19

- Merge pull request #172 from kurogin23mech-source/feat/ms-75-trek-task-state-machine
- feat(ms-75): Trek task state machine + leader review 強制経路 (= e-2048)
- docs(release): update README/CHANGELOG for v0.43.0
- chore(release): bump formula to 0.43.0

## [v0.43.0] - 2026-06-19

- Merge pull request #171 from kurogin23mech-source/feat/ms-83-scheduler-session-fanout
- feat(ms-83): scheduler が Trek member の live session 全部に fanout する (= e-2036, leader 単独受信からの脱却)
- docs(release): update README/CHANGELOG for v0.42.0
- chore(release): bump formula to 0.42.0

## [v0.42.0] - 2026-06-19

- Merge pull request #170 from kurogin23mech-source/feat/ms-83-scheduler-autonomy-reminder
- feat(ms-83): scheduler payload に Trek 自律権限 reminder を埋め込む (= protocol drift 構造的防止)
- feat(ms-82): pre-commit に UI text 日本語 grep gate を追加 (e-1978)
- feat(ms-82): ハンバーガーメニュー Online Agents 行に renderName 適用 (e-1971)
- feat(ms-82): Trek detail / 一覧ページの UI text を英語化 (e-1976)
- feat(ms-82): server/static/index.html の非 Trek UI text を英語化 (e-1975)
- feat(ms-82): Settings panel から Profile タブを削除 (e-1973)
- feat(ms-82): avatar クリックで開く Profile modal を新設 (e-1972)
- feat(ms-82): Settings 各 section の長文 paragraph を tooltip 化 + 英語化 (e-1974)
- feat(ms-82): CSS hover tooltip card 機構を追加 (e-1970)
- docs(release): update README/CHANGELOG for v0.41.1
- chore(release): bump formula to 0.41.1

## [v0.41.1] - 2026-06-18

- Merge pull request #169 from kurogin23mech-source/fix/ms-83-current-project-id-e2007
- fix(ms-83): _current_project_id cloud-mode fallback (e-2007)
- docs(release): update README/CHANGELOG for v0.41.0
- chore(release): bump formula to 0.41.0

## [v0.41.0] - 2026-06-18

- Merge pull request #168 from kurogin23mech-source/ms-83-trek-server-side-continuity
- feat(ms-83): trek session idle detection + escalation DM (e-2001)
- feat(ms-83): AI autonomous task.add envelope decision (e-2000)
- feat(ms-83): /beacon-trek-execute recognises T1-system envelope (e-1999)
- feat(ms-83): Cloud Scheduler trek tick endpoint + payload builder (e-1997 e-1998)
- feat(ms-83): T1-system envelope mint + dm_gate bypass (e-1995)
- feat(ms-83): trek.meta cadence_minutes + manager_agent_url (e-1994)
- Merge pull request #167 from kurogin23mech-source/ms-75-phase3-trek-aggregation
- feat(ms-75): DM multi-recipient + Trek session-start + sensitivity gate
- feat(ms-75): trek aggregation view + goal_state + timeline + docs filter
- Merge pull request #166 from kurogin23mech-source/ms-76-ai-framework-envelope-tier-spec-ms-75
- Merge remote-tracking branch 'origin/main' into ms-76-ai-framework-envelope-tier-spec-ms-75
- feat(ms-76): operation-trigger unicast default + claim-based receiver (e-1860, e-1604)
- feat(ms-76): Operation execute に disclosure gate AND check を明文化 (e-1841)
- feat(ms-76): Operation setup Skill に tier 必須欄追加 (e-1840)
- feat(ms-76): bus budget grant に T1-only 構造的禁止帯を land (e-1852)
- Merge pull request #165 from kurogin23mech-source/ms-75-trek-ai-framework-operation-pattern
- feat(ms-76): DM Skill に envelope tier 判定セクション追加 (e-1850)
- feat(ms-75): codify Trek scope DM blanket exception (e-1856)
- feat(ms-75): /beacon-trek-execute Skill (e-1868)
- feat(ms-75): Trek-aware trigger system (e-1870)
- docs(release): update README/CHANGELOG for v0.40.0
- chore(release): bump formula to 0.40.0

## [v0.40.0] - 2026-06-18

- Merge pull request #163 from kurogin23mech-source/ms-81-ms-status-assignee-worktree
- feat(ms-81): doctor state-machine warnings + occupations CLI verb (e-1921)
- feat(ms-81): swap /beacon-dispatch workspace verb for start + skill drift detector (e-1920)
- feat(ms-81): transition + done-MS re-open prompts (e-1919)
- fix(ms-81): gate occupation claim with NO_BRANCH/NO_ASSIGNEE + add session-end auto-release (e-1918 follow-up)
- feat(ms-81): session occupation model + worktree_sessions audit log (e-1918)
- feat(ms-81): unify activation entry + project-type detect for milestone start (e-1917)
- feat(ms-81): CLI status write gate — warn before writing to non-active MS (e-1916)
- docs(ms-81): register beacon milestone wait in cmd_help_json (e-1915 follow-up)
- docs(ms-81): add `beacon milestone wait` row to README CLI table (e-1915 follow-up)
- feat(ms-81): waiting status formal CLI + transition rules (e-1915)
- Merge pull request #162 from kurogin23mech-source/ms-78/work
- Merge pull request #161 from kurogin23mech-source/ms-70/work
- feat(ms-78): display_name end-to-end propagation (e-1909)
- feat(ms-70): beacon dm log CLI for approval audit history (e-1923 / e-1718 AC 4)
- Merge pull request #159 from kurogin23mech-source/ms-72/work
- Merge origin/main into ms-72/work: resolve conflicts in desktop/layer.js + regen dist
- Merge pull request #160 from kurogin23mech-source/ms-78/work
- Merge pull request #158 from kurogin23mech-source/ms-70/work
- fix(ms-78): setup prompt に skill install + restart 動線を追加 (UC11-F6 ギャップ補修)
- feat(ms-70): DM approval history audit view (e-1718)
- feat(ms-70): denied notification reply chain to sender (e-1717)
- feat(ms-72): Tauri Rust member + invitation commands + UI parity (e-1774 / e-1779)
- feat(ms-70): beacon dm respond CLI primitive (e-1716)
- feat(ms-70): inline pending-DM banner in bus listen / receive (e-1715)
- feat(ms-78): beacon member invite CLI + display_name 優先表示 (e-1805/e-1807)
- feat(ms-70): cross-session pending DM action flush at session-start (e-1714)
- feat(ms-78): token-based invitation flow + /join landing (e-1803/e-1804)
- feat(ms-70): cross-user DM action authorization gate (e-1713)
- feat(ms-70): bus_event_approvals sidecar subcollection (e-1712)
- docs(release): update README/CHANGELOG for v0.39.0
- chore(release): bump formula to 0.39.0

## [v0.39.0] - 2026-06-17

- feat(ms-80): release-marker auto-clear + PR claim 競合検知 (e-1829 / e-1821)
- feat(ms-80): reviewer 任命動線 + deploy backend 記録 (e-1819 / e-1831)
- feat(ms-78): 招待 UI / CLI に GitHub repo collaborator は範疇外と明示 (e-1806)
- feat(ms-80): PR review DM template + 取り込み戦略の構造防御 (e-1820 / e-1823)
- Merge pull request #157 from kurogin23mech-source/ms-79-beacon-log-retro-retrospect-uc3-uc5
- Merge pull request #156 from kurogin23mech-source/ms-77-onboarding-beacon-init-archaeology-ux
- Merge pull request #155 from kurogin23mech-source/ms-61-cli-skill-forcing-function-drift
- merge e-1862/work into ms-61 branch: cloud push/pull cleanup + resolve conflicts with e-1861 (= cloud off section unified, default picker open/status, alias rename)
- merge e-1861/work into ms-61 branch: local mode removal
- merge e-1859/work into ms-61 branch: doc op CLI fix
- test(ms-79): retrospect fork/Trek/DM 取り込み検証 + source filter (e-1835 / e-1832 / e-1833)
- feat(ms-79): retro / retrospect Skill markdown を ms-79 拡張に対応 (e-1837 / e-1832 / e-1833 / e-1834 / e-1835)
- docs(ms-79): /beacon-log と /beacon-task の責務分界 CORE doc 参照誘導 (e-1818)
- feat(ms-79): /beacon-log Step 4 で actor / source を表示 (e-1815 / e-1817)
- fix(ms-79): /beacon-log honors fork.json target_ms_id (e-1816)
- feat(ms-79): tag auto-op commits with meta.source (e-1817)
- feat(ms-79): retro_prepare uses unified base + catch-up batch (e-1836 / e-1837)
- feat(ms-79): wire cmd_search to retro_query for ms-79 extensions  (e-1832 / e-1833 / e-1834 / e-1835)
- feat(ms-79): retro/retrospect unified query base (e-1836)
- feat(ms-77): extend entry-writing-principle to AI responses (e-1801)
- feat(ms-61): remove local mode, cloud.json is single source of truth (e-1861)
- fix(ms-61): doc update --op flag + list --op filter + frontmatter preservation (e-1859)
- chore(ms-61): align CLI help drift surfaces for cloud upload-initial / force-pull (e-1862 follow-up)
- feat(ms-61): cloud subaction picker default to open/status only, push/pull → special-purpose with explicit aliases (e-1862)
- Merge pull request #154 from kurogin23mech-source/ms-74/work
- Merge pull request #153 from kurogin23mech-source/ms-73/work
- Merge pull request #152 from kurogin23mech-source/ms-72/work
- feat(ms-74): /beacon-cloud Skill — push/pull/off/open 4 subaction (e-1770)
- feat(ms-73): dispatch.py Win parity for 11 session/bus/envelope verbs (e-1762/e-1763/e-1764)
- feat(ms-72): SHARED purity 復旧 — dataSource に member 系 5 メソッド + PLATFORM.accountMenuItemsHTML (e-1772/e-1773/e-1774/e-1775)
- feat(ms-74): /beacon-member Skill — invite/role/remove 二段確認 (e-1769)
- feat(ms-74): /beacon-trek Skill — 8 subaction 統合 (e-1768)
- feat(ms-74): /beacon-dm-respond Skill (e-1767)
- docs(release): update README/CHANGELOG for v0.38.1
- chore(release): bump formula to 0.38.1

## [v0.38.1] - 2026-06-14

- Merge ms-55-amended: follow-up 6 件 (receive-side halt / rollback log / morning doc / dispatch.py mirror / cloud claims / CLI table)
- fix(ms-55): stop signal の receive 側 halt 実装 (e-1721)
- fix(ms-55): cloud-mode active_claims subcollection 永続化 (e-1730)
- fix(ms-55): dispatch.py Windows mirror for 6 coordination verbs (e-1735)
- docs(ms-55): cmd_help_json + README CLI table — 14 coordination verbs (e-1736)
- fix(ms-55): morning briefing → report doc (e-1733)
- fix(ms-55): rollback 履歴記録 — save entry on successful rollback (e-1727)
- docs(release): update README/CHANGELOG for v0.38.0
- chore(release): bump formula to 0.38.0

## [v0.38.0] - 2026-06-14

- chore(ms-55): exempt new coordination CLIs from drift gate (Win parity follow-up)
- Merge ms-55/work: stop / rollback / claim primitives + STUCK detector + morning briefing CLI
- Merge ms-63/work: envelope disclosure_contract + beacon init --sensitivity + T5 schema enforcement
- Merge ms-68/work: entry-writing principle marker + draft display step in write Skills
- feat(ms-55): beacon morning — 4-category briefing of overnight autonomous activity (e-1650)
- feat(ms-55): STUCK signal detector — idle timeout escalation (e-1649)
- feat(ms-55): claim primitives (request / handoff / post) + local persistence (e-1648)
- feat(ms-68): embed draft display step in remaining write Skills (e-1643)
- feat(ms-55): rollback boundary CLI — auto-undo local, report on pushed (e-1647)
- feat(ms-63): T5 in_reply_to chain → T3 disclosure promotion (e-1432 方向 A)
- feat(ms-68): embed draft display step in mid-freq write Skills (e-1642)
- feat(ms-63): beacon init --sensitivity (default high) writes disclosure_policy (e-1428, e-1441)
- feat(ms-68): embed draft display step + autonomous-path exceptions in high-freq write Skills (e-1641)
- feat(ms-55): stop signal CLI + bus broadcast schema (e-1646)
- feat(ms-63): envelope disclosure_contract schema + receive-side gate (e-1428, e-1429, e-1430, e-1431, e-1432, e-1433, e-1443)
- feat(ms-68): embed entry-writing principle marker + doctor check (e-1639, e-1640)
- docs(release): update README/CHANGELOG for v0.37.4
- chore(release): bump formula to 0.37.4

## [v0.37.4] - 2026-06-14

- fix(ms-69): Settings の Archive ボタン無反応 + Export JSON が別プロジェクト
- docs(release): update README/CHANGELOG for v0.37.3
- chore(release): bump formula to 0.37.3

## [v0.37.3] - 2026-06-14

- fix(ms-69): settings dropdown peeks instead of navigating + menu paints once
- docs(release): update README/CHANGELOG for v0.37.2
- chore(release): bump formula to 0.37.2

## [v0.37.2] - 2026-06-14

- refactor(ms-69): My Agents & Treks の CLI ラベルを削除 + Milestones の Graph 機能を完全削除
- fix(ms-69): Settings Projects プルダウンを /api/me/projects 化 + 権限別表示 (e-1659 v3 follow-up)
- docs(ms-52): version-rules + /beacon-push Skill を MS 駆動 bump ルールに改訂
- docs(release): update README/CHANGELOG for v0.37.1
- chore(release): bump formula to 0.37.1

## [v0.37.1] - 2026-06-14

- fix(ms-69): Web UI 9 件の UX 修正 + Settings 機能補完 (e-1659 v3 follow-up)
- docs(release): update README/CHANGELOG for v0.37.0
- chore(release): bump formula to 0.37.0

## [v0.37.0] - 2026-06-14

- feat(ms-69): Web UI Navigation を mock 通りに再構成 (e-1659 v3)
- docs(release): update README/CHANGELOG for v0.36.0
- chore(release): bump formula to 0.36.0

## [v0.36.0] - 2026-06-14

- feat(ms-69): Web UI Trek 詳細をワイヤーフレームに準拠した full-page に refactor (e-1659 v2)
- docs(ms-69): README + bin/beacon usage + help_json に Trek を追加
- docs(release): update README/CHANGELOG for v0.35.0
- chore(release): bump formula to 0.35.0

## [v0.35.0] - 2026-06-14

- Merge pull request #151 from kurogin23mech-source/ms-69-trek-cross-project-cross-session
- feat(ms-69): work item 詳細に Related Treks widget を追加 (e-1664)
- feat(ms-69): Web UI に Treks タブ + trek 詳細ページを追加 (e-1659)
- feat(ms-69): doc.trek_id field + 関連 trek の逆引き / 順引き API (e-1663)
- test(ms-69): ms-55 が依存する trek API contract を pin (e-1657)
- feat(ms-69): beacon trek CLI を cloud-mode 対応 (e-1681)
- feat(ms-69): server に /api/treks/ CRUD + member ops + halt + transfer + summary endpoint (e-1656)
- test(ms-69): trek full-lifecycle smoke (SPEC AC #14) — e-1658
- feat(ms-69): beacon trek stop / resume / transfer-leader (e-1662)
- feat(ms-69): beacon trek plan — scope 編集 CLI (e-1655)
- feat(ms-69): beacon trek member CLI (invite / join / leave) — e-1654
- chore(ms-69): mirror beacon trek in beacon_cli/dispatch.py — Win pipx 対応
- feat(ms-69): beacon trek CLI 基本 CRUD + schema v2 (e-1653)
- feat(ms-69): UI mock + lifecycle 簡素化 (3-state, archived terminal)
- feat(ms-69): trek data schema — top-level collection + dual backend (e-1652)
- chore: fix CLI drift for beacon doc image-upload
- feat(ms-43): doc image upload — agent CLI/API for embedding images in SPECs (e-1660)
- docs(release): update README/CHANGELOG for v0.34.1
- chore(release): bump formula to 0.34.1

## [v0.34.1] - 2026-06-13

- fix(release): rename PyPI distribution to beacon-ai
- docs(release): update README/CHANGELOG for v0.34.0
- chore(release): bump formula to 0.34.0

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

