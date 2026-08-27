#!/usr/bin/env python3
"""cmd_deploy.py — the `beacon deploy *` command family (ms-127 e-4815).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (core / store), never on
commands.py — acyclic (SPEC 方針4). commands.py re-imports the PUBLIC handlers
for dispatch + `commands.X`; family-private helpers (_next_deploy_id /
_next_release_id / _is_default_prod_backend / _update_deployed_prod_marker /
_fire_map_reconcile_trigger / _fire_graph_reseed_trigger) are NOT re-exported
(patch them at cmd_deploy.<name>).

The application-map applicability helper (_application_map_applies) was promoted
to commands_shared in this same change (e-4815-foundation) so
_auto_fire_map_drift_trigger can keep using it without importing cmd_deploy (which
would form a cycle). (ms-155 e-5599: it now derives from the deliverable
declaration rather than a profession helper.)

Test patch target (monkeypatch trap): a test driving a cmd_deploy_* handler
patches helpers the handler resolves in cmd_deploy's own namespace (cmd_deploy._X),
including re-exported ones — `commands._X` is an independent binding and a silent
no-op on the cmd_deploy call path (the e-4320 rule).
"""

import json
import os
import subprocess
import sys

import core
from store import get_store
from commands_shared import (
    load_project,
    save_project,
    _get_triggers_dir,
    _project_id_for_ops,
    _application_map_applies,
)


def _fire_map_reconcile_trigger() -> None:
    """Fire a 'map-reconcile' trigger at deploy time (ms-104 e-3154).

    A deploy is the natural moment surfaces (= 機能の入口) go to the world, so
    per SPEC §4 the application-map should be reconciled (足す＆消す) then.
    Fires only when a map already exists — a missing map is the session-start
    proposal's job. The reconcile itself is /beacon-map's work; this trigger
    only prompts it. General mechanism (fires for any project's deploy).
    Degrades silently with no store / no map / IO error.

    Development-only: a non-dev project (e.g. sales) owns no application-map, so
    the deploy-time reconcile prompt does not fire (ms-109 e-3404).
    """
    if not _application_map_applies():
        return
    try:
        doc = get_store().get_document("application-map")
    except Exception:
        return
    if not doc:
        return
    try:
        triggers_dir = _get_triggers_dir()
        os.makedirs(triggers_dir, exist_ok=True)
        import datetime
        now_iso = datetime.datetime.now().isoformat()
        trigger_data = {
            "name": "map-reconcile",
            "kind": "map-reconcile",
            "message": (
                "デプロイを記録しました。全貌マップ (application-map) を "
                "`/beacon-map` で reconcile (= 足す＆消す) して、"
                "今回の surface 変化を地図に反映してください。"
            ),
            "created_at": now_iso,
            "refreshed_at": now_iso,
        }
        with open(os.path.join(triggers_dir, "map-reconcile.json"), "w", encoding="utf-8") as f:
            json.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")
    except OSError:
        return


