#!/usr/bin/env bash
# setup_log_sink.sh – Audit log long-term retention: Log Sink → Cloud Storage
#                      + Log Analytics (linked dataset) setup
#
# Spec (ms-14 Phase 2):
#   - Log Sink routes beacon.audit logs to a GCS bucket
#   - Log Analytics linked BigQuery dataset for dashboard queries
#   - Retention: GCS lifecycle policy applied separately (see backup/gcs_lifecycle.json)
#
# Usage:
#   GCP_PROJECT=beacon-cloud-96f5f ./setup_log_sink.sh

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-beacon-cloud-96f5f}"
REGION="${REGION:-asia-northeast1}"
LOG_BUCKET_NAME="beacon-audit-logs"
SINK_NAME="beacon-audit-sink"
# Log Analytics: linked BigQuery dataset
BQ_DATASET="beacon_audit_logs"
BQ_LOCATION="asia-northeast1"
# Log bucket for Log Analytics (Cloud Logging bucket, not GCS)
LOGGING_BUCKET_NAME="_Default"

echo "=== Audit Log Long-term Retention Setup ==="
echo "Project    : ${GCP_PROJECT}"
echo "GCS bucket : ${LOG_BUCKET_NAME}"
echo "Sink       : ${SINK_NAME}"
echo "BQ dataset : ${BQ_DATASET}"
echo ""

# ── 1. Create GCS bucket for audit log archives ───────────────────────────
echo "[1/6] Creating GCS bucket for audit logs (if not exists)..."
gsutil mb -p "${GCP_PROJECT}" -l "${REGION}" "gs://${LOG_BUCKET_NAME}" 2>/dev/null \
  && echo "  Created gs://${LOG_BUCKET_NAME}" \
  || echo "  Bucket already exists – skipping"

# Apply 90-day lifecycle (audit logs kept longer than backups)
echo "  Applying 90-day lifecycle policy to audit log bucket..."
gsutil lifecycle set /dev/stdin "gs://${LOG_BUCKET_NAME}" <<'EOF'
{
  "rule": [
    {
      "action": { "type": "Delete" },
      "condition": { "age": 90 }
    }
  ]
}
EOF

# ── 2. Create Log Sink (routes beacon.audit logs → GCS) ───────────────────
echo "[2/6] Creating log sink '${SINK_NAME}'..."
# Filter: only structured audit log lines emitted by AuditLogMiddleware
SINK_FILTER='jsonPayload.type="audit" OR logName:"beacon.audit"'

gcloud logging sinks create "${SINK_NAME}" \
  "storage.googleapis.com/${LOG_BUCKET_NAME}" \
  --project "${GCP_PROJECT}" \
  --log-filter="${SINK_FILTER}" \
  --quiet 2>/dev/null \
  && echo "  Sink created." \
  || {
    gcloud logging sinks update "${SINK_NAME}" \
      "storage.googleapis.com/${LOG_BUCKET_NAME}" \
      --project "${GCP_PROJECT}" \
      --log-filter="${SINK_FILTER}" \
      --quiet
    echo "  Sink updated."
  }

# ── 3. Grant sink service account write access to GCS bucket ──────────────
echo "[3/6] Granting sink service account write access to bucket..."
SINK_SA=$(gcloud logging sinks describe "${SINK_NAME}" \
  --project "${GCP_PROJECT}" \
  --format="value(writerIdentity)")
echo "  Sink service account: ${SINK_SA}"

gsutil iam ch "${SINK_SA}:roles/storage.objectCreator" "gs://${LOG_BUCKET_NAME}"
echo "  Permission granted."

# ── 4. Enable Log Analytics on the _Default logging bucket ────────────────
echo "[4/6] Enabling Log Analytics on _Default logging bucket..."
gcloud logging buckets update "${LOGGING_BUCKET_NAME}" \
  --project "${GCP_PROJECT}" \
  --location global \
  --enable-analytics \
  --quiet 2>/dev/null \
  && echo "  Log Analytics enabled on _Default bucket." \
  || echo "  _Default bucket already has Log Analytics or update not needed."

# ── 5. Create BigQuery linked dataset for Log Analytics ───────────────────
echo "[5/6] Creating BigQuery linked dataset '${BQ_DATASET}'..."
gcloud logging links create "${BQ_DATASET}" \
  --bucket "${LOGGING_BUCKET_NAME}" \
  --project "${GCP_PROJECT}" \
  --location global \
  --quiet 2>/dev/null \
  && echo "  BigQuery linked dataset created." \
  || echo "  Link already exists or Log Analytics not fully supported – skipping."

# ── 6. Print sample Log Analytics query ───────────────────────────────────
echo "[6/6] Sample Log Analytics query for audit dashboard:"
cat <<SQL

-- Audit events in the last 7 days (run in BigQuery or Log Analytics)
SELECT
  JSON_VALUE(json_payload, '\$.timestamp')  AS ts,
  JSON_VALUE(json_payload, '\$.action')     AS action,
  JSON_VALUE(json_payload, '\$.email')      AS email,
  JSON_VALUE(json_payload, '\$.project_id') AS project_id,
  JSON_VALUE(json_payload, '\$.ip')         AS ip,
  JSON_VALUE(json_payload, '\$.status')     AS status
FROM \`${GCP_PROJECT}.global._Default._AllLogs\`
WHERE
  JSON_VALUE(json_payload, '\$.type') = 'audit'
  AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY ts DESC
LIMIT 1000;

SQL

echo ""
echo "=== Log Sink setup complete ==="
echo "Audit logs are now routed to: gs://${LOG_BUCKET_NAME}/"
echo "Logs older than 90 days are automatically deleted by GCS lifecycle."
echo "Log Analytics queries can be run in BigQuery console:"
echo "  https://console.cloud.google.com/bigquery?project=${GCP_PROJECT}"
