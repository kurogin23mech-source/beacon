# ms-84 Phase 3 Write Path Audit (2026-06-22)

## Direct project.json write paths (= bypass Store抽象)

The following sites write `.beacon/project.json` without going through `save_project()` / Store:

1. **lib/commands.py:778-780 (cmd_cloud_join)** — writes project.json with json.dump after fetching cloud project. Comment explicitly says "LocalStore.save_project requires the file to exist". This is the cloud-mode-bootstrap path that creates the dead local cache.

2. **lib/commands.py:5944-5946 (cmd_member_join)** — same pattern: writes project.json with json.dump for cloud-mode invite-acceptance.

3. **lib/commands.py:8613-8615 (cmd_cloud_push)** — uses LocalStore directly to load_project for upload-initial. This is the local-→-cloud migration path and is the natural site for `.before-cloud-YYYYMMDD` rename.

## Indirect path / json.dump usage

Other json.dump sites are not project.json writes (cloud.json, config.json, notes, triggers, claims, retros, session.json, etc.). They are unrelated.

## Reads of project.json that should be cut over in cloud mode

- `bin/beacon-bus-inbox-hook.py:304` — reads project.json for auto-execute allowlist (`bus.disabled` etc.).
- `bin/bclaude:78` — reads project.json for bus.disabled check.
- `bin/beacon-save-hook.sh:79,86` — uses jq to read active MS from project.json.
- `bin/context-usage-monitor.sh:26` — checks file existence.
- `bin/beacon-find-root` — uses project.json as the root marker (essential, must keep).

These are mostly external scripts and "find project root" markers. In cloud mode they currently fail or no-op gracefully; we keep these as-is because they're read-side and the eventual structure is a stub marker (see implementation plan).

## Remaining `_is_cloud_mode()` branches (23 sites)

Categorized:

- **Trek mutation (cmd_trek_create/start/archive/invite/join/halt/clear-halt/scope/leave)** — 10 sites. Currently `client.X` vs `trek_store.X`. Phase 3 *scope-creep* candidate; not directly tied to project.json existence. Defer to AC10 polish if time allows.

- **Document mutation (cmd_doc_add/update/delete)** — 3 sites (8010, 8144, 8293). Phase 3 in-scope candidate; not directly project.json but parallel.

- **Retro mutation (cmd_retro_save)** — 1 site (6610). Parallel to docs.

- **cmd_cloud_push** — 2 sites (8599, 8643). To be removed in Phase 4 entirely.

- **cmd_project_export** — 1 site (10896). Snapshot mode selector; keep, possibly migrate to `store.is_cloud()`.

- **Operation envelope guards** — 4 sites (12668, 12747, 12820, 12899). Intentional `not _is_cloud_mode()` gates because envelope signing requires server. Keep; can be `not store.is_cloud()`.

- **System slug** — 2 sites (15083, 15351). Provider registration + memo doc creation.

## Phase 3 Concrete Plan

1. **Add `_rename_local_project_json_for_cloud_cutover()` helper** — moves `.beacon/project.json` → `.beacon/project.json.before-cloud-YYYYMMDD`. Idempotent (no-op if source missing). Never deletes.

2. **Wire helper into cmd_cloud_push after successful PUT** — once cloud upload succeeds, rename local file. Records changelog entry for audit.

3. **cmd_cloud_join / cmd_member_join: stop writing project.json** — they only need cloud.json. Remove the json.dump-to-project.json blocks. If a stale project.json exists (= migration mid-state), rename it.

4. **Phase 3 deliberately keeps the doc/trek/retro write-side `_is_cloud_mode()` branches.** They are not project.json drift sites — they branch between cloud API and a separate local file (trek_store/, documents/, retro/). Migration to Store is Phase 3.5 / out-of-scope-creep.

5. **Tests**: 
   - new test: cloud_push renames project.json idempotently
   - new test: cmd_cloud_join does NOT create project.json
   - existing tests with cloud mode + mocks should still pass

## Acceptance Criteria Coverage

| AC | How Phase 3 satisfies |
|---|---|
| AC2 (cloud mode → no project.json) | rename in cloud_push + suppress create in cloud_join / member_join |
| AC7 (rename to .before-cloud-YYYYMMDD) | _rename_local_project_json_for_cloud_cutover helper |
| AC6 (local mode regression-free) | local mode never enters the rename path; LocalStore.save_project unchanged |
| AC10 (_is_cloud_mode reduction) | indirect: trek/doc/retro branches deferred; rename does not add new branches |