def _fire_graph_reseed_trigger() -> None:
    """Fire a 'graph-reseed' trigger at deploy time (ms-156 e-5628).

    A deploy ships source changes to the world, and the code-understanding graph
    (code-graph) derives its whole machine layer — module nodes + depends-on /
    surfaces-as edges — from that source (lib/*.py・server/*.py・channel/*.mjs).
    So a deploy is the natural moment to re-check the graph against current source
    and re-seed it if drifted, mirroring how a deploy prompts an application-map
    reconcile (_fire_map_reconcile_trigger). Without this, the graph only drifts
    from source and is re-checked on demand — the "0-drift *verified*" becomes
    "0-drift *verifiable*" gap the ms-156 target-close review flagged
    (root_target.py went missing from the graph after merge).

    Mirror-but-not-identical to _fire_map_reconcile_trigger: the *shape* (fire a
    ship-time prompt, best-effort, no-op when the artifact is absent) matches, but
    the applicability *gate* differs. Map gates on _application_map_applies() (the
    deliverable-declaration check); this gates on the graph nodes doc actually
    existing (get_document(NODES_DOC_ID)) — a project that never seeded the graph,
    e.g. any non-source project whose doc id resolves to nothing, is a no-op here.

    The message is *check-first*: it tells the reader to run check-graph-drift and
    re-seed ONLY on drift, never to run the cloud-writing `seed --update`
    unconditionally. That matters because a session-start AI can receive this
    trigger without ever entering the /beacon-deploy Skill, so the trigger text
    must itself carry the "verify before write" ordering (AX review PR#682) — an
    unconditional re-seed instruction would bypass the write-confirmation boundary.
    The re-seed itself is scripts/seed-code-graph.py's work; this trigger only
    prompts it. Degrades silently with no store / no graph / IO error.
    """
    try:
        import code_graph_store
        doc = get_store().get_document(code_graph_store.NODES_DOC_ID)
    except Exception:
        return
    if not doc:
        return
    try:
        triggers_dir = _get_triggers_dir()
        os.makedirs(triggers_dir, exist_ok=True)
        import datetime
        now_iso = datetime.datetime.now().isoformat()
        trigger_data = {
            "name": "graph-reseed",
            "kind": "graph-reseed",
            "message": (
                "デプロイを記録しました。コード理解グラフ (code-graph) が現在ソースと"
                "ズレていないか `python3 scripts/check-graph-drift.py` で照合し、"
                "drift があれば `python3 scripts/seed-code-graph.py --derive --update` で"
                "再 seed してください (照合が先、再 seed は drift 時のみ = cloud 書き込みを"
                "無条件に走らせない。出荷で module / 依存 / surface が動いた可能性があります)。"
            ),
            "created_at": now_iso,
            "refreshed_at": now_iso,
        }
        with open(os.path.join(triggers_dir, "graph-reseed.json"), "w", encoding="utf-8") as f:
            json.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")
    except OSError:
        return


