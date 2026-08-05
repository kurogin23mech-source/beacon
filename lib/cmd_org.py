#!/usr/bin/env python3
"""cmd_org.py — the `beacon org *` and `beacon member *` command families (ms-127 e-4318).

Extracted verbatim from commands.py (the god-module split). Holds the
organization and project-membership CLI handlers and their member-only private
helpers (_member_identity / _build_owner_row / _annotate_external_guests /
_resolve_cloud_project_id). Depends only on commands_shared (upward) + leaf
domain modules (core / store / org / operations / auth / api_client, most
imported lazily inside functions), never on commands.py — acyclic (SPEC 方針4).
commands.py re-imports these names so `import commands; commands.cmd_org_create()`
and the dispatch dict keep resolving.
"""

import json
import os
import sys
from typing import Optional

from store import get_store
import core

from commands_shared import (
    get_project_file,
    load_project,
    _is_cloud_mode,
    _resolve_active_api_url,
    _extract_token,
    _project_id_for_ops,
    _resolve_creator_identity,
    _rename_local_project_json_for_cloud_cutover,
)


# --- org (organization) family ---

def cmd_org_create():
    """Create a team org (= 明示的に立てる組織). Caller becomes owner (ms-118 / e-4231).

    Reads from env:
      BEACON_ORG_NAME    (required) org display name (法人名など)
      BEACON_USER_ID     creator user_id (local mode; cloud resolves from token)
      BEACON_USER_EMAIL  creator email (local mode; cloud resolves from token)
      BEACON_JSON        "1" → emit json instead of human text

    org 所属はアクセスを与えない (participation-only): 作った org に社員を招いても、
    社員は別途 project に参加させて初めてその project が見える。
    """
    name = os.environ.get("BEACON_ORG_NAME", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not name:
        print("Error: org name is required (beacon org create \"名前\")",
              file=sys.stderr)
        sys.exit(1)

    # local mode は auth token が無いので作成者 identity を渡す (cloud は無視して
    # server が token から解決する)。
    user_id, email, _ = _resolve_creator_identity()
    try:
        doc = get_store().create_org(
            name=name, creator_user_id=user_id, creator_email=email)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print(f"Created org {doc['org_id']} \"{doc['name']}\"")
        owner = (doc.get("members") or [{}])[0]
        print(f"  owner: {owner.get('email') or owner.get('user_id')}")
        # 招待は所属だけを与え、アクセスは別途 project 参加で付く (participation-only)。
        # `org invite` は後続タスク e-4232 で追加予定なので、未実装のコマンドを
        # 案内しない (= ツールが実在しない次アクションを指し示さない)。
        print("  組織に社員を招く導線 (org invite) は後続 e-4232 で追加予定です。"
              "招いても所属だけで、必要な project に `beacon member add` で参加させて"
              "初めてその project が見えます (participation-only)。")


def cmd_org_list():
    """List orgs the caller is a member of (ms-118 / e-4231).

    Reads from env:
      BEACON_USER_ID   visibility filter (defaults to current user; org は自分が
                       member のものだけ出す)
      BEACON_ORG_ALL   "1" → disable the actor filter (admin view, local only)
      BEACON_JSON      "1" → emit json
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    all_orgs = os.environ.get("BEACON_ORG_ALL", "") == "1"

    if all_orgs:
        # --all (= 全件 admin view) は local mode 専用。cloud は auth token で
        # サーバ側 filter されるため --all を渡しても効かない → 黙って素通り
        # させず明示拒否する (= 全件を見たつもりが自分の org しか見えない誤解を防ぐ)。
        if _is_cloud_mode():
            print("Error: --all は local mode 専用です "
                  "(cloud では自分が member の org のみ表示されます)。",
                  file=sys.stderr)
            sys.exit(1)
        user_id = None
    else:
        uid, _, _ = _resolve_creator_identity()
        # identity が解決できないときに黙って全件 admin view へ昇格しない
        # (= help は「自分が member の org」と謳っているので、他 user の org を
        # 混ぜて返すのは silent な開示違反)。
        if not uid:
            print("Error: 自分の identity が解決できませんでした (member 一覧を絞れません)。\n"
                  "  `beacon member whoami` で確認するか、全件を見るなら --all を指定してください。",
                  file=sys.stderr)
            sys.exit(1)
        user_id = uid

    try:
        orgs = get_store().list_orgs(user_id=user_id)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(orgs, ensure_ascii=False, indent=2))
        return

    if not orgs:
        print("(no orgs yet — `beacon org create \"名前\"` で最初の組織を立てる)")
        return

    print(f"Orgs ({len(orgs)}):")
    for o in orgs:
        members = o.get("members") or []
        personal = " [personal]" if o.get("personal") else ""
        print(f"  {o.get('org_id')} \"{o.get('name')}\"{personal} "
              f"— members: {len(members)}")


def cmd_org_show():
    """Show a single org by id (ms-118 / e-4231).

    Reads from env:
      BEACON_ORG_ID  (required) the org id to show
      BEACON_JSON    "1" → emit json

    member でない org は not found として扱う (= 存在を漏らさない)。
    """
    org_id = os.environ.get("BEACON_ORG_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not org_id:
        print("Error: org id is required (beacon org show <org-id>)",
              file=sys.stderr)
        sys.exit(1)

    try:
        doc = get_store().get_org(org_id)
    except ValueError as e:
        # not found: 有効な id を知る導線を添える。cloud では「member でない実在 org」も
        # 同じ not found になる (= 存在を漏らさない業務規則) 点も明かし、id 綴りの
        # 誤診で当て推量リトライに入るのを防ぐ。
        print(f"Error: {e}", file=sys.stderr)
        print("  自分が member の org は `beacon org list` で確認できます "
              "(member でない org も not found として扱われます)。", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return

    personal = " [personal]" if doc.get("personal") else ""
    print(f"Org {doc.get('org_id')} \"{doc.get('name')}\"{personal}")
    print(f"  created: {doc.get('created_at', '')}")
    members = doc.get("members") or []
    print(f"  members ({len(members)}):")
    for m in members:
        print(f"    - {m.get('email') or m.get('user_id')} ({m.get('role')})")


def cmd_org_add_member():
    """Add a member into an org — 所属だけ与えアクセスは付けない (ms-118 / e-4232).

    CLI verb は `beacon org add-member` (別名 `invite`)。承諾フローは無く即時に
    member になる (= project 側の token+accept 招待とは別物)。

    Reads from env:
      BEACON_ORG_ID     (required) target org id
      BEACON_ORG_EMAIL  (required) member email (user-id は不可)
      BEACON_ORG_ROLE   member | admin (default member)
      BEACON_JSON       "1" → emit json
    """
    org_id = os.environ.get("BEACON_ORG_ID", "").strip()
    email = os.environ.get("BEACON_ORG_EMAIL", "").strip()
    role = os.environ.get("BEACON_ORG_ROLE", "").strip() or "member"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not org_id or not email:
        print("Usage: beacon org add-member <org-id> <email> [--role member|admin]",
              file=sys.stderr)
        sys.exit(1)
    try:
        org = get_store().add_org_member(org_id, email=email, role=role)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(org, ensure_ascii=False))
    else:
        print(f"Added {email} to org {org.get('org_id')} "
              f"\"{org.get('name')}\" (role={role})")
        print("  所属のみ付与しました (即時、承諾フローなし)。"
              "この社員はまだどの project も見えません。")
        print("  アクセスは必要な project で `beacon member add` を実行して初めて付きます "
              "(participation-only)。")


def cmd_org_remove_member():
    """Remove a member from an org (ms-118 / e-4232).

    Reads from env:
      BEACON_ORG_ID      (required) target org id
      BEACON_ORG_MEMBER  (required) member to remove (user_id or email)
      BEACON_JSON        "1" → emit json
    """
    org_id = os.environ.get("BEACON_ORG_ID", "").strip()
    target = os.environ.get("BEACON_ORG_MEMBER", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not org_id or not target:
        print("Usage: beacon org remove-member <org-id> <email-or-userid>",
              file=sys.stderr)
        sys.exit(1)
    try:
        org = get_store().remove_org_member(org_id, target=target)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(org, ensure_ascii=False))
    else:
        print(f"Removed {target} from org {org.get('org_id')} "
              f"\"{org.get('name')}\"")
        print(f"  remaining members: {len(org.get('members') or [])}")


def cmd_org_delete():
    """Delete a team org — 破壊的操作なので owner のみ (ms-118 / e-4234).

    二段確認 (= 誤発火防止) は `/beacon-member` Skill 側の責務。CLI は primitive として
    owner-only ガード (cloud は server が token で enforce) と personal org 削除禁止を
    保証する。project の org 所属リンクの後始末はしない (= re-home で先に移すのが前提)。

    Reads from env:
      BEACON_ORG_ID  (required) the org id to delete
      BEACON_JSON    "1" → emit json
    """
    org_id = os.environ.get("BEACON_ORG_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not org_id:
        print("Error: org id is required (beacon org delete <org-id>)",
              file=sys.stderr)
        sys.exit(1)
    try:
        result = get_store().delete_org(org_id)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Deleted org {result.get('org_id')}")
        print("  この org に home していた project があれば、所属 org が無くなります。"
              "先に `beacon org rehome <project> --to <別org>` で移しておくのが安全です。")


def cmd_org_rehome():
    """Re-home a project into an org — org 所属リンクだけ張り替える (ms-118 / e-4233).

    既存 project の identity (project_id) と履歴は保ったまま所属 org を差し替える。
    開示は移動後の org 基準で即座に再評価される (= ms-113 の開示は現在の org_id を
    live 参照。participation-only なので、参加していない社員には引き続き見えない)。

    Reads from env:
      BEACON_ORG_REHOME_PROJECT  (required) project id to move (= 付け替える project)
      BEACON_ORG_REHOME_TARGET   (required) destination org id (`--to`)
      BEACON_JSON                "1" → emit json
    """
    project_id = os.environ.get("BEACON_ORG_REHOME_PROJECT", "").strip()
    target_org_id = os.environ.get("BEACON_ORG_REHOME_TARGET", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not project_id or not target_org_id:
        print("Usage: beacon org rehome <project-id> --to <org-id> [--json]",
              file=sys.stderr)
        sys.exit(1)
    try:
        result = get_store().rehome_project(
            project_id, target_org_id=target_org_id)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        prev = result.get("previous_org_id") or "(none)"
        print(f"Re-homed project {result.get('project_id')} "
              f"→ org {result.get('org_id')} (was: {prev})")
        print("  project の identity と履歴は保たれています (org_id リンクのみ変更)。")
        print("  開示は移動後の org 基準で即座に再評価されます "
              "(参加していない社員には引き続き見えません = participation-only)。")


# --- member (project membership) family ---

def cmd_member_add():
    """Add a member to the project."""
    import operations  # lazy import to avoid circular at module load

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    name = os.environ.get("BEACON_MEMBER_NAME", "")
    email = os.environ.get("BEACON_MEMBER_EMAIL", "")
    role = os.environ.get("BEACON_MEMBER_ROLE", "") or "contributor"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id:
        print("Error: member id is required", file=sys.stderr)
        print("Usage: beacon member add <id> [--name N] [--email E] [--role owner|maintainer|contributor|viewer]",
              file=sys.stderr)
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        new_member = core.member_add(data, member_id, name=name, email=email, role=role)
        return data, new_member

    try:
        new_member = operations.apply_operation(
            project_id, op,
            op_name="member.add",
            reason=f"add member {member_id} as {role}",
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(new_member, ensure_ascii=False))
    else:
        print(f"Added member {new_member['id']} ({new_member['role']})")
        print("  note: this adds a Beacon project member. GitHub repo")
        print("  collaborator access is separate — set via `gh repo edit --add-collaborator <user>`.")


def _build_owner_row(data: dict) -> Optional[dict]:
    """Build a project-owner row in the same shape as members[] (ms-95 e-2288).

    Background: `project.json` stores the project owner in a top-level
    ``owner`` field (= user_id string) that is structurally separate from
    ``members[]``. Historically `beacon member list` only iterated
    ``members[]`` and silently omitted the owner — leading AI / human readers
    to mis-identify the highest-privilege actor (= 2026-06-23 incident:
    profile-extractor AI sent a DM to the wrong recipient because the
    `member list` output hid the actual project owner).

    Returns a dict mirroring the members[] row shape so JSON consumers can
    iterate one combined list:
      {
        "user_id":      <str>,
        "email":        <str (best-effort, "" in pure local mode)>,
        "display_name": <str (best-effort, "" in pure local mode)>,
        "role":         "owner",
        "source":       "project.owner",  # provenance marker (= AC #2 audit)
      }

    Resolution strategy for email / display_name:
      1. **Cloud mode** (``.beacon/cloud.json`` present): call
         ``GET /api/projects/{pid}/members`` — the server already enriches
         ``owner_email`` + ``owner_display_name`` from the users collection.
         Best-effort; auth / network failures fall through to (3).
      2. **Members[] cross-reference**: if a member row carries the same
         user_id as the project owner, reuse its email / display_name.
      3. **Pure local fallback**: leave email / display_name empty. The
         user_id itself is still surfaced so the row is never silently
         omitted (the original bug).

    Returns ``None`` only when ``data["owner"]`` is empty / missing
    (= legacy local-only project that never bound an owner). Callers should
    skip prepending in that case to preserve "no members" output.
    """
    owner_id = (data.get("owner") or "").strip()
    if not owner_id:
        return None

    email = ""
    display_name = ""

    # Strategy 2: scan members[] for matching user_id (works in any mode and
    # avoids an HTTP roundtrip when the data is already on hand).
    for m in core.members_list(data):
        if not isinstance(m, dict):
            continue
        if (m.get("user_id") or "") == owner_id:
            email = m.get("email", "") or email
            display_name = m.get("display_name", "") or m.get("name", "") or display_name
            break

    # Strategy 1: cloud-mode enrichment via /members endpoint. Best-effort —
    # silent on any failure (auth missing, offline, server 5xx) so local /
    # disconnected workflows still emit the row.
    if not email or not display_name:
        try:
            project_file = get_project_file()
            beacon_dir = os.path.dirname(project_file) or ".beacon"
            cloud_json = os.path.join(beacon_dir, "cloud.json")
            if os.path.exists(cloud_json):
                with open(cloud_json, "r", encoding="utf-8") as f:
                    pid = (json.load(f) or {}).get("project_id", "")
                if pid:
                    from auth import load_credentials
                    creds = load_credentials()
                    if creds is not None:
                        api_url = _resolve_active_api_url()
                        from api_client import ApiClient
                        client = ApiClient(api_url, _extract_token(creds))
                        resp = client.get(f"/api/projects/{pid}/members")
                        if isinstance(resp, dict):
                            email = email or (resp.get("owner_email") or "")
                            display_name = display_name or (
                                resp.get("owner_display_name") or ""
                            )
        except Exception:
            # Best-effort enrichment — never block the owner row on cloud
            # round-trip failures. The user_id still surfaces.
            pass

    return {
        "user_id": owner_id,
        "email": email,
        "display_name": display_name,
        "role": "owner",
        "source": "project.owner",
    }


def _member_identity(m: dict) -> str:
    """member row から org 照合に使う user 識別子を取り出す (cloud=user_id / local=id)。"""
    return (m.get("user_id") or m.get("id") or "") if isinstance(m, dict) else ""


def _annotate_external_guests(project: dict, members: list) -> None:
    """各 member row に ``external_guest: bool`` を in-place で付ける (ms-118 / e-4235).

    外部ゲスト = project 参加者のうち、project が属する team org に所属していない人
    (ms-113 / e-3735)。判定は org.external_guest_user_ids に一本化する (= team org
    限定・単一真実源)。org のロードは best-effort: 解決できない (personal org /
    org file 不在 / cloud エラー) 場合は全員 ``external_guest=False`` にして、member
    一覧の表示そのものは決して妨げない (= 可視化は付加情報)。
    """
    import org as org_mod
    for m in members:
        if isinstance(m, dict):
            m["external_guest"] = False
    org_id = org_mod.project_org_id(project)
    if not org_id or not org_mod.is_team_org_id(org_id):
        return  # personal org / org 不明 → 外部ゲストの概念が無い
    try:
        org = get_store().get_org(org_id)
    except (ValueError, RuntimeError):
        return  # ロード不能でも一覧は出す (best-effort)
    guest_ids = org_mod.external_guest_user_ids(
        org, [_member_identity(m) for m in members if isinstance(m, dict)])
    for m in members:
        if isinstance(m, dict) and _member_identity(m) in guest_ids:
            m["external_guest"] = True


def cmd_member_list():
    """List members of the project.

    ms-78 e-1807: prefer display_name (= the human-friendly label set during
    invite accept) over the raw id / email when present. Local-mode members
    use the id-based schema; cloud members use the user_id-based schema —
    the display logic accepts both.

    ms-95 e-2288: project owner (= ``data["owner"]`` top-level field) is
    prepended as the first row with ``role="owner"``. Previously the owner
    was silently omitted because the members[] iteration never inspected
    the owner field, which led AI readers to mis-identify the highest
    privileged actor on the project. The prepended row carries
    ``source="project.owner"`` so external tooling can distinguish it from
    rows that live inside members[].
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    members = list(core.members_list(data))

    # Build the synthetic owner row and decide whether it needs prepending.
    # Skip if the owner is already present in members[] with role="owner"
    # (= some projects model owner inside members[] as well; we de-dupe to
    # avoid two owner rows when both schemas hold the same identity).
    owner_row = _build_owner_row(data)
    if owner_row is not None:
        owner_id = owner_row["user_id"]
        already_in_members = any(
            isinstance(m, dict)
            and (m.get("user_id") or "") == owner_id
            and (m.get("role") or "") == "owner"
            for m in members
        )
        if not already_in_members:
            members = [owner_row, *members]

    # ms-118 / e-4235: 外部ゲスト (= project 参加だが org 非所属) を可視化する。
    # project が team org に属する場合のみ意味を持つ (= 個人 project の共同編集者は
    # guest ではない)。org のロードは best-effort — 失敗しても member 一覧は出す
    # (= 可視化の付加情報であって、一覧表示の前提条件ではない)。
    _annotate_external_guests(data, members)

    if json_mode:
        print(json.dumps(members, ensure_ascii=False, indent=2))
        return
    if not members:
        # AC #4: preserve the "no members" output path even after the owner
        # prepend logic — owner-less local projects are logically rare but
        # defensively allowed.
        print("(no members — `beacon member add <id>` to add the first one)")
        return
    print(f"Members ({len(members)}):")
    # Pretty-print: align role column for readability. Use display_name (or
    # name fallback) as the primary label; email goes in the parens.
    def label(m):
        return (m.get("display_name") or m.get("name") or
                m.get("id") or m.get("user_id") or m.get("email") or "?")
    width = max(len(str(label(m))) for m in members) + 2
    for m in members:
        role = m.get("role", "?")
        lbl = label(m)
        email = m.get("email", "")
        extras = []
        if email and email != lbl:
            extras.append(email)
        # e-4235: org 非所属の参加者は「外部ゲスト」と明示する (= 社内 member と区別)。
        if m.get("external_guest"):
            extras.append("external guest")
        extras_str = f"  ({', '.join(extras)})" if extras else ""
        print(f"  {str(lbl):<{width}} {role:<11}{extras_str}")


def cmd_member_remove():
    """Remove a member from the project."""
    import operations

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id:
        print("Error: member id is required", file=sys.stderr)
        sys.exit(1)
    if not reason:
        # e-630: state-changing operations require an audit reason.
        print(
            "Error: --reason is required for member remove "
            "(audit trail per CORE doc data-immutability-principle)",
            file=sys.stderr,
        )
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        removed = core.member_remove(data, member_id, reason=reason)
        return data, removed

    try:
        removed = operations.apply_operation(
            project_id, op,
            op_name="member.remove",
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(removed, ensure_ascii=False))
    else:
        print(f"Removed member {removed['id']}")


def cmd_member_role():
    """Change a member's role."""
    import operations

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    role = (os.environ.get("BEACON_MEMBER_ROLE", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id or not role:
        print("Error: member id and role are required", file=sys.stderr)
        print("Usage: beacon member role <id> <owner|maintainer|contributor|viewer>",
              file=sys.stderr)
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        updated = core.member_set_role(data, member_id, role)
        return data, updated

    try:
        updated = operations.apply_operation(
            project_id, op,
            op_name="member.role",
            reason=f"set role of {member_id} to {role}",
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(updated, ensure_ascii=False))
    else:
        print(f"Set {updated['id']} role to {updated['role']}")


# ---------------------------------------------------------------------------
# Member invitations (ms-78 e-1805) — token-based invite flow.
# These thin commands call the server REST API (= no local-mode equivalent;
# invitations always go through the cloud project doc). The Skill / CLI / Web
# trinity stays symmetric: the same UC11 invitation can be issued, listed,
# and cancelled from any of the three surfaces.
# ---------------------------------------------------------------------------

def _resolve_cloud_project_id() -> tuple[str, "object", "object"]:
    """Resolve the active cloud project_id + an authenticated ApiClient.

    Returns (project_id, client, creds). Raises SystemExit on failure with a
    user-friendly message. Used by `member invite / invitation list / cancel /
    join / whoami` to share one auth-resolve path.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    # Read cloud project_id from .beacon/cloud.json (= the same file
    # `beacon cloud join` writes).
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    if not os.path.exists(cloud_path):
        print(
            "No cloud project bound to this cwd.\n"
            "Run `beacon cloud join <project-id>` or `beacon cloud setup` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with open(cloud_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pid = cfg.get("project_id", "")
    except Exception as e:
        print(f"Error reading {cloud_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not pid:
        print(f"cloud.json has no project_id", file=sys.stderr)
        sys.exit(1)
    return pid, client, creds


def cmd_member_invite():
    """Issue an invite URL for a Beacon project member (ms-78 e-1805)."""
    email = (os.environ.get("BEACON_MEMBER_EMAIL", "") or "").strip()
    role = (os.environ.get("BEACON_MEMBER_ROLE", "") or "viewer").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not email:
        print("Error: email is required", file=sys.stderr)
        print("Usage: beacon member invite <email> [--role viewer|editor]",
              file=sys.stderr)
        sys.exit(1)
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.post(
            f"/api/projects/{pid}/invitations",
            {"email": email, "role": role},
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(resp, ensure_ascii=False))
        return
    url = resp.get("url", "")
    exp = (resp.get("expires_at") or "")[:10]
    print(f"Invite URL for {email} ({role}):")
    print(f"  {url}")
    print(f"  expires: {exp}")
    print()
    print("  Send this URL to the invitee. It can only be used once.")
    print("  note: this adds a Beacon project member only. GitHub repo")
    print("  collaborator access is separate — set via")
    print("  `gh repo edit --add-collaborator <user>`.")


def cmd_member_invitation_list():
    """List pending invitations for the current cloud project (ms-78 e-1805)."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.get(f"/api/projects/{pid}/invitations")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    items = resp.get("invitations", [])
    if json_mode:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("(no pending invitations)")
        return
    print(f"Pending invitations ({len(items)}):")
    for inv in items:
        exp = (inv.get("expires_at") or "")[:10]
        print(f"  {inv.get('id', '?'):<12} {inv.get('email', ''):<32} "
              f"{inv.get('role', ''):<8} expires {exp}")


def cmd_member_invitation_cancel():
    """Cancel a pending invitation by id (ms-78 e-1805)."""
    invitation_id = (os.environ.get("BEACON_INVITATION_ID", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not invitation_id:
        print("Error: invitation id required", file=sys.stderr)
        print("Usage: beacon member invitation cancel <id>", file=sys.stderr)
        sys.exit(1)
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.delete(f"/api/projects/{pid}/invitations/{invitation_id}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(resp, ensure_ascii=False))
        return
    inv = resp.get("invitation") or {}
    print(f"Cancelled invitation {invitation_id} ({inv.get('email', '')}).")


def cmd_member_join():
    """Accept an invite token and bind this cwd to the joined project (ms-78 e-1805).

    Server-side: consumes the invite + adds caller to project members[].
    Client-side: writes .beacon/cloud.json + .beacon/project.json so subsequent
    Beacon commands operate against the joined project.
    """
    token = (os.environ.get("BEACON_INVITE_TOKEN", "") or "").strip()
    display_name = (os.environ.get("BEACON_DISPLAY_NAME", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not token:
        print("Error: --token is required", file=sys.stderr)
        print("Usage: beacon member join --token <token> [--display-name <name>]",
              file=sys.stderr)
        sys.exit(1)
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login first.", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    try:
        resp = client.post(
            f"/api/invitations/{token}/accept",
            {"display_name": display_name},
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    pid = resp.get("project_id", "")
    role = resp.get("role", "")
    if not pid:
        print(f"Server returned no project_id: {resp}", file=sys.stderr)
        sys.exit(1)
    # Bind this cwd to the joined project by writing cloud.json. ms-84
    # Phase 3 (e-2037): the matching local project.json write was retired
    # — cloud mode reads through Store → StoreApi, so a local cache file
    # just decays into a silent-drift source.
    try:
        data = client.get_project(pid)
    except RuntimeError as e:
        print(f"Joined project {pid}, but failed to fetch project doc: {e}",
              file=sys.stderr)
        sys.exit(1)
    core.validate_project(data)
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    os.makedirs(beacon_dir, exist_ok=True)
    cloud_config_path = os.path.join(beacon_dir, "cloud.json")
    with open(cloud_config_path, "w", encoding="utf-8") as f:
        json.dump({"project_id": pid, "api_url": api_url}, f,
                  indent=2, ensure_ascii=False)
        f.write("\n")
    # Rename any leftover project.json (= migration from a previous local
    # install) so the cut-over is final.
    _rename_local_project_json_for_cloud_cutover(get_project_file())
    if json_mode:
        print(json.dumps({
            "status": "joined",
            "project_id": pid,
            "project_name": data.get("name", ""),
            "role": role,
            "display_name": display_name,
        }, ensure_ascii=False))
        return
    print(f"Joined project {data.get('name', pid)} ({pid}) as {role}.")
    if display_name:
        print(f"  display name: {display_name}")
    print()
    print("Next steps:")
    print("  - Run `beacon channel install` in your work directory so other")
    print("    sessions can DM you.")
    print("  - In Claude Code, invoke `/beacon-onboard` to load project context.")
    print("  note: GitHub repo collaborator access is separate — ask the")
    print("  inviter if you need write access to the repo.")


def cmd_member_whoami():
    """Show the calling user's identity + role on the current cloud project (e-1805)."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    # Best-effort: look at .beacon/cloud.json to scope role lookup.
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    pid = ""
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                pid = (json.load(f) or {}).get("project_id", "")
        except Exception:
            pid = ""
    email = ""
    sub = ""
    if isinstance(creds, dict):
        # Token payload may not include email; rely on auth claims if present.
        email = creds.get("email", "") or creds.get("user_email", "")
        sub = creds.get("sub", "") or creds.get("user_id", "")
    # Fallback: ask the server for the caller's projects + role list.
    role = ""
    project_name = ""
    if pid:
        try:
            resp = client.get(f"/api/projects/{pid}/members")
            owner_email = resp.get("owner_email", "")
            if email and owner_email == email:
                role = "owner"
            else:
                for m in resp.get("members", []) or []:
                    if (m.get("email") or "") == email:
                        role = m.get("role", "")
                        break
            # Also try to recover the project name for nicer display
            try:
                proj = client.get_project(pid)
                project_name = proj.get("name", "")
            except Exception:
                pass
        except RuntimeError:
            pass
    out = {
        "email": email,
        "user_id": sub,
        "project_id": pid,
        "project_name": project_name,
        "role": role,
    }
    if json_mode:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"email:        {email or '(unknown — token has no email claim)'}")
    if sub:
        print(f"user_id:      {sub}")
    if pid:
        print(f"project_id:   {pid}")
        if project_name:
            print(f"project_name: {project_name}")
        print(f"role:         {role or '(not a member)'}")
    else:
        print("(no cloud project bound to this cwd — run `beacon cloud join <id>`)")
