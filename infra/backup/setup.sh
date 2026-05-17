#!/usr/bin/env bash
# setup.sh – provision Firestore backup infrastructure
#
# What this does:
#   1. Creates a GCS bucket for Firestore exports (if not exists)
#   2. Applies 30-day lifecycle policy for automatic snapshot deletion
#   3. Grants the Cloud Run service account write access to the bucket
#   4. Grants the service account the Firestore export IAM role
#   5. Builds and pushes the backup Cloud Run Job image
#   6. Creates (or updates) the Cloud Run Job
#   7. Creates a Cloud Scheduler job to trigger daily at 02:00 JST (17:00 UTC)
#
# Usage:
#   GCP_PROJECT=beacon-cloud-96f5f ./setup.sh
#   Or just: ./setup.sh  (defaults below are used)

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-beacon-cloud-96f5f}"
REGION="${REGION:-asia-northeast1}"
BACKUP_BUCKET="${BACKUP_BUCKET:-beacon-firestore-backups}"
JOB_NAME="beacon-firestore-backup"
IMAGE="gcr.io/${GCP_PROJECT}/${JOB_NAME}"
# Cloud Run service account (from beacon-api-prod)
SERVICE_ACCOUNT="192136550838-compute@developer.gserviceaccount.com"
# Scheduler service account (Cloud Scheduler invokes Cloud Run Jobs via HTTP)
SCHEDULER_SA="${SERVICE_ACCOUNT}"

echo "=== Beacon Firestore Backup Setup ==="
echo "Project : ${GCP_PROJECT}"
echo "Region  : ${REGION}"
echo "Bucket  : ${BACKUP_BUCKET}"
echo ""

# ── 1. Create GCS bucket ───────────────────────────────────────────────────
echo "[1/7] Creating GCS bucket (if not exists)..."
gsutil mb -p "${GCP_PROJECT}" -l "${REGION}" "gs://${BACKUP_BUCKET}" 2>/dev/null \
  && echo "  Created gs://${BACKUP_BUCKET}" \
  || echo "  Bucket already exists – skipping"

# ── 2. Apply 30-day lifecycle policy ──────────────────────────────────────
echo "[2/7] Applying 30-day lifecycle policy..."
gsutil lifecycle set "$(dirname "$0")/gcs_lifecycle.json" "gs://${BACKUP_BUCKET}"

# ── 3. Grant service account write access to bucket ───────────────────────
echo "[3/7] Granting Storage Object Admin to service account..."
gsutil iam ch \
  "serviceAccount:${SERVICE_ACCOUNT}:roles/storage.objectAdmin" \
  "gs://${BACKUP_BUCKET}"

# ── 4. Grant Firestore export IAM role ────────────────────────────────────
echo "[4/7] Granting datastore.importExportAdmin to service account..."
gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/datastore.importExportAdmin" \
  --condition=None \
  --quiet

# ── 5. Build and push Docker image ────────────────────────────────────────
echo "[5/7] Building and pushing backup job image..."
gcloud builds submit "$(dirname "$0")" \
  --tag "${IMAGE}" \
  --project "${GCP_PROJECT}"

# ── 6. Create / update Cloud Run Job ──────────────────────────────────────
echo "[6/7] Creating/updating Cloud Run Job '${JOB_NAME}'..."
gcloud run jobs create "${JOB_NAME}" \
  --image "${IMAGE}" \
  --project "${GCP_PROJECT}" \
  --region "${REGION}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --set-env-vars "GCP_PROJECT=${GCP_PROJECT},BACKUP_BUCKET=${BACKUP_BUCKET}" \
  --max-retries 2 \
  --task-timeout 1800 \
  --quiet 2>/dev/null \
  || gcloud run jobs update "${JOB_NAME}" \
       --image "${IMAGE}" \
       --project "${GCP_PROJECT}" \
       --region "${REGION}" \
       --service-account "${SERVICE_ACCOUNT}" \
       --set-env-vars "GCP_PROJECT=${GCP_PROJECT},BACKUP_BUCKET=${BACKUP_BUCKET}" \
       --max-retries 2 \
       --task-timeout 1800 \
       --quiet
echo "  Cloud Run Job ready."

# ── 7. Create Cloud Scheduler job ─────────────────────────────────────────
echo "[7/7] Creating Cloud Scheduler job (daily 02:00 JST = 17:00 UTC)..."
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT}/jobs/${JOB_NAME}:run"

gcloud scheduler jobs create http "beacon-firestore-backup-daily" \
  --project "${GCP_PROJECT}" \
  --location "${REGION}" \
  --schedule "0 17 * * *" \
  --uri "${JOB_URI}" \
  --http-method POST \
  --oauth-service-account-email "${SCHEDULER_SA}" \
  --time-zone "UTC" \
  --quiet 2>/dev/null \
  || gcloud scheduler jobs update http "beacon-firestore-backup-daily" \
       --project "${GCP_PROJECT}" \
       --location "${REGION}" \
       --schedule "0 17 * * *" \
       --uri "${JOB_URI}" \
       --http-method POST \
       --oauth-service-account-email "${SCHEDULER_SA}" \
       --time-zone "UTC" \
       --quiet
echo "  Scheduler job created/updated."

echo ""
echo "=== Setup complete ==="
echo "Exports will appear in: gs://${BACKUP_BUCKET}/YYYY-MM-DD/"
echo "Snapshots older than 30 days are deleted automatically by GCS lifecycle."
