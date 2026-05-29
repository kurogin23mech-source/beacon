# Changelog

All notable changes to Beacon are documented here. See [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for format.

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

