#!/usr/bin/env python3
"""cmd_doc.py — the `beacon doc *` command family (ms-127 e-4831).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (core / work_model / occupation /
table_doc / table_type / store), never on commands.py — acyclic (SPEC 方針4).
commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`; the
family-private helpers (_rewrite_doc_frontmatter / _doc_restore_revision) are
NOT re-exported (patch them at cmd_doc.<name>).

The frontmatter / table-doc / link-validation leaf helpers this family shares
with the acquisition / profile / briefing callers remaining in commands.py
(_add_frontmatter / _doc_slug / _persist_table_doc / _load_table_model /
_write_table_model / _validate_link_target_exists / _now_iso /
_split_frontmatter_raw, plus _actor_str / _sales_skill_nudge) were promoted to
commands_shared in this same change (e-4831-foundation) so those callers keep
resolving them without importing cmd_doc (which would form a cycle).

Test patch target (the e-4320 rule): a test driving a cmd_doc_* handler must
patch EVERY name the handler resolves at import time in cmd_doc's own namespace
— each `from commands_shared import name` binds an independent copy, so
`monkeypatch.setattr(commands, "get_store", ...)` is a silent no-op on the
cmd_doc call path. Patch `cmd_doc.get_store` (or the helper's home
commands_shared.<name> when tested directly) instead.
"""

import json
import urllib.parse
import os
import sys

from typing import Optional  # noqa: F401

import core
import work_model  # noqa: F401
import occupation  # noqa: F401

from commands_shared import (  # noqa: F401
    DEFAULT_SCOPE,
    VALID_SCOPES,
    load_project,
    save_project,
    verify_cloud_write_persisted,
    get_store,
    _is_cloud_mode,
    _get_api_client,
    _get_docs_dir,
    _parse_frontmatter,
    _append_changelog,
    _refuse_if_bus_origin,
    _resolve_content_input,
    _actor_str,
    _sales_skill_nudge,
    _add_frontmatter,
    _doc_slug,
    _now_iso,
    _split_frontmatter_raw,
    _validate_link_target_exists,
    _load_table_model,
    _write_table_model,
    _persist_table_doc,
)


def cmd_doc_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    ms_filter = os.environ.get("BEACON_MS", "")
    op_filter = os.environ.get("BEACON_OP", "")
    # ms-75 / e-1866: ``--trek <trek-id>`` filter — surfaces all docs
    # whose frontmatter is tagged with the given trek_id. The trek_id
    # frontmatter field has existed since ms-69 / e-1663; this filter
    # makes it usable from the CLI (= mirrors what the server-side
    # /api/treks/{tid}/documents lookup does for the Web UI).
    trek_filter = os.environ.get("BEACON_TREK_ID", "")
    # ms-109 e-3754 — target-class-agnostic filter. --target / --account /
    # --opportunity all resolve here and match a doc's linked Target via the
    # tolerant ``doc_target`` read (canonical ``target`` first, legacy keys
    # second), so ``doc list --account acc-1`` surfaces a customer's docs.
    target_filter = (os.environ.get("BEACON_TARGET", "")
                     or os.environ.get("BEACON_ACCOUNT", "")
                     or os.environ.get("BEACON_OPPORTUNITY", ""))
    # Trashed docs are hidden by default — pass --include-trashed to see
    # them in line with active ones (ms-14 e-973).
    include_trashed = os.environ.get("BEACON_INCLUDE_TRASHED", "") == "1"

    # ms-84 Phase 2: Store 経由で local / cloud を統一。 LocalStore.list_documents
    # は frontmatter 解析済の同形 dict 列を返すため、 ここでは soft-delete filter
    # と post-filter (scope / ms / op / trek) を一括で適用するだけ。
    docs = get_store().list_documents()
    if not include_trashed:
        docs = [d for d in docs if d.get("status") != "cancelled"]

    if scope_filter:
        docs = [d for d in docs if d.get("scope") == scope_filter]
    if ms_filter:
        docs = [d for d in docs if d.get("milestone") == ms_filter]
    if op_filter:
        docs = [d for d in docs if d.get("operation") == op_filter]
    if trek_filter:
        docs = [d for d in docs if d.get("trek_id") == trek_filter]
    if target_filter:
        if target_filter == "root":
            # ms-160 e-5817: root rollup. A query for the first-class root
            # Target surfaces project-level docs (linked to the root sentinel or
            # not yet linked — the pre-root "target 空" project docs) PLUS every
            # doc attached to any descendant Target, so a spec / memo / report
            # linked to a child Target (e.g. ms-104) is reachable from the root
            # too. Flat single-target links used to hide these from the root.
            import root_target
            child_ids = root_target.child_target_ids(load_project())
            docs = [d for d in docs
                    if root_target.doc_rolls_up_to_root(d, child_ids)]
        else:
            docs = [d for d in docs if work_model.doc_target(d) == target_filter]

    if json_mode:
        print(json.dumps(docs, ensure_ascii=False))
    else:
        if not docs:
            print("No documents.")
            return
        scope_icons = {"core": "*", "spec": "+", "memo": "-", "retro": "~", "report": "!"}
        for doc in docs:
            icon = scope_icons.get(doc.get("scope", "memo"), "?")
            print(f"  {icon} [{doc.get('scope', 'memo')}] {doc['doc_id']}: {doc['title']}")