def _next_deploy_id(data: dict, date_str: str) -> str:
    """Generate next deploy ID like deploy-20260517-1."""
    prefix = f"deploy-{date_str.replace('-', '')[:8]}"
    nums = []
    for d in data.get("deployments", []):
        if d["id"].startswith(prefix + "-"):
            try:
                nums.append(int(d["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def _next_release_id(data: dict, date_str: str) -> str:
    prefix = f"release-{date_str.replace('-', '')[:8]}"
    nums = []
    for r in data.get("releases", []):
        if r["id"].startswith(prefix + "-"):
            try:
                nums.append(int(r["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def _is_default_prod_backend(environment, backend):
    """True when a deploy record targets the real prod (the box behind
    beacon-ai.dev), i.e. the ``deployed-prod`` marker should move.

    ``backend == ""`` means "no profile resolved" and ``"default"`` is the
    default profile name — both denote the same primary prod gateway, distinct
    from ``aws-ga`` / ``trailnode`` / other backends which must NOT move the
    prod marker. Extracted as a named predicate (ms-105, maintainability review
    2026-07-30) so this backend-equivalence rule lives in one greppable place.
    """
    return environment == "prod" and (backend or "") in ("", "default")


def _update_deployed_prod_marker(rev, json_mode=False):
    """Force-move the ``deployed-prod`` git tag to ``rev`` and push it (ms-105).

    Single source of truth for "what rev prod should be serving", read
    token-free by the deploy-health monitor (.github/workflows/
    deploy-health-monitor.yml → scripts/deploy-health-monitor.py). Best-effort:
    a failed tag / push never raises, so recording a deploy can't fail on a
    marker hiccup.

    Returns a result dict ``{"rev", "updated": bool, "error": str|None}`` so the
    caller can surface a failure even in ``--json`` mode — a silently-swallowed
    push failure would recreate the exact marker-drift the monitor exists to
    catch (AX review 2026-07-30). In non-JSON mode it also prints a human line.
    """
    import subprocess as _sp
    result = {"rev": (rev or "").strip(), "updated": False, "error": None}
    rev = result["rev"]
    if not rev:
        result["error"] = "no rev to mark"
        return result
    try:
        _sp.run(["git", "tag", "-f", "deployed-prod", rev],
                check=True, capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001 — best-effort, never fail the record
        result["error"] = f"tag failed: {e}"
        if not json_mode:
            print(f"  ⚠ deployed-prod タグの更新に失敗しました ({e}). "
                  f"監視は次回の記録で追随します。")
        return result
    try:
        _sp.run(["git", "push", "-f", "origin", "deployed-prod"],
                check=True, capture_output=True, text=True, timeout=30)
        result["updated"] = True
        if not json_mode:
            print(f"  deployed-prod → {rev} (deploy-health 監視の基準を更新)")
    except Exception as e:  # noqa: BLE001
        result["error"] = f"push failed: {e}"
        if not json_mode:
            print(f"  ⚠ deployed-prod タグの push に失敗しました ({e}). "
                  f"`git push -f origin deployed-prod` を手動で実行してください。")
    return result


def cmd_deploy_record():
    """Record a deployment entry (major or minor) based on recent commits."""
    import subprocess as _sp
    mode = os.environ.get("BEACON_MODE", "")          # "prepare" or "finalize" or ""
    revision = os.environ.get("BEACON_REVISION", "")
    semver = os.environ.get("BEACON_SEMVER", "")
    # version (e-1274): git tag attached to this deploy (e.g. v0.22.0). Recorded
    # in the entry for cross-referencing with releases. Defaults to --semver
    # when --version not supplied so existing release flows backfill cleanly
    # without a CLI change.
    version = os.environ.get("BEACON_VERSION", "") or semver
    description = os.environ.get("BEACON_DESCRIPTION", "")
    deploy_hash = os.environ.get("BEACON_HASH", "")   # override: specify deployed commit
    deploy_date = os.environ.get("BEACON_DATE", "")   # override: specify deploy datetime
    insert_before = os.environ.get("BEACON_INSERT_BEFORE", "")  # insert before this deploy-id
    type_override = os.environ.get("BEACON_TYPE", "")  # override: "major" or "minor"
    environment = os.environ.get("BEACON_ENVIRONMENT", "prod")
    # ms-80 e-1831: backend (= GCP Cloud Run / AWS / TrailNode / custom) を deploy 記録に保存し
    # backend 別に追えるように。env 未指定 + cloud profile があれば profile name を fallback。
    backend = os.environ.get("BEACON_BACKEND", "").strip()
    if not backend:
        # Active profile を引いて backend を auto-detect (= 例: profile=aws-ga → backend="aws-ga")
        try:
            import profile as _profile
            backend = _profile.resolve_active_profile().name or ""
        except Exception:
            backend = ""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    now = deploy_date or core._now_iso()
    today = now[:10]

    # Resolve the target hash (short form)
    if deploy_hash:
        try:
            deploy_hash = _sp.check_output(
                ["git", "rev-parse", "--short", deploy_hash],
                stderr=_sp.DEVNULL, text=True
            ).strip()
        except Exception:
            pass

    # For retroactive inserts, find the previous deploy in insertion order
    deployments = data.get("deployments", [])
    if insert_before:
        idx = next((i for i, d in enumerate(deployments) if d["id"] == insert_before), len(deployments))
        prev_hash = deployments[idx - 1]["git_hash"] if idx > 0 else ""
        after_hash = deploy_hash or (deployments[idx]["git_hash"] if idx < len(deployments) else "HEAD")
    else:
        prev_hash = deployments[-1]["git_hash"] if deployments else ""
        after_hash = deploy_hash or "HEAD"

    # Collect commits in the range prev_hash..after_hash
    try:
        if prev_hash:
            log_out = _sp.check_output(
                ["git", "log", f"{prev_hash}..{after_hash}", "--format=%H %s"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
        else:
            log_out = _sp.check_output(
                ["git", "log", after_hash, "--format=%H %s", "-50"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
    except Exception:
        log_out = ""

    new_commits = []
    for line in log_out.splitlines():
        if line.strip():
            parts = line.split(" ", 1)
            new_commits.append({"hash": parts[0][:7], "message": parts[1] if len(parts) > 1 else ""})

    head_hash = deploy_hash or (new_commits[0]["hash"] if new_commits else _sp.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True).strip())

    # Map commit hashes to milestones via beacon entries
    commit_hashes = [c["hash"] for c in new_commits]
    ms_status: dict[str, str] = {ms["id"]: ms.get("status", "") for ms in data.get("milestones", [])}

    # MSes that already appeared in previous deploys → they are patched, not newly completed
    previously_deployed: set[str] = set()
    for d in deployments:
        previously_deployed.update(d.get("newly_completed_ms", []))
        previously_deployed.update(d.get("milestones", []))  # legacy records

    # Find which MSes are touched by these commits, build per-MS commit lists
    newly_completed: set[str] = set()
    patch_ms: set[str] = set()
    milestone_commits: dict[str, list[str]] = {}  # ms_id -> [commit_hashes]

    for ms in data.get("milestones", []):
        ms_id = ms["id"]
        matched: list[str] = []

        def _scan(entries, _matched=matched, _ms_id=ms_id):
            for e in entries:
                if e.get("type") == "commit":
                    h = (e.get("meta") or {}).get("hash", "")
                    if h:
                        for c in commit_hashes:
                            if (h.startswith(c) or c.startswith(h)) and c not in _matched:
                                _matched.append(c)
                                status = ms_status.get(_ms_id, "")
                                if status in ("done", "observing") and _ms_id not in previously_deployed:
                                    newly_completed.add(_ms_id)
                                else:
                                    patch_ms.add(_ms_id)
                for child in e.get("entries", []):
                    _scan([child], _matched, _ms_id)
        _scan(ms.get("entries", []))

        if matched:
            milestone_commits[ms_id] = matched

    # Commits not associated with any milestone
    assigned_hashes = {c for cs in milestone_commits.values() for c in cs}
    unassigned_commits = [c for c in commit_hashes if c not in assigned_hashes]

    # Determine type (allow manual override)
    deploy_type = type_override if type_override in ("major", "minor") else ("major" if newly_completed else "minor")
    affected_ms = sorted(newly_completed if newly_completed else patch_ms)

    # --- Prepare mode: return context JSON for AI description generation ---
    if mode == "prepare":
        def _ms_context(ms_id):
            ms = next((m for m in data.get("milestones", []) if m["id"] == ms_id), {})
            entries = []
            def _collect(es):
                for e in es:
                    if e.get("type") == "commit" and len(entries) < 5:
                        h = (e.get("meta") or {}).get("hash", "")
                        if h and any(h.startswith(c) or c.startswith(h) for c in commit_hashes):
                            entries.append({"id": e.get("id",""), "description": e.get("description",""), "hash": h})
                    for child in e.get("entries", []):
                        _collect([child])
            _collect(ms.get("entries", []))
            return {"id": ms_id, "title": ms.get("title", ms_id), "commit_entries": entries}

        payload = {
            "deploy_type": "major" if newly_completed else "minor",
            "new_commits": new_commits[:20],
            "newly_completed_ms": [_ms_context(mid) for mid in sorted(newly_completed)],
            "patch_ms": [_ms_context(mid) for mid in sorted(patch_ms)],
            "unassigned_commits": unassigned_commits,
            "last_deploy": {"id": deployments[-1]["id"], "date": deployments[-1].get("date","")} if deployments else None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    # Auto-generate description (fallback if not AI-provided)
    if not description:
        ms_titles = []
        for ms in data.get("milestones", []):
            if ms["id"] in affected_ms:
                ms_titles.append(ms.get("title", ms["id"]))
        description = "・".join(ms_titles) if ms_titles else "deploy"

    deploy_id = _next_deploy_id(data, today)

    # Find links_to for minor: find the most recent major deploys that touch the same MSes
    links_to = []
    if deploy_type == "minor":
        for d in reversed(deployments):
            if d.get("type") == "major":
                if any(m in d.get("milestones", []) for m in affected_ms):
                    links_to.append(d["id"])
            if len(links_to) >= 3:
                break

    deploy_entry = {
        "id": deploy_id,
        "type": deploy_type,
        "date": now,
        "git_hash": head_hash,
        "environment": environment,
        "milestones": affected_ms,
        "newly_completed_ms": sorted(newly_completed),
        "patch_ms": sorted(patch_ms),
        "milestone_commits": milestone_commits,
        "unassigned_commits": unassigned_commits,
        "commit_hashes": commit_hashes,
        "description": description,
        "linked_release": None,
    }
    if revision:
        deploy_entry["cloud_run_revision"] = revision
    if version:
        # e-1274: top-level version tag (e.g. v0.22.0). Surfaces in Releases tab
        # without needing to dereference linked_release → releases[*].semver.
        deploy_entry["version"] = version
    if backend:
        # ms-80 e-1831: backend 名 (= 例 "default" / "aws-ga" / "trailnode")。multi-backend
        # 運用で「どの backend に何が反映されたか」を deploy 別に追えるように。
        deploy_entry["backend"] = backend
    if links_to:
        deploy_entry["links_to"] = links_to

    # Handle semver: create a Release entry and link
    release_entry = None
    if semver:
        release_id = _next_release_id(data, today)
        release_entry = {
            "id": release_id,
            "date": now,
            "semver": semver,
            "deploy_ids": [deploy_id],
            "description": description,
        }
        # e-1274: also stash the raw tag (e.g. v0.22.0). semver is the bare
        # number; version preserves the "v" prefix when the workflow passed it.
        if version and version != semver:
            release_entry["version"] = version
        deploy_entry["linked_release"] = release_id
        data.setdefault("releases", []).append(release_entry)
        # Create git tag
        try:
            _sp.run(["git", "tag", semver], check=True, capture_output=True)
            if not json_mode:
                print(f"Tagged: {semver}")
        except _sp.CalledProcessError:
            if not json_mode:
                print(f"Warning: git tag {semver} already exists or failed")

    dep_list = data.setdefault("deployments", [])
    if insert_before:
        idx = next((i for i, d in enumerate(dep_list) if d["id"] == insert_before), len(dep_list))
        dep_list.insert(idx, deploy_entry)
    else:
        dep_list.append(deploy_entry)
    save_project(data)

    # ms-105 e-4600: move the `deployed-prod` git marker so the deploy-health
    # monitor (GitHub Actions, token-free) can compare prod's live /api/version
    # rev against the rev we intended to deploy. main HEAD stopped being that
    # truth when the VPS pull-timer was disabled (2026-07-28) and prod began
    # tracking manual deploys only. Only the real prod backend moves the marker;
    # aws-ga / trailnode / non-prod deploys must not. Best-effort — a failed
    # tag/push must never fail the record (the monitor's soft "unrecorded deploy"
    # nudge covers a missed marker).
    marker_result = None
    if _is_default_prod_backend(environment, backend):
        marker_result = _update_deployed_prod_marker(head_hash, json_mode)

    # ms-104 e-3154: deploy = surface が世に出る節目。全貌マップの reconcile を促す。
    _fire_map_reconcile_trigger()
    # ms-156 e-5628: 同じ節目でコード理解グラフ (code-graph) の再 seed を促す
    # (機械層は出荷したソースから導出されるので、出荷で drift しうる)。
    _fire_graph_reseed_trigger()

    if json_mode:
        out = {"deploy": deploy_entry}
        if release_entry:
            out["release"] = release_entry
        # Surface the marker outcome so a --json caller can detect a failed push
        # (AX review 2026-07-30 — it was silently swallowed in JSON mode before).
        if marker_result is not None:
            out["deployed_prod_marker"] = marker_result
        print(json.dumps(out, ensure_ascii=False))
    else:
        icon = "◉" if deploy_type == "major" else "○"
        ms_str = " ".join(f"[{m}]" for m in affected_ms) or "(no MS detected)"
        ver_str = f" {version}" if version and not semver else ""
        backend_str = f" <backend={backend}>" if backend else ""
        print(f"{icon} {deploy_id}{ver_str} [{deploy_type}]{backend_str} {ms_str}")
        print(f"  {description}")
        if semver:
            print(f"  Release: {release_entry['id']} ({semver})")
        if links_to:
            print(f"  Patches: {', '.join(links_to)}")


def cmd_deploy_list():
    """List deployment records, optionally filtered by environment or backend."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    env_filter = os.environ.get("BEACON_ENVIRONMENT", "")
    # ms-80 e-1831: backend 別 filter (= 例 --backend aws-ga で AWS だけ列挙)
    backend_filter = os.environ.get("BEACON_BACKEND", "").strip()
    data = load_project()
    deployments = data.get("deployments", [])
    releases = {r["id"]: r for r in data.get("releases", [])}

    if env_filter:
        deployments = [d for d in deployments if d.get("environment", "prod") == env_filter]
    if backend_filter:
        deployments = [d for d in deployments if d.get("backend", "") == backend_filter]

    if json_mode:
        print(json.dumps({"deployments": deployments, "releases": list(releases.values())},
                         ensure_ascii=False))
        return

    if not deployments:
        msg = f"No deployments recorded for env='{env_filter}'." if env_filter else "No deployments recorded yet."
        print(msg)
        return

    for d in reversed(deployments):
        icon = "◉" if d.get("type") == "major" else "○"
        rel = releases.get(d.get("linked_release", ""))
        semver_str = f" {rel['semver']}" if rel and rel.get("semver") else ""
        ms_str = " ".join(d.get("milestones", [])) or "-"
        env_str = f" [{d.get('environment', 'prod')}]" if d.get("environment") not in (None, "prod") else ""
        backend_str = f" <{d['backend']}>" if d.get("backend") else ""
        print(f"{icon} {d['id']}{semver_str}  {d['date'][:10]}  [{d.get('type','')}]{env_str}{backend_str}  {ms_str}")
        print(f"   {d.get('description', '')}")
        if d.get("links_to"):
            print(f"   patches: {', '.join(d['links_to'])}")


def cmd_deploy_delete():
    """Deprecated: physical deletion of deploy records is not allowed."""
    print("Error: 'beacon deploy delete' is deprecated.")
    print("  Deploy records are immutable facts — they cannot be physically deleted.")
    print("  To mark a record as invalid: beacon deploy void <id> --reason \"...\"")
    sys.exit(1)


def cmd_deploy_rollback():
    """Roll back to the deployment that ran *before* the most recent one (e-581).

    Plan:
      1. Find the latest non-voided deploy in `data["deployments"]`.
      2. Find the deploy immediately before it (same `service` if recorded).
      3. Print the `gcloud run services update-traffic ... --to-revisions ...`
         command the user should run.
      4. Optionally execute it (when --execute is passed).
      5. Mark the rolled-back deploy as voided with reason="rolled back to
         <prev-id>" so the timeline preserves the decision.

    This is an audit-first design: we never silently execute gcloud. The
    user must opt in with `--execute`, or read the command and run it
    themselves. Reason matches CORE doc `data-immutability-principle`:
    void carries a reason; the reason is what makes the timeline auditable.
    """
    reason = os.environ.get("BEACON_REASON", "")
    execute = os.environ.get("BEACON_EXECUTE", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    service_override = os.environ.get("BEACON_SERVICE", "")

    if not reason:
        print("Error: --reason is required for deploy rollback.", file=sys.stderr)
        print("  Example: beacon deploy rollback --reason \"high error rate on prod\"",
              file=sys.stderr)
        sys.exit(1)

    data = load_project()
    deployments = data.get("deployments", []) or []
    # Sort by date desc; void records may exist but should still be present.
    active = [d for d in deployments if not d.get("voided")]
    if not active:
        print("Error: no active deployments to roll back.", file=sys.stderr)
        sys.exit(1)
    active.sort(key=lambda d: d.get("deployed_at") or d.get("date", ""), reverse=True)

    current = active[0]
    service = service_override or current.get("service", "")
    previous = None
    for d in active[1:]:
        # Match by service when possible; fall back to "most recent earlier" otherwise.
        if not service or d.get("service") == service:
            previous = d
            break

    if not previous:
        print(
            "Error: cannot determine rollback target — only one active deploy "
            "exists for this service. Roll forward instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    prev_rev = previous.get("revision", "") or previous.get("meta", {}).get("revision", "")
    if not prev_rev:
        print(
            "Error: previous deploy has no `revision` recorded — cannot construct "
            "the gcloud update-traffic command. Add it with "
            f"`beacon deploy update {previous['id']} --revision <rev>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    region = current.get("region", "") or os.environ.get("BEACON_REGION", "asia-northeast1")
    gcloud_cmd = [
        "gcloud", "run", "services", "update-traffic", service,
        f"--to-revisions={prev_rev}=100",
        f"--region={region}",
    ]
    cmd_str = " ".join(gcloud_cmd)

    print(f"Rolling back {service}:")
    print(f"  current  → {current['id']}  rev={current.get('revision', '?')}")
    print(f"  rollback → {previous['id']}  rev={prev_rev}")
    print(f"  command  → {cmd_str}")
    print()

    if execute:
        print("=> executing gcloud command...")
        r = subprocess.run(gcloud_cmd)
        if r.returncode != 0:
            print("Error: gcloud command failed; deploy record NOT voided.", file=sys.stderr)
            sys.exit(r.returncode)
    else:
        print(
            "Not executed. Run the command above, then re-invoke with --execute "
            "to mark the deploy as voided automatically."
        )
        if not json_mode:
            sys.exit(0)

    # Void the rolled-back deploy so the project timeline reflects the
    # decision. We reuse cmd_deploy_void's core function via apply_operation.
    try:
        import operations
        project_id = _project_id_for_ops()

        full_reason = f"rolled back to {previous['id']} ({prev_rev}): {reason}"

        def op(d):
            voided = core.deploy_void(d, current["id"], reason=full_reason)
            return d, voided

        voided = operations.apply_operation(
            project_id, op, op_name="deploy.rollback", reason=full_reason,
        )
    except Exception as e:
        print(f"Warning: failed to void deploy record: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps({
            "voided_deploy": current["id"],
            "rollback_target": previous["id"],
            "rollback_revision": prev_rev,
            "executed": execute,
        }, ensure_ascii=False))
    else:
        print(f"✓ Voided {current['id']} with reason: {full_reason}")


def cmd_deploy_void():
    """Mark a deployment record as voided (immutable, never physically deleted)."""
    deploy_id = os.environ.get("BEACON_DEPLOY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not deploy_id:
        print("Error: deploy ID required", file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for deploy void.", file=sys.stderr)
        print("  Example: beacon deploy void <id> --reason \"誤ったハッシュで記録\"", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        dep = core.deploy_void(data, deploy_id, reason=reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    save_project(data, op={"op": "deploy_void", "deploy_id": dep["id"], "reason": reason})
    if json_mode:
        print(json.dumps({"id": dep["id"], "voided": True}, ensure_ascii=False))
    else:
        print(f"Voided: {dep['id']}")
        print(f"  Reason: {reason}")
