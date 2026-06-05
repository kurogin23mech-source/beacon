# Changelog

All notable changes to Beacon are documented here. See [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for format.

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