# #496 review: doc show の「not-found」専用 exit code に名前を付ける (散文でなく定数で
# 自己記述)。「lookup 失敗 (API 障害等 = 例外→その他非ゼロ)」と区別するための値。send-account
# resolve の BLOCK (exit1) は『route が未設定』という別 semantic なのでここには寄せない。
EXIT_DOC_NOT_FOUND = 3


def cmd_doc_show():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # ms-84 Phase 2: Store 経由で local / cloud を統一。 Store.get_document は
    # 両 backend で同形 ({} on not-found / full dict on hit) を返す契約。
    # 例外 (API 障害 / cloud 到達不能 等) はここで raise され非ゼロ終了する — つまり
    # 「not-found」と「lookup 失敗」は別物。呼び出し側がそれを区別できるよう、not-found は
    # **専用の EXIT_DOC_NOT_FOUND (=3)** を返す (skill が『未作成(正常)=3』と『障害=その他
    # 非ゼロ』を分けられる。従来同様 exit は非ゼロなので `|| fallback` する既存呼び出しは不変)。
    doc = get_store().get_document(doc_id)
    if not doc:
        print(f"Document not found: {doc_id}")
        sys.exit(EXIT_DOC_NOT_FOUND)

    if json_mode:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print(doc.get("content", ""))


# ---------------------------------------------------------------------------
# Sales-Target doc linkage helpers (ms-109 e-3754, generalized for ms-131 e-4497).
#
# Account / opportunity / acquisition are the non-dev Target classes a doc can
# link to via the canonical ``target`` key. They differ from milestone/operation
# in two ways the doc write paths must honor: (1) they are hard-validated to
# exist before linking, and (2) they carry no dev-era milestone/operation entry
# log, so doc create/update must NOT try to record a save_entry against them.
# Centralized here so cmd_doc_add / cmd_doc_update / cmd_doc_table_create stay in
# step (ms-131 added ``acquisition`` — the acq- Target that table-doc links to).
# ---------------------------------------------------------------------------


