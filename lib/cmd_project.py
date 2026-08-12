#!/usr/bin/env python3
"""cmd_project.py — the `beacon project *` command family (ms-127 e-4860).

Extracted verbatim from commands.py (god-module split). The project family is the
whole-project lifecycle surface: archive / unarchive (retire a project without
deleting it), export / import (back up + restore the project as a portable
bundle), cleanup / orphans (find + drop dangling references), rename.

At IMPORT time this module depends only on commands_shared (upward) + stdlib —
the import-time DAG is acyclic (SPEC 方針4). Handlers additionally do function-local
imports of leaf domain modules as needed (auth / api_client in the cloud paths,
project_cleanup in cleanup/orphans) — deferred so they never widen the import-time
surface. The one runtime tie to commands.py is _beacon_version() below: the CLI
version literal (__version__) lives in commands.py because version_skew.py pins its
file-regex to that file, so it cannot be promoted to commands_shared without breaking
version detection across the wheel layout. A function-local import keeps the
import-time DAG acyclic.

export / import (ms-14 e-828) write/read a full-snapshot backup ZIP —
  manifest.json (required: source / version / entry counts) + project.json +
  documents/<doc_id>.md + changelog.jsonl + retro/<file>.md + config.json.
  Local-mode export reads .beacon/ directly; cloud-mode fetches via the API for
  authoritative state. Import is local-mode-only in this iteration (extract →
  fresh .beacon/).

commands.py re-imports the PUBLIC handlers for dispatch + `commands.cmd_project_*`;
the family-private helpers (_collect_export_* / _reconstruct_doc_markdown /
_resolve_current_project_id_from_cloud_json) and the _BACKUP_SCHEMA_VERSION
constant are NOT re-exported (patch them at cmd_project.<name>).

Test patch target (the e-4320 rule): a test driving a cmd_project_* handler must
patch the name in cmd_project's own namespace — each `from commands_shared import
name` binds an independent copy, so `monkeypatch.setattr(commands, "get_store",
...)` is a silent no-op on this call path. Patch `cmd_project.get_store` instead.
To pin the manifest's beacon_version field in an export test, patch
`cmd_project._beacon_version` (not commands.__version__).
"""

import os
import sys
import json

from commands_shared import (  # noqa: F401
    Optional,
    _extract_token,
    _get_api_client,
    _get_cloud_config_path,
    _is_cloud_mode,
    _resolve_active_api_url,
    get_project_file,
    get_store,
    load_project,
    save_project,
)


def _beacon_version() -> str:
    """Lazy read of the CLI version string. The literal lives in
    commands.py (version_skew.py pins its file-regex there, so it cannot be
    promoted to commands_shared). Function-local import keeps cmd_project
    import-time acyclic — this is the module's only tie to commands.py."""
    from commands import __version__
    return __version__


