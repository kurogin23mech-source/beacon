"""Firestore Managed Export job.

Runs as a Cloud Run Job triggered daily by Cloud Scheduler.
Exports all Firestore collections to a GCS bucket with a date-stamped prefix.
Designed to be run as a one-shot container (Cloud Run Job).

Environment variables:
  GCP_PROJECT     - GCP project ID (default: beacon-cloud-96f5f)
  BACKUP_BUCKET   - GCS bucket for exports (default: beacon-firestore-backups)
"""

from __future__ import annotations

import datetime
import os
import sys

from google.cloud import firestore_admin_v1


GCP_PROJECT = os.environ.get("GCP_PROJECT", "beacon-cloud-96f5f")
BACKUP_BUCKET = os.environ.get("BACKUP_BUCKET", "beacon-firestore-backups")


def run_export() -> None:
    client = firestore_admin_v1.FirestoreAdminClient()

    # Date-stamped output prefix: gs://<bucket>/YYYY-MM-DD/
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    output_uri_prefix = f"gs://{BACKUP_BUCKET}/{today}"

    database_name = f"projects/{GCP_PROJECT}/databases/(default)"

    print(f"Starting Firestore export: project={GCP_PROJECT} → {output_uri_prefix}")

    operation = client.export_documents(
        request={
            "name": database_name,
            "output_uri_prefix": output_uri_prefix,
            # Export all collections (omit collection_ids to export everything)
        }
    )

    print("Export operation started, waiting for completion...")
    result = operation.result()  # blocks until done; Cloud Run Job timeout handles failure

    print(f"Export complete: output_uri_prefix={result.output_uri_prefix}")


if __name__ == "__main__":
    try:
        run_export()
    except Exception as exc:
        print(f"ERROR: Firestore export failed: {exc}", file=sys.stderr)
        sys.exit(1)