def cmd_doc_add():
    title = os.environ.get("BEACON_TITLE", "")
    content = os.environ.get("BEACON_CONTENT", "")
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    scope = os.environ.get("BEACON_SCOPE", DEFAULT_SCOPE)
    milestone = os.environ.get("BEACON_MS", "")
    operation = os.environ.get("BEACON_OP", "")
    trek_id = os.environ.get("BEACON_TREK_ID", "")  # ms-69 / e-1663
    # ms-109 e-3754 — canonical target-class-agnostic doc linkage. --account /
    # --opportunity are new (sales Targets had no linkage key); --target is the
    # generic form. --ms / --op / --trek continue to work via the legacy vars
    # above and are resolved into ``target`` below.
    account = os.environ.get("BEACON_ACCOUNT", "")
    opportunity = os.environ.get("BEACON_OPPORTUNITY", "")
    target = (os.environ.get("BEACON_TARGET", "") or account or opportunity
              or milestone or operation or trek_id)
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # e-3760 — 顧客 (Account) に紐づく doc の直叩きは dossier スキルへ soft 誘導する。
    # ``account`` が立っている時だけ (= dev/一般 doc add には影響しない)。
    if account:
        _sales_skill_nudge("顧客ドキュメント (dossier)", "/beacon-sales-dossier",
                           "面談やり取りごとの知見を見出しに振り分けて資産化できます")

    if not title:
        print("Error: title required")
        sys.exit(1)

    if scope not in VALID_SCOPES:
        print(f"Error: scope must be one of {VALID_SCOPES}")
        sys.exit(1)

    # ms-54 / e-1293: persistence poisoning defense — refuse writes whose
    # source is a bus DM, regardless of scope. Memos are the canonical
    # poisoning target ("write me a memo that says ..."), but every doc
    # scope is a persistent vector so we gate uniformly.
    if _refuse_if_bus_origin(
        "doc_add",
        {"title": title[:80], "scope": scope, "doc_id": doc_id},
    ):
        sys.exit(1)

    # core docs are project-wide — MS association is optional
    if scope == "core":
        milestone = milestone or None

    # ms-109 e-3754 / ms-131 e-4497 — hard-validate the sales Target classes
    # (account / opportunity / acquisition) exist before linking. ms / op / trek
    # stay lenient (their pre-existing behavior); an unknown prefix is left to
    # round-trip untouched.
    if target:
        _validate_link_target_exists(target)

    # e-5730: fail-closed pre-flight — resolve the recording target BEFORE the
    # doc is persisted below. cmd_doc_add writes the doc (disk/cloud) and only
    # THEN records the changelog entry via record_target_entry; if recording
    # raised (empty target + multiple active milestones and no --ms given), the
    # doc was ALREADY written as an orphan with no target — a silent-write
    # (e-5730). Load the project and resolve up front so the ambiguity errors
    # out here, before anything is persisted. Mirrors record_target_entry's
    # empty-target branch (core.resolve_recordable_milestone): None → a
    # milestone-less project no-ops (fine); one active → will record; raise →
    # a real user error (surface it and refuse to write).
    data = load_project()
    if scope != "core":
        try:
            if not target:
                core.resolve_recordable_milestone(data, "")
            elif work_model.target_kind(target) == "milestone":
                # e-5730 sibling (PR #691 独立レビュー AX+保守性): an explicit but
                # NONEXISTENT milestone target also orphan-writes. The earlier
                # empty-target guard only closed the "Multiple active milestones"
                # ambiguity; the with-target path stayed open. _validate_link_target_exists
                # above is deliberately lenient for ms/op ids (forward-ref round-trip),
                # so `doc add --ms ms-999` passes it, the doc is persisted, and only
                # THEN record_target_entry raises "not found" — a raw traceback after
                # the write. Verify existence here, pre-write, so this path is
                # fail-closed too. Resolve through the profession-AGNOSTIC L2
                # resolver ``occupation.resolve_target`` (NOT the dev-concrete
                # ``core.find_target_milestone`` — capability-scope forbids an
                # L2 shared verb like doc_add from reaching a dev concrete; the
                # milestone-kind gate keeps trek/sales targets off this path).
                occupation.resolve_target(data, target)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

    content = _resolve_content_input(content)

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    # Duplicate guard: same title+scope already exists (ms-166 e-6044).
    # 旧挙動は "Proceeding anyway" で **重複を黙って作成** していた。cloud 同時書き込みの
    # retry と相まって同じ SPEC doc が4重生成された実害があり (2026-09-03)、これは
    # 「書き込み系が黙って重複する」silent 非機能。default では重複を作らず、既存 update か
    # 別 title を促して exit 1 で止める。意図的に重複させたい時だけ --force (BEACON_FORCE)。
    # ms-84 Phase 2: Store 経由で local / cloud を統一。dupe 判定の read が失敗したら
    # best-effort で skip (= 従来通り、read 不能を理由に書き込みを止めはしない)。
    force = os.environ.get("BEACON_FORCE", "") == "1"
    try:
        existing = get_store().list_documents()
        dupes = [d for d in existing
                 if d.get("title") == title and d.get("scope") == scope]
    except Exception:
        dupes = []
    if dupes and not force:
        existing_id = dupes[0].get("doc_id", "?")
        # 出力先の使い分け (PR#728 保守性 M2): --json 時は機械可読な error object を stdout に
        # (呼び出し側は stdout を parse して existing_doc_id で次の一手を取れる)、非 json 時は
        # 人向けテキストを stderr に。メッセージ本文は "Error:" prefix 以降を日本語で統一する
        # (PR#728 AX: 1 文中で英日が混ざると日本語非対応の agent が回復手順を読み違える)。
        if json_mode:
            print(json.dumps({"error": "duplicate",
                              "existing_doc_id": existing_id,
                              "title": title, "scope": scope}, ensure_ascii=False))
        else:
            print(f"Error: 同じ title+scope の document が既に存在します "
                  f"({existing_id} [{scope}])。重複を作らないため中止しました。"
                  f"既存を更新するなら `beacon doc update {existing_id} ...`、別物なら "
                  f"title を変えてください。意図的に重複を作る場合のみ --force を付けます。",
                  file=sys.stderr)
        sys.exit(1)
    if dupes and force:
        print(f"Warning: --force 指定のため同 title+scope の重複 "
              f"({dupes[0].get('doc_id', '?')}) を許して作成します。", file=sys.stderr)

    # Add frontmatter with scope, milestone, operation, trek_id, and the
    # canonical ``target`` linkage (ms-109 e-3754).
    content = _add_frontmatter(content, scope, milestone or "", operation or "",
                               trek_id or "", target=target or "")

    if _is_cloud_mode():
        client, config = _get_api_client()
        if doc_id:
            result = client.update_document(config["project_id"], doc_id, title, content)
        else:
            result = client.create_document(config["project_id"], title, content)
        doc_id = result["doc_id"]
        # ms-166 e-6036 (PR#728 AX high finding): verify the doc write actually landed —
        # doc add is the verb that produced the 4× silent duplicate/miss on 2026-09-03, yet
        # only milestone add verified its write. Documents live in a SEPARATE cloud
        # collection (not inside the project document), so pass list_documents as the reader
        # and check the new doc_id is present. A 2xx create that did not persist now exits
        # non-zero with a retry hint instead of a false "Saved" line.
        verify_cloud_write_persisted(
            lambda docs: any(d.get("doc_id") == doc_id for d in docs),
            what=f"document {doc_id} [{scope}] ({title})",
            reader=lambda: get_store().list_documents())
    else:
        docs_dir = _get_docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        if not doc_id:
            doc_id = _doc_slug(title)
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    # ``data`` was loaded up front for the e-5730 pre-flight guard above; reuse
    # it here (nothing between mutates it — the doc write targets disk/cloud).
    today = _now_iso()  # ms-127 e-4838: unified through the commands_shared binding
    # ms-134 e-4720: record the doc-add side effect through the occupation layer,
    # which dispatches by the Target's kind and no-ops when there is no dev-era
    # changelog to record onto — a sales Target (opportunity/account/acquisition),
    # a trek, or a project with no milestone. This replaces a direct
    # ``core.save_entry(ms_id=…)`` that required an active milestone in EVERY
    # project and so errored (write succeeded, exit non-zero) in a sales project
    # (bug e-4710/e-4711). core docs are project-wide and record nothing.
    if scope != "core":
        rec = occupation.record_target_entry(
            data, target or "", description=f"doc add: {title} ({scope})",
            source="auto", date=today, revision_id=doc_id or "")
        if rec.get("recorded"):
            save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Saved: {doc_id} [{scope}] ({title})")