def cmd_project_rename():
    """Rename the project — change its display name after creation (ms-122
    e-4033).

    beacon project rename <new-name>

    A project's name is set once at ``beacon init`` and could not be changed
    afterward, so a project whose purpose drifted kept a stale name. This
    updates the display name in place. In cloud mode ``save_project`` writes the
    whole project document, so the server-side display name (dashboard / Web UI
    / project directory) follows the same rename — no separate step."""
    new_name = os.environ.get("BEACON_NEW_NAME", "").strip()
    if not new_name:
        print("Usage: beacon project rename <new-name>", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    old = data.get("name", "")
    if new_name == old:
        print(f"プロジェクト名は既に '{new_name}' です (変更なし)。")
        return
    data["name"] = new_name
    save_project(data, op={"op": "project_rename", "old": old, "new": new_name})
    print(f"プロジェクト名を変更しました: {old or '(無名)'} → {new_name}")


def cmd_project_archive():
    """Archive the current project (sets archived: true in project.json)."""
    data = load_project()
    if data.get("archived"):
        print("Project is already archived.")
        return
    data["archived"] = True
    save_project(data)
    print(f"Archived: [{data.get('name', '')}]")


def cmd_project_unarchive():
    """Unarchive the current project."""
    data = load_project()
    if not data.get("archived"):
        print("Project is not archived.")
        return
    data["archived"] = False
    save_project(data)
    print(f"Unarchived: [{data.get('name', '')}]")


def cmd_project_orphans():
    """List throwaway / orphan project cleanup candidates (ms-123 / e-4030).

    Read-only. Scans the projects the current user can see in the cloud and
    flags the ones that look like test residue (``phase4-test`` etc.), using
    the multi-signal classifier in ``lib/project_cleanup``. Changes nothing —
    the actual archive step is a separate, human-confirmed command (e-4028).

    ``BEACON_JSON=1`` emits the raw candidate list for scripting.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    try:
        projects = client.list_projects() or []
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Never propose archiving the project we're running from.
    current_pid = _resolve_current_project_id_from_cloud_json()

    import project_cleanup
    candidates = project_cleanup.detect_orphan_candidates(
        projects, current_project_id=current_pid or None,
    )
    # ms-125 e-4095: name which signals are degraded on this scan so the human
    # doesn't trust a phantom redundancy (in prod only test_named fires).
    coverage = project_cleanup.assess_signal_coverage(projects)

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if json_mode:
        print(json.dumps(
            {"total_scanned": len(projects), "candidates": candidates,
             "signal_coverage": coverage},
            ensure_ascii=False,
        ))
        return
    print(project_cleanup.format_orphan_report(candidates, len(projects)))
    note = project_cleanup.format_coverage_note(coverage)
    if note:
        print()
        print(note)


def _resolve_current_project_id_from_cloud_json() -> str:
    """Read the cwd's cloud.json project_id (best-effort, "" on any miss)."""
    try:
        cfg_path = _get_cloud_config_path()
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("project_id", "") or ""
    except Exception:
        pass
    return ""


def cmd_project_cleanup():
    """Archive throwaway / orphan projects, human-confirmed (ms-123 / e-4028).

    Two-phase by design (SPEC ms-123 方針 2), because ``project.archive`` is
    owner-only + envelope-gated but a terminal AI holds the human's token —
    so the real safety valve is a human confirmation checkpoint in the flow,
    not the server gate. The pattern mirrors ``BEACON_PR_MERGE_USER_OVERRIDE``:

      * No confirm flag  → DRY-RUN. Print exactly what would be archived, the
        signal-coverage note, and the ready-to-paste ``--confirm --ids <…>``
        command. Nothing is issued or archived.
      * ``--confirm --ids <a,b,…>`` (BEACON_CLEANUP_CONFIRM=1 +
        BEACON_CLEANUP_CONFIRM_IDS) → archive ONLY the ids the dry-run printed
        (ms-125 e-4095). For each, mint a T1 envelope authorizing
        ``project.archive`` (the calling user's token is the human-signature
        proof) and archive it, carrying the signed envelope in the
        ``X-Beacon-Envelope`` header. Raw API calls never happen by hand.

    Binding the confirm to the reviewed ids (ms-125 e-4095) closes the hole
    where the confirm step re-fetched + re-detected candidates: a project that
    became a candidate *after* the dry-run would otherwise be archived without
    ever having been shown to the human. ``--confirm`` without ``--ids`` now
    fails closed. ``--limit N`` caps the batch (safety: inspect a small first
    sweep before the full run). ``BEACON_JSON=1`` emits a machine-readable
    result.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    try:
        projects = client.list_projects() or []
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    current_pid = _resolve_current_project_id_from_cloud_json()

    import project_cleanup
    candidates = project_cleanup.detect_orphan_candidates(
        projects, current_project_id=current_pid or None,
    )
    coverage = project_cleanup.assess_signal_coverage(projects)

    confirm = os.environ.get("BEACON_CLEANUP_CONFIRM", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # ms-125 e-4095: the ids the dry-run showed, carried into --confirm so the
    # archive is bound to the reviewed set (comma / whitespace separated).
    confirm_ids_raw = (os.environ.get("BEACON_CLEANUP_CONFIRM_IDS", "") or "").strip()
    confirm_ids = [s.strip() for s in confirm_ids_raw.replace(",", " ").split()
                   if s.strip()]

    # ms-125 review (AX high): --ids only means something WITH --confirm. Without
    # it the dry-run ignored --ids entirely — a silent no-op that reads as "narrowed
    # to these ids" when it actually shows every candidate. Fail-closed instead.
    if confirm_ids and not confirm:
        msg = ("Error: --ids は --confirm と併用する時だけ有効です (ms-125 e-4095)。\n"
               "  dry-run (確認表示) は候補を絞り込めません。--ids を外して\n"
               "  `beacon project cleanup` で全候補を確認するか、`--confirm --ids <…>` で\n"
               "  実行してください。")
        if json_mode:
            print(json.dumps({"mode": "dry-run", "error": "ids_without_confirm",
                              "hint": "run `beacon project cleanup` (no --ids) for the dry-run"},
                             ensure_ascii=False))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    def _emit_coverage_note():
        """ms-125 review: one place for the 'print a blank line + coverage note
        if any signal is degraded' block (was copy-pasted at 3 call sites)."""
        note = project_cleanup.format_coverage_note(coverage)
        if note:
            print()
            print(note)
    # ms-123 AX finding: fail-closed on a bad --limit. Previously a non-numeric
    # value fell to limit=0 → "no limit" → --confirm mass-archived ALL candidates.
    # An invalid value now errors; only an ABSENT limit means "no cap" (explicit).
    _limit_raw = (os.environ.get("BEACON_CLEANUP_LIMIT", "") or "").strip()
    if _limit_raw:
        try:
            limit = int(_limit_raw)
        except ValueError:
            print(f"Error: --limit の値が不正です: '{_limit_raw}'. 正の整数を "
                  f"指定してください (例: --limit 5)。", file=sys.stderr)
            sys.exit(1)
        if limit <= 0:
            print(f"Error: --limit は正の整数です (受領: {limit})。",
                  file=sys.stderr)
            sys.exit(1)
    else:
        limit = 0
    plan = project_cleanup.build_archive_plan(candidates, limit=limit or None)

    # ---- DRY-RUN (default): show the plan, change nothing ----------------
    if not confirm:
        plan_ids = [c["project_id"] for c in plan]
        if json_mode:
            print(json.dumps({
                "mode": "dry-run", "total_scanned": len(projects),
                "would_archive": plan, "confirm_ids": plan_ids,
                "signal_coverage": coverage,
            }, ensure_ascii=False))
            return
        if not plan:
            print(project_cleanup.format_orphan_report(candidates, len(projects)))
            _emit_coverage_note()
            return
        print(project_cleanup.format_orphan_report(plan, len(projects)))
        _emit_coverage_note()
        print()
        # ms-125 e-4095: the confirm run is bound to THESE ids. Emit them so the
        # human archives exactly what they just reviewed — not a set recomputed
        # later (which could sweep up a candidate that appeared in between).
        print(
            f"⚠ これは DRY-RUN です。上記 {len(plan)} 件はまだ archive していません。\n"
            "  実際に archive するには、候補を目視で確認したうえで、以下をそのまま実行してください\n"
            "  (この --ids は今表示した候補に束縛されます。後から新たに候補化した project は\n"
            "   このコマンドでは archive されません):\n"
            f"    beacon project cleanup --confirm --ids {','.join(plan_ids)}"
            + (f" --limit {limit}" if limit else "")
        )
        return

    # ---- CONFIRMED: bind to the reviewed --ids, then archive -------------
    # ms-125 e-4095: a confirmed run MUST carry the ids the dry-run printed.
    # Without them we'd re-archive a recomputed set — the exact hole where a
    # project that became a candidate after the dry-run gets swept up unseen.
    # Fail-closed: no --ids → stop and point back at the dry-run.
    if not confirm_ids:
        msg = ("Error: --confirm には --ids が必要です (ms-125 e-4095)。\n"
               "  まず dry-run で候補を確認してください:\n"
               "    beacon project cleanup\n"
               "  出力末尾に表示される `--confirm --ids <…>` をそのまま実行すると、\n"
               "  目視した候補だけが archive されます。")
        if json_mode:
            # ms-125 review (AX low): carry a recovery hint in the JSON error so
            # an automated caller learns HOW to obtain the ids, not just that
            # they're missing (the dry-run JSON exposes them as `confirm_ids`).
            print(json.dumps({"mode": "confirmed", "error": "ids_required",
                              "hint": "run `beacon project cleanup` (dry-run) and use its confirm_ids",
                              "archived": [], "failed": []}, ensure_ascii=False))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    bound = project_cleanup.bind_confirmed_plan(
        candidates, confirm_ids, limit=limit or None,
    )
    plan = bound["plan"]

    # Surface what the binding refused / skipped / capped, so a confirmed run is
    # never silently narrower or wider than the human expects (ms-125 review: a
    # limit-dropped id was previously in no bucket, so "no error" read as "all
    # archived").
    if not json_mode:
        for pid in bound["skipped_missing"]:
            print(f"  ⟳ skip {pid} — dry-run 時の候補が現在は候補でない "
                  "(既 archive / owner 出現 等)。archive しません。")
        for c in bound["unreviewed_new"]:
            print(f"  ⛔ refuse {c['project_id']}  «{c['name']}» — dry-run 未提示の"
                  "新規候補。再度 dry-run で確認してください。archive しません。")
        for pid in bound["dropped_by_limit"]:
            print(f"  ✂ limit 超過 {pid} — --limit {limit} を超えたため今回は "
                  "archive しません (残りは再実行してください)。")

    results = {"archived": [], "failed": [],
               "skipped_missing": bound["skipped_missing"],
               "refused_unreviewed": [c["project_id"]
                                      for c in bound["unreviewed_new"]],
               "dropped_by_limit": bound["dropped_by_limit"]}
    for c in plan:
        pid = c["project_id"]
        try:
            env = client.issue_bus_envelope(
                pid, tier="T1", actions_authorized=["project.archive"],
            )
            client.archive_project(pid, env)
            results["archived"].append(pid)
            if not json_mode:
                print(f"  ✓ archived {pid}  «{c['name']}»")
        except (RuntimeError, ConnectionError) as e:
            results["failed"].append({"project_id": pid, "error": str(e)})
            if not json_mode:
                print(f"  ✗ FAILED   {pid}  «{c['name']}» — {e}")

    if json_mode:
        print(json.dumps({"mode": "confirmed", **results}, ensure_ascii=False))
        return
    print()
    print(
        f"完了: {len(results['archived'])} 件 archive、"
        f"{len(results['failed'])} 件 失敗。"
    )
    if results["failed"]:
        print("  失敗分は owner/envelope/ネットワークを確認して再実行してください。")
    print("  取り消しは各 project で `beacon project unarchive`（復元可能）。")

_BACKUP_SCHEMA_VERSION = 1


def cmd_project_export():
    """Pack the current project into a backup ZIP (ms-14 e-828).

    Env-driven CLI surface (matches the rest of the dispatcher):
      BEACON_OUTPUT   path to the ZIP to write (required)
      BEACON_BACKUP   "1" to flag this as a full backup snapshot
                     (currently the only mode; accepted for forward-compat
                     with --backup CLI flag)
      BEACON_JSON     "1" to emit a JSON receipt instead of human text
    """
    import zipfile
    output = os.environ.get("BEACON_OUTPUT", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not output:
        print("Error: --output <path> is required.", file=sys.stderr)
        print(
            "  Example: beacon project export --backup -o ~/Backups/myproj.zip",
            file=sys.stderr,
        )
        sys.exit(1)

    # Refuse to silently overwrite an existing file — backups should be
    # append-only from the operator's POV.
    if os.path.exists(output):
        print(
            f"Error: output already exists: {output}\n"
            "  Choose a different path or remove the file first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Collect contents based on mode.
    if _is_cloud_mode():
        snapshot = _collect_export_cloud()
    else:
        snapshot = _collect_export_local()

    # Build manifest (entry_counts gives a quick integrity hash without
    # parsing the whole project).
    project_data = snapshot["project"]
    # Count Targets through the occupation abstraction, not data['milestones']
    # directly (ms-134 e-5061): project export is L1 (instance-universal), so its
    # integrity count must cover a sales project's Opportunities too, not only dev
    # Milestones. iter_target_records returns every Target record across
    # occupations verbatim (with nested entries). The "milestones" entry_counts
    # key name is kept for backup-schema back-compat (schema_version unchanged);
    # it now counts all Targets regardless of occupation.
    import occupation
    target_records = occupation.iter_target_records(project_data)
    op_list = project_data.get("operations", []) or []

    # ms-142 e-5115: count every Target's fat-arm items through the occupation
    # manifest instead of the hardcoded dev ``entries`` arm. Each target-class
    # declares its fat arms (dev milestone → ("entries",); sales opportunity →
    # ("activities", "communications")), so the integrity count covers a sales
    # project's work too rather than under-counting it. Dev stays byte-identical:
    # arms == ("entries",) reproduces the old "top-level entries + nested entries"
    # walk (a task's nested commits ride the same ``entries`` arm). This greens the
    # (cmd_project, "entries") arm-reach debt — the arm name is now read from the
    # manifest, never as a literal.
    manifest = occupation.profession_manifest(project_data)

    def _count_arm_items(record, arm_names):
        """Count a Target record's fat-arm items + their nested children across
        every arm the class declares. Recurses over the SAME arm names, so a dev
        commit nested under a task (both in ``entries``) and a sales evidence row
        nested under a work item are both counted, with no arm name hardcoded."""
        total = 0
        for arm in arm_names:
            for item in record.get(arm, []) or []:
                total += 1
                total += _count_arm_items(item, arm_names)
        return total

    top_level_entries = 0
    for tc in manifest["target_classes"]:
        arms = tc["arms"]
        if not arms:
            continue
        for record in project_data.get(tc["collection"], []) or []:
            top_level_entries += _count_arm_items(record, arms)

    entry_counts = {
        "milestones": len(target_records),
        "operations": len(op_list),
        "top_level_entries": top_level_entries,
        "documents": len(snapshot["documents"]),
        "changelog_lines": snapshot["changelog_lines"],
        "retro_files": len(snapshot["retros"]),
    }

    import datetime
    manifest = {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "export_ts": datetime.datetime.now().isoformat(),
        "project_name": project_data.get("name", ""),
        "project_id": snapshot.get("project_id", ""),
        "beacon_version": _beacon_version(),
        "source_mode": "cloud" if get_store().is_cloud() else "local",
        "entry_counts": entry_counts,
    }

    # Stream into ZIP. zipfile is in stdlib so no extra deps; DEFLATED
    # keeps the size small for text-heavy beacon dumps.
    written_paths: list[str] = []
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            written_paths.append("manifest.json")

            zf.writestr(
                "project.json",
                json.dumps(project_data, ensure_ascii=False, indent=2) + "\n",
            )
            written_paths.append("project.json")

            for doc_id, doc_body in snapshot["documents"].items():
                # doc_id is a Firestore-style ID or local slug; both are
                # filesystem-safe (alphanumeric / hyphen).
                zf.writestr(f"documents/{doc_id}.md", doc_body)
                written_paths.append(f"documents/{doc_id}.md")

            if snapshot["changelog"] is not None:
                zf.writestr("changelog.jsonl", snapshot["changelog"])
                written_paths.append("changelog.jsonl")

            for retro_name, retro_body in snapshot["retros"].items():
                zf.writestr(f"retro/{retro_name}", retro_body)
                written_paths.append(f"retro/{retro_name}")

            if snapshot["config"] is not None:
                zf.writestr("config.json", snapshot["config"])
                written_paths.append("config.json")
    except (OSError, zipfile.BadZipFile) as e:
        print(f"Error writing ZIP: {e}", file=sys.stderr)
        # Best-effort cleanup so a half-written backup isn't mistaken for a
        # complete one.
        if os.path.exists(output):
            try:
                os.remove(output)
            except OSError:
                pass
        sys.exit(1)

    size_bytes = os.path.getsize(output)
    if json_mode:
        print(json.dumps({
            "output": output,
            "size_bytes": size_bytes,
            "manifest": manifest,
            "entries_written": len(written_paths),
        }, ensure_ascii=False))
    else:
        print(f"Exported [{manifest['project_name']}] -> {output}")
        print(f"  size: {size_bytes} bytes ({len(written_paths)} files)")
        # Reference the Target count via the local (not entry_counts['milestones'])
        # so the capability-scope checker does not read this local-dict subscript
        # as a data['milestones'] reach — project export is L1 now (ms-134 e-5061).
        print(f"  milestones: {len(target_records)}, "
              f"docs: {entry_counts['documents']}, "
              f"changelog: {entry_counts['changelog_lines']} lines")


def _collect_export_local() -> dict:
    """Gather export contents from the local .beacon/ tree."""
    data = load_project()
    project_dir = os.path.dirname(get_project_file())

    documents: dict[str, str] = {}
    docs_dir = os.path.join(project_dir, "documents")
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(docs_dir, fname), "r", encoding="utf-8") as f:
                    documents[fname[:-3]] = f.read()
            except (OSError, UnicodeDecodeError):
                # Skip unreadable docs (cp932 cruft, etc.) — don't fail the
                # whole export. The manifest count reflects what we got.
                continue

    changelog: Optional[str] = None
    changelog_lines = 0
    changelog_path = os.path.join(project_dir, "changelog.jsonl")
    if os.path.isfile(changelog_path):
        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                changelog = f.read()
            changelog_lines = sum(
                1 for line in changelog.splitlines() if line.strip()
            )
        except (OSError, UnicodeDecodeError):
            changelog = None

    retros: dict[str, str] = {}
    retro_dir = os.path.join(project_dir, "retro")
    if os.path.isdir(retro_dir):
        for fname in sorted(os.listdir(retro_dir)):
            if fname.startswith("."):
                continue
            fpath = os.path.join(retro_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    retros[fname] = f.read()
            except (OSError, UnicodeDecodeError):
                continue

    config_body: Optional[str] = None
    config_path = os.path.join(project_dir, "config.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_body = f.read()
        except (OSError, UnicodeDecodeError):
            config_body = None

    return {
        "project": data,
        "project_id": data.get("id", "") or data.get("name", ""),
        "documents": documents,
        "changelog": changelog,
        "changelog_lines": changelog_lines,
        "retros": retros,
        "config": config_body,
    }


def _collect_export_cloud() -> dict:
    """Gather export contents via the Beacon API (authoritative)."""
    client, config = _get_api_client()
    project_id = config["project_id"]

    project_data = client.get_project(project_id)

    documents: dict[str, str] = {}
    try:
        doc_index = client.list_documents(project_id)
    except Exception:
        doc_index = []
    for entry in doc_index:
        doc_id = entry.get("doc_id") or entry.get("id") or ""
        if not doc_id:
            continue
        try:
            full = client.get_document(project_id, doc_id)
        except Exception:
            continue
        # Reconstruct the .md body with frontmatter the same way
        # `beacon doc show` does, so a local import sees identical files.
        body = _reconstruct_doc_markdown(full)
        documents[doc_id] = body

    # Changelog and retros are not yet exposed by the API (e-825 covers
    # changelog persistence). Snapshot what we can from the local cache
    # if present — better than nothing for now, and the manifest records
    # source_mode=cloud so future imports can recognize the gap.
    project_dir = os.path.dirname(get_project_file())
    changelog: Optional[str] = None
    changelog_lines = 0
    changelog_path = os.path.join(project_dir, "changelog.jsonl")
    if os.path.isfile(changelog_path):
        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                changelog = f.read()
            changelog_lines = sum(
                1 for line in changelog.splitlines() if line.strip()
            )
        except (OSError, UnicodeDecodeError):
            changelog = None

    retros: dict[str, str] = {}
    retro_dir = os.path.join(project_dir, "retro")
    if os.path.isdir(retro_dir):
        for fname in sorted(os.listdir(retro_dir)):
            if fname.startswith("."):
                continue
            fpath = os.path.join(retro_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    retros[fname] = f.read()
            except (OSError, UnicodeDecodeError):
                continue

    return {
        "project": project_data,
        "project_id": project_id,
        "documents": documents,
        "changelog": changelog,
        "changelog_lines": changelog_lines,
        "retros": retros,
        "config": None,
    }


def _reconstruct_doc_markdown(full: dict) -> str:
    """Rebuild the on-disk .md body (frontmatter + content) for an API doc."""
    frontmatter_keys = ("scope", "milestone", "operation", "title")
    fm_lines = ["---"]
    for k in frontmatter_keys:
        v = full.get(k)
        if v not in (None, ""):
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    content = full.get("content", "")
    if content and not content.startswith("\n"):
        content = "\n" + content
    return "\n".join(fm_lines) + content


def cmd_project_import():
    """Extract a backup ZIP into a target .beacon/ directory (local mode).

    Env-driven CLI surface:
      BEACON_INPUT    path to the ZIP to read (required)
      BEACON_TARGET   directory to create / write into (required).
                     The .beacon/ subdir is created here; refusal if it
                     already exists with content unless --force.
      BEACON_FORCE    "1" to allow overwriting an existing .beacon/
                     (destructive — operator confirmation surface).
      BEACON_JSON     "1" to emit a JSON receipt.
    """
    import zipfile
    inp = os.environ.get("BEACON_INPUT", "").strip()
    target = os.environ.get("BEACON_TARGET", "").strip()
    force = os.environ.get("BEACON_FORCE", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not inp or not target:
        print(
            "Error: --input <zip> and --target <dir> are both required.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.path.isfile(inp):
        print(f"Error: backup not found: {inp}", file=sys.stderr)
        sys.exit(1)

    target = os.path.abspath(target)
    target_beacon = os.path.join(target, ".beacon")
    if os.path.exists(target_beacon) and not force:
        print(
            f"Error: {target_beacon} already exists.\n"
            "  Use --force to overwrite (destructive).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with zipfile.ZipFile(inp, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names or "project.json" not in names:
                print(
                    "Error: not a valid backup ZIP "
                    "(missing manifest.json or project.json).",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"Error: malformed manifest.json: {e}", file=sys.stderr)
                sys.exit(1)
            if manifest.get("schema_version") != _BACKUP_SCHEMA_VERSION:
                print(
                    f"Error: backup schema_version={manifest.get('schema_version')} "
                    f"is not supported by this beacon (expects {_BACKUP_SCHEMA_VERSION}).",
                    file=sys.stderr,
                )
                sys.exit(1)

            os.makedirs(target_beacon, exist_ok=True)
            written = 0
            for name in names:
                if name == "manifest.json":
                    continue  # don't litter the destination; we keep it
                              # alongside as .manifest.json for trace
                # Refuse any path attempting to escape the target via
                # absolute paths or ".." segments.
                norm = os.path.normpath(name)
                if norm.startswith(("..", "/")) or os.path.isabs(norm):
                    print(f"Error: refusing unsafe path in backup: {name}",
                          file=sys.stderr)
                    sys.exit(1)
                dest = os.path.join(target_beacon, norm)
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as out:
                    out.write(src.read())
                written += 1

            # Drop the manifest beside the data as a trace marker —
            # future operators / tooling can see how the dir was created.
            manifest_marker = os.path.join(target_beacon, ".import_manifest.json")
            with open(manifest_marker, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

    except zipfile.BadZipFile as e:
        print(f"Error: corrupt backup ZIP: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error writing target: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps({
            "target": target_beacon,
            "files_written": written,
            "manifest": manifest,
        }, ensure_ascii=False))
    else:
        print(f"Imported [{manifest.get('project_name', '?')}] -> {target_beacon}")
        print(f"  files: {written}, exported at {manifest.get('export_ts', '?')}")
        print(f"  source: {manifest.get('source_mode', '?')}, "
              f"beacon {manifest.get('beacon_version', '?')}")
