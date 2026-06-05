"""One-shot migration: existing capabilities → ms-6 org-scoped schema.

ms-6 changes the capability id structure from `<author>/<name>@<version>`
to `<org>/<name>@<version>`. The existing MVP capabilities were pushed
under personal author slugs (`alice`, `kurogin`) which are not org slugs.

Strategy: **delete the old entries**, then re-push under the right org.
Rationale:

  - Only two known capabilities exist in production (alice/hello-skill,
    kurogin/win-hello) and both are MVP smoke-test data.
  - Renaming in place would require copying GCS objects and rewriting
    Firestore document IDs (the doc ID *is* the slug). A delete-and-redo
    keeps the migration code trivial.
  - Re-push lets each owner pick the correct org slug intentionally
    rather than auto-deriving one and locking in a wrong choice.

## Run

Set `BEACON_ENV=prod` (or leave dev). Then:

    cd server
    python trailnode_migrate_ms6.py --dry-run            # show what would happen
    python trailnode_migrate_ms6.py --delete             # actually delete

After deletion, the owners re-push from their own machines with the
right org slug (typically created via `trailnode org create` first).

## Safety

  - `--dry-run` is the default; nothing is deleted without `--delete`.
  - Each delete prints the capability id, Firestore slug, and GCS object
    key before removal so the operator can verify.
  - The script never touches `orgs` / `memberships` collections.
"""

from __future__ import annotations

import argparse
import os
import sys

# Capabilities that pre-date ms-6's org-scoped id structure. These were
# pushed when the first id segment was the uploader's username, not the
# org slug.
LEGACY_CAPABILITIES = [
    "alice/hello-skill@0.1.0",
    "kurogin/win-hello@0.1.0",
]


def _slug(capability_id: str) -> str:
    return capability_id.replace("/", "__").replace("@", "__")


def _parse_id(capability_id: str) -> tuple[str, str, str]:
    # Mirrors trailnode.py's _ID_PATTERN structure but kept inline so
    # this script has no app-side import dependency.
    first, rest = capability_id.split("/", 1)
    name, version = rest.split("@", 1)
    return first, name, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete (default is --dry-run).",
    )
    parser.add_argument(
        "--ids",
        nargs="*",
        default=LEGACY_CAPABILITIES,
        help="Override the list of capability ids to delete.",
    )
    args = parser.parse_args(argv)

    env = os.environ.get("BEACON_ENV", "dev")
    cap_collection = "capabilities" if env == "prod" else "capabilities-dev"
    bucket_name = os.environ.get("TRAILNODE_BUCKET", "trailnode-artifacts")
    gcs_prefix = f"{env}/"

    print(f"BEACON_ENV         = {env}")
    print(f"Firestore coll     = {cap_collection}")
    print(f"GCS bucket         = {bucket_name}")
    print(f"GCS prefix         = {gcs_prefix}")
    print(f"Dry-run            = {not args.delete}")
    print()

    from google.cloud import firestore as fs
    from google.cloud import storage as gcs

    db = fs.Client(project="beacon-cloud-96f5f")
    bucket = gcs.Client().bucket(bucket_name)

    deleted_any = False
    for cap_id in args.ids:
        slug = _slug(cap_id)
        first, name, version = _parse_id(cap_id)
        object_key = f"{gcs_prefix}{first}/{name}/{version}.tar.gz"

        print(f"--- {cap_id}")
        print(f"  Firestore: {cap_collection}/{slug}")
        print(f"  GCS:       gs://{bucket_name}/{object_key}")

        doc = db.collection(cap_collection).document(slug).get()
        if doc.exists:
            print("  [Firestore] exists")
            if args.delete:
                db.collection(cap_collection).document(slug).delete()
                print("  [Firestore] DELETED")
                deleted_any = True
        else:
            print("  [Firestore] not found (skip)")

        blob = bucket.blob(object_key)
        if blob.exists():
            print("  [GCS] exists")
            if args.delete:
                blob.delete()
                print("  [GCS] DELETED")
                deleted_any = True
        else:
            print("  [GCS] not found (skip)")

        print()

    if not args.delete:
        print("(dry-run — nothing was deleted; re-run with --delete to apply)")
    elif deleted_any:
        print("Done. Owners should re-push under the new <org>/<name>@<version> id.")
    else:
        print("Nothing to delete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