def cmd_doc_update():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    content = os.environ.get("BEACON_CONTENT", "")
    title = os.environ.get("BEACON_TITLE", "")
    scope = os.environ.get("BEACON_SCOPE", "")
    milestone = os.environ.get("BEACON_MS", "")
    trek_id = os.environ.get("BEACON_TREK_ID", "")  # ms-69 / e-1663
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # e-1859: the bin/beacon wrapper sets BEACON_{MS,OP}_SET=1 whenever the
    # user typed --ms / --op (even with an empty value). This lets us treat
    # `--ms ms-1` and "did not pass --ms" differently — without it, op-scoped
    # docs silently keep their `operation:` frontmatter while gaining a
    # `milestone:` field, producing two-headed scope rows that the
    # /beacon-operation-review discovery filter can't reason about.
    ms_explicit = os.environ.get("BEACON_MS_SET", "") == "1"
    op_explicit = os.environ.get("BEACON_OP_SET", "") == "1"
    # ms-131 e-4497: same "flag absent vs passed empty" distinction for --target,
    # so ``doc update <id> --target ""`` can *detach* (clear the linkage) instead
    # of silently preserving the doc's existing target. Extends the shared doc
    # linkage uniformly (= 既存機構踏襲) so 付け外し works for every doc, tables
    # included.
    target_explicit = os.environ.get("BEACON_TARGET_SET", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # ms-54 / e-1293: persistence poisoning defense. Updating a doc with
    # bus-derived content is the same poisoning vector as creating one.
    if _refuse_if_bus_origin(
        "doc_update",
        {"doc_id": doc_id, "title": title[:80], "scope": scope},
    ):
        sys.exit(1)

    content = _resolve_content_input(content)
    # ms-131 e-4544: capture "did the user supply replacement content" NOW, while
    # ``content`` still reflects only the caller's input. ``_resolve_content_input``
    # above has already consumed any --stdin, and ``content`` is reassigned to the
    # existing body further down (``if not content: content = existing…``). Reading
    # this flag here — not at the guard site — keeps it correct regardless of that
    # later reassignment, and this line must stay after _resolve_content_input.
    user_provided_content = bool(content)

    # Fetch existing document to merge fields.
    # ms-84 Phase 2: Store 経由で local / cloud を統一。 Store.get_document は
    # 両 backend で同形 ({} on not-found / full dict on hit) を返す契約。
    existing = get_store().get_document(doc_id)
    if not existing:
        print(f"Document not found: {doc_id}")
        sys.exit(1)

    # ms-131 e-4544 — close the write-path side door (SPEC 方針4: the type-check +
    # append-only history must be the *only* way a table-doc's rows change). A
    # generic `doc update --content ...` would rewrite a table-doc's beacon-table
    # payload with no type-check and no history, and — if the new content lacks
    # frontmatter — silently drop ``format: table`` and downgrade the doc to
    # markdown. Refuse content replacement on a table-doc (identified by its stored
    # ``format: table`` frontmatter) and route the caller to the `doc table` verbs.
    # Linkage/title/scope updates (which preserve the body) stay allowed — detach
    # (e-4497) relies on `doc update --target ""`.
    import table_doc
    if user_provided_content and table_doc.is_table_content(existing.get("content", "")):
        print(f"Error: doc {doc_id} は table-doc です。行の変更は次を使ってください:\n"
              f"  beacon doc table add-row/set-cell/rm-row {doc_id}\n"
              f"  (doc update --content は型検査と履歴を迂回するため拒否しました)\n"
              f"  紐づけ/タイトル/スコープの変更は --content なしで可能です。",
              file=sys.stderr)
        sys.exit(1)

    operation = os.environ.get("BEACON_OP", "")
    # ms-109 e-3754 — canonical target linkage inputs (account / opportunity are
    # new sales Targets; --target is generic). Resolved into ``target`` below.
    account = os.environ.get("BEACON_ACCOUNT", "")
    opportunity = os.environ.get("BEACON_OPPORTUNITY", "")
    target_in = os.environ.get("BEACON_TARGET", "") or account or opportunity
    # ms-131 e-4497 — detach intent, decided from *explicit user signals only*
    # (before any preservation of existing links). "--target ''" with no other
    # link flag means "fully unlink this doc" — clear the canonical target AND
    # any legacy milestone/operation mirror a prior --ms/--op create wrote. If
    # the user also passed a link flag, that is a relink, not a detach.
    detach_target = (target_explicit and not target_in and not ms_explicit
                     and not op_explicit and not trek_id
                     and not account and not opportunity)
    # Use existing values as defaults
    if not title:
        title = existing.get("title", "")
    if not scope:
        scope = existing.get("scope", DEFAULT_SCOPE)

    # e-1859: scope (= milestone vs operation binding) is treated as mutually
    # exclusive. The three input modes:
    #   1. User passed --ms <id>  → switch to milestone scope; drop operation.
    #   2. User passed --op <id>  → switch to operation scope; drop milestone.
    #   3. User passed neither    → preserve whichever the doc already had.
    # If a user passes BOTH --ms and --op in one call, that is a programmer
    # error; we honor both literally (= same behavior as before this fix) and
    # leave the duplicated frontmatter visible so the mistake is loud, not
    # silent.
    if detach_target:
        # Full detach: clear the legacy milestone/operation mirror too, so the
        # doc ends up linked to nothing (the preservation below is skipped).
        milestone = ""
        operation = ""
    elif ms_explicit and not op_explicit:
        # Mode 1: user wants this doc on a milestone. Drop any prior op.
        operation = ""
    elif op_explicit and not ms_explicit:
        # Mode 2: user wants this doc on an operation. Drop any prior ms.
        milestone = ""
    else:
        # Mode 3 (neither flag) or both flags: preserve whatever wasn't passed.
        if not milestone:
            milestone = existing.get("milestone", "")
        if not operation:
            operation = existing.get("operation", "")

    if not trek_id:
        trek_id = existing.get("trek_id", "")
    if not content:
        content = existing.get("content", "")

    # ms-109 e-3754 / ms-131 e-4497 — resolve the canonical target. An explicit
    # ``--target ""`` (target_explicit + empty) detaches: clear the linkage
    # rather than preserving the existing one. Otherwise an explicitly passed
    # account/opportunity/acquisition/--target wins, then the resolved
    # milestone/operation/trek, then the doc's existing ``target``.
    import work_model
    if detach_target:
        target = ""
    else:
        target = (target_in or milestone or operation or trek_id
                  or existing.get("target", ""))
    # Hard-validate the sales Target classes when explicitly passed (a preserved
    # existing link was already validated at creation).
    if target_in:
        _validate_link_target_exists(target_in)

    # Rebuild with frontmatter. e-1859: _add_frontmatter is called with an
    # explicit "scope wipe" pass so the field we are dropping (= operation
    # under Mode 1, milestone under Mode 2) is removed from the existing
    # frontmatter dict instead of being left behind alongside the new field.
    # e-4497: drop_target removes the ``target`` (and its legacy mirror) on detach.
    content = _add_frontmatter(
        content, scope, milestone, operation, trek_id,
        drop_milestone=(op_explicit and not ms_explicit) or detach_target,
        drop_operation=(ms_explicit and not op_explicit) or detach_target,
        target=target or "",
        drop_target=detach_target,
    )

    # Write path still branches per backend (Phase 3 で Store.save_document
    # 化予定)。 read だけ Phase 2 で Store 経由化したので、 ここで client /
    # docs_dir を遅延 resolve する。
    if _is_cloud_mode():
        client, config = _get_api_client()
        client.update_document(config["project_id"], doc_id, title, content)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    data = load_project()
    today = _now_iso()  # ms-127 e-4838: unified through the commands_shared binding
    # ms-134 e-4720: record the doc-update side effect through the occupation
    # layer, which dispatches by the Target's kind and no-ops when there is no
    # dev-era changelog to record onto — a sales Target (opportunity/account/
    # acquisition), a trek, or a project with no milestone. Replaces a direct
    # ``core.save_entry`` that errored with "No active milestone" in a milestone-
    # less project (bug e-4710). core docs are project-wide, and a detach
    # (``--target ""``) intentionally unlinks the doc — neither records.
    # ms-134: same conditional-save pattern as cmd_doc_add / _persist_table_doc —
    # persist only when the side-effect actually recorded (a no-op record means
    # data is unchanged, so no write). Keeps the three doc write paths uniform
    # (maintainability review 2026-08-02, finding B).
    rec = {"recorded": False}
    if scope != "core" and not detach_target:
        rec = occupation.record_target_entry(
            data, target or "", description=f"doc update: {title} ({scope})",
            source="auto", date=today, revision_id=doc_id or "")
    if rec.get("recorded"):
        save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Updated: {doc_id} [{scope}] ({title})")


# ---------------------------------------------------------------------------
# Table-doc row operations (ms-131 e-4496).
#
# A table-doc's structured payload (columns / rows / per-row append-only
# history) is owned by lib/table_doc + lib/table_type. These handlers are the
# *only* write path into that payload — markdown 直編集 would break the
# invariants (型検査 / 履歴追記), so add-row / set-cell / rm-row go through the
# model, which type-checks every value and never overwrites a past one. Each
# handler loads the doc, mutates the model, and writes the whole doc back via
# the same cloud/local path cmd_doc_update uses, preserving the frontmatter
# verbatim so the format/scope/target linkage is never disturbed.
# ---------------------------------------------------------------------------


def cmd_doc_table_create():
    """Create a table-doc: a document with format:table and typed columns."""
    import table_doc
    import table_type
    title = os.environ.get("BEACON_TITLE", "")
    columns_raw = os.environ.get("BEACON_COLUMNS", "")
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    scope = os.environ.get("BEACON_SCOPE", "") or DEFAULT_SCOPE
    milestone = os.environ.get("BEACON_MS", "")
    operation = os.environ.get("BEACON_OP", "")
    # ms-131 e-4497: link via --target (any Target id incl opp-/acc-/acq-) or
    # --ms / --op. The bash + Windows dispatchers pass exactly these three env
    # vars, so we read exactly them (no dead --account/--opportunity/--trek
    # fallback that the dispatchers never populate — maintainability review of
    # PR #544).
    trek_id = ""
    target = (os.environ.get("BEACON_TARGET", "") or milestone or operation)
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: title required", file=sys.stderr)
        sys.exit(1)
    if scope not in VALID_SCOPES:
        print(f"Error: scope must be one of {VALID_SCOPES}", file=sys.stderr)
        sys.exit(1)
    if not columns_raw:
        print("Error: --columns '<json>' required (例: '[{\"key\":\"name\",\"type\":\"text\"}]')",
              file=sys.stderr)
        sys.exit(1)
    try:
        columns = json.loads(columns_raw)
    except (ValueError, TypeError) as exc:
        print(f"Error: --columns が不正な JSON です: {exc}", file=sys.stderr)
        sys.exit(1)

    if _refuse_if_bus_origin("doc_table_create",
                             {"title": title[:80], "scope": scope}):
        sys.exit(1)

    try:
        doc_id, model = _persist_table_doc(
            title=title, columns=columns, scope=scope, milestone=milestone,
            operation=operation, trek_id=trek_id, target=target, doc_id=doc_id)
    except table_doc.TableDocError as exc:
        print(f"Error: 列定義が不正です: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        # Emit full column objects (same shape as `show --json`) so an AI
        # chaining create → show sees one consistent ``columns`` shape (AX
        # review of PR #544).
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope,
                          "format": table_doc.TABLE_FORMAT,
                          "columns": model.get("columns", [])},
                         ensure_ascii=False))
    else:
        print(f"Created table: {doc_id} [{scope}] ({title}) "
              f"columns={', '.join(table_doc.column_keys(model))}")


def cmd_doc_table_add_row():
    """Append a row to a table-doc (type-checked, history-seeded)."""
    import table_doc
    import table_type
    table_type.install()
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    cells_raw = os.environ.get("BEACON_CELLS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not doc_id:
        print("Error: doc-id required", file=sys.stderr)
        sys.exit(1)
    if _refuse_if_bus_origin("doc_table_add_row", {"doc_id": doc_id}):
        sys.exit(1)
    try:
        cells = json.loads(cells_raw) if cells_raw else {}
    except (ValueError, TypeError) as exc:
        print(f"Error: --cells が不正な JSON です: {exc}", file=sys.stderr)
        sys.exit(1)

    content, title, model = _load_table_model(doc_id)
    try:
        row_id = table_doc.add_row(model, cells, actor=_actor_str(), at=_now_iso())
    except table_doc.TableDocError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _write_table_model(doc_id, title, content, model)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "row_id": row_id}, ensure_ascii=False))
    else:
        print(f"Added row {row_id} to {doc_id}")


def cmd_doc_table_set_cell():
    """Update one cell in a table-doc row; the old value is kept in history."""
    import table_doc
    import table_type
    table_type.install()
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    row_id = os.environ.get("BEACON_ROW_ID", "")
    col_key = os.environ.get("BEACON_COL_KEY", "")
    value = os.environ.get("BEACON_VALUE", "")
    # AX review of PR #544: a missing <value> must NOT silently write an empty
    # string. BEACON_VALUE_SET (set by the dispatchers when a value was actually
    # provided — positional or --value) distinguishes "forgot the value" from
    # "explicitly set it to empty", mirroring the BEACON_TARGET_SET pattern.
    value_set = os.environ.get("BEACON_VALUE_SET", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not (doc_id and row_id and col_key):
        print("Error: doc-id, row-id, col-key すべて必須です", file=sys.stderr)
        sys.exit(1)
    if not value_set:
        print("Error: <value> が必要です (空にする場合は明示的に --value \"\" を渡す)",
              file=sys.stderr)
        sys.exit(1)
    if _refuse_if_bus_origin("doc_table_set_cell",
                             {"doc_id": doc_id, "row_id": row_id}):
        sys.exit(1)

    content, title, model = _load_table_model(doc_id)
    try:
        table_doc.set_cell(model, row_id, col_key, value,
                           actor=_actor_str(), at=_now_iso())
    except table_doc.TableDocError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _write_table_model(doc_id, title, content, model)

    # Return what was actually stored (and displaced) so an AI can read its own
    # write without a follow-up show (AX review of PR #544). The model recorded
    # both in the row's latest history entry.
    last = table_doc.get_row(model, row_id).get("history", [])[-1]
    if json_mode:
        print(json.dumps({"doc_id": doc_id, "row_id": row_id, "key": col_key,
                          "old_value": last.get("old"), "new_value": last.get("new")},
                         ensure_ascii=False))
    else:
        print(f"Set {row_id}.{col_key} in {doc_id}")


def cmd_doc_table_rm_row():
    """Soft-delete a row in a table-doc (tombstone; audit trail survives)."""
    import table_doc
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    row_id = os.environ.get("BEACON_ROW_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not (doc_id and row_id):
        print("Error: doc-id と row-id が必須です", file=sys.stderr)
        sys.exit(1)
    if _refuse_if_bus_origin("doc_table_rm_row",
                             {"doc_id": doc_id, "row_id": row_id}):
        sys.exit(1)

    content, title, model = _load_table_model(doc_id)
    try:
        table_doc.rm_row(model, row_id, actor=_actor_str(), at=_now_iso())
    except table_doc.TableDocError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _write_table_model(doc_id, title, content, model)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "row_id": row_id, "removed": True},
                         ensure_ascii=False))
    else:
        print(f"Removed row {row_id} from {doc_id}")


def cmd_doc_table_show():
    """Render a table-doc as a markdown table, or emit its model as JSON."""
    import table_doc
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not doc_id:
        print("Error: doc-id required", file=sys.stderr)
        sys.exit(1)
    _content, title, model = _load_table_model(doc_id)
    if json_mode:
        # ``rows`` is the active (non-tombstoned) view. Surface ``removed_count``
        # so an AI auditing table state can tell "N rows" from "had N+K, K
        # removed" instead of silently missing the deletions (AX review of #544).
        active = table_doc.active_rows(model)
        all_rows = model.get("rows", [])
        print(json.dumps({
            "doc_id": doc_id, "title": title,
            "columns": model.get("columns", []),
            "rows": active,
            "removed_count": len(all_rows) - len(active),
        }, ensure_ascii=False))
    else:
        print(f"# {title}\n")
        print(table_doc.render_table(model))


def cmd_doc_history():
    """Show revision history of a document."""
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    if not doc_id:
        print("Error: doc ID required")
        sys.exit(1)
    client, config = _get_api_client()
    project_id = config["project_id"]
    try:
        revs = client.get(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}/revisions")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    if not revs:
        print(f"No revisions found for '{doc_id}'")
        return
    for r in revs:
        print(f"  rev-{r['rev']}  {r['ts'][:10]}  {r.get('saved_by', '?')}")


def _rewrite_doc_frontmatter(fpath: str, *,
                              updates: Optional[dict] = None,
                              removes: Optional[list] = None) -> None:
    """Rewrite a doc's frontmatter in place.

    Reads the on-disk content, parses frontmatter via the existing
    parser, applies ``updates`` (key → str value) and ``removes`` (list
    of keys to drop), and writes back. Body is preserved exactly. Used
    by the doc soft-delete path (ms-14 e-973) so we don't bypass the
    canonical frontmatter shape.
    """
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, body = _parse_frontmatter(content)
    if removes:
        for k in removes:
            meta.pop(k, None)
    if updates:
        meta.update({k: str(v) for k, v in updates.items()})
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            quoted = ", ".join(f'"{item}"' for item in v)
            lines.append(f"{k}: [{quoted}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    new_content = "\n".join(lines) + body
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(new_content)


def cmd_doc_restore():
    """Restore a document to a historical revision.

    Requires ``--rev N`` (BEACON_REV). Trash-restore was removed; cancelled
    docs are reactivated via ``beacon doc update`` or equivalent edits.
    """
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    rev = os.environ.get("BEACON_REV", "")
    if not doc_id:
        print("Error: doc_id required", file=sys.stderr)
        sys.exit(1)
    if not rev:
        print("Usage: beacon doc restore <doc-id> --rev <N>", file=sys.stderr)
        sys.exit(1)
    _doc_restore_revision(doc_id, rev)


def _doc_restore_revision(doc_id: str, rev: str) -> None:
    """Restore a doc to a historical revision. Pre-e-973 behaviour."""
    client, config = _get_api_client()
    project_id = config["project_id"]
    try:
        rev_data = client.get(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}/revisions/{rev}")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    try:
        client.put_document(project_id, doc_id, rev_data["title"], rev_data["content"])
    except RuntimeError as e:
        print(f"Error restoring: {e}")
        sys.exit(1)
    print(f"Restored '{doc_id}' to rev-{rev}")


def cmd_doc_delete():
    """Soft-delete a document.

    Both modes set ``status: cancelled`` and stamp ``trashed_at`` /
    ``trashed_by`` / optional ``trash_reason``. Local rewrites the file
    frontmatter (ms-14 e-973); cloud calls the server's soft-delete
    endpoint and stores the same fields on the Firestore document
    (ms-14 e-991). Restore is a status flip in both modes — no version
    control undelete required.
    """
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.delete_document(config["project_id"], doc_id, reason=reason)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
        if json_mode:
            print(json.dumps(
                {"doc_id": doc_id, "status": "cancelled"},
                ensure_ascii=False,
            ))
        else:
            print(f"Trashed: {doc_id}")
            if reason:
                print(f"  Reason: {reason}")
        return

    docs_dir = _get_docs_dir()
    fpath = os.path.join(docs_dir, f"{doc_id}.md")
    if not os.path.exists(fpath):
        print(f"Document not found: {doc_id}")
        sys.exit(1)

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, _ = _parse_frontmatter(content)
    if meta.get("status") == "cancelled":
        print(f"Document {doc_id} is already in trash.")
        sys.exit(1)

    # ms-127 e-4838: use the module-level _now_iso binding (e-4320 patch契約に整合)
    # and the top-level `import core` for the operator identity (core._get_actor
    # returns 'claude'/email — distinct from _actor_str's machine/agent pair, so
    # it stays a core leaf call). The redundant `import core as _core` alias is gone.
    now_iso = _now_iso()
    actor = core._get_actor()
    updates = {"status": "cancelled", "trashed_at": now_iso, "trashed_by": actor}
    if reason:
        updates["trash_reason"] = reason
    # Drop any restored_* meta from a prior trash → restore cycle so the
    # only audit fields present reflect the current trash event.
    _rewrite_doc_frontmatter(
        fpath,
        updates=updates,
        removes=["restored_at", "restored_by", "restore_reason"],
    )
    _append_changelog({"op": "doc_delete", "doc_id": doc_id, "reason": reason})

    if json_mode:
        print(json.dumps(
            {"doc_id": doc_id, "status": "cancelled", "trashed_at": now_iso},
            ensure_ascii=False,
        ))
    else:
        print(f"Trashed: {doc_id}")
        if reason:
            print(f"  Reason: {reason}")


def cmd_doc_image_upload():
    """ms-43: SPEC / memo / retro 本文に貼る画像を 1 枚アップロードする。

    ローカルファイルパスを受け取って Beacon API にアップロードし、本文に
    貼り付け可能な markdown img tag (= ``![filename](url)``) を stdout に
    返す。AI / 人間ともに ``beacon doc image-upload <path>`` を叩いて URL
    を得てから ``beacon doc update <doc-id>`` で本文に取り込む使い方を
    想定。

    クラウドモード必須 (= 画像 hosting に GCS bucket を使う、ローカル
    モードでは UI render 経路が無い)。AWS S3 対応は別 task で拡張する。
    """
    local_path = os.environ.get("BEACON_DOC_IMAGE_PATH", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not local_path:
        print("Error: image file path required (set BEACON_DOC_IMAGE_PATH or pass path as argument)")
        sys.exit(1)
    if not os.path.exists(local_path):
        print(f"Error: file not found: {local_path}")
        sys.exit(1)

    # ms-84 Phase 2: 直接 _is_cloud_mode 呼び出しを Store 経由に統一 (= 受入条件
    # 10 の direct-call 削減)。 image upload は cloud-only operation のため、
    # ここでは mode guard として is_cloud() を確認するだけ。
    if not get_store().is_cloud():
        print("Error: image upload requires cloud mode "
              "(run 'beacon cloud upload-initial' first)")
        sys.exit(1)

    client, config = _get_api_client()
    try:
        result = client.upload_document_image(config["project_id"], local_path)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except ConnectionError as e:
        print(f"Network error: {e}")
        sys.exit(1)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result.get("markdown", ""))
        # ファイル sizeと URL も補助情報として stderr に出す (= stdout を
        # markdown 単独に保つことで、`beacon doc image-upload x.png` の出力を
        # そのまま doc 本文へ append できる)。
        size_kb = result.get("size", 0) / 1024
        sys.stderr.write(
            f"Uploaded: {result.get('content_type', '?')}, "
            f"{size_kb:.1f} KiB\n"
            f"URL: {result.get('url', '')}\n"
        )
