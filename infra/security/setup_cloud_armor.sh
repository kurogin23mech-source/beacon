#!/usr/bin/env bash
# setup_cloud_armor.sh – Cloud Armor rate limiting for Beacon API
#
# Spec: Same IP, max 100 requests per 60 seconds.
# Required before public launch.
#
# Cloud Run on Google Cloud does NOT support Cloud Armor directly unless a
# Global HTTPS Load Balancer (with NEG backend) is in front of it.
# This script creates/updates the security policy.
# If a load balancer is not yet configured, run setup_load_balancer.sh first.
#
# Usage:
#   GCP_PROJECT=beacon-cloud-96f5f ./setup_cloud_armor.sh

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-beacon-cloud-96f5f}"
POLICY_NAME="beacon-api-rate-limit"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="beacon-api-prod"

echo "=== Cloud Armor Rate Limit Setup ==="
echo "Project : ${GCP_PROJECT}"
echo "Policy  : ${POLICY_NAME}"
echo "Limit   : 100 req/60s per IP"
echo ""

# ── 1. Create or update security policy ───────────────────────────────────
echo "[1/3] Creating security policy '${POLICY_NAME}'..."
gcloud compute security-policies create "${POLICY_NAME}" \
  --project "${GCP_PROJECT}" \
  --description "Rate limit: 100 req/60s per IP for Beacon API" \
  --quiet 2>/dev/null \
  || echo "  Policy already exists – will update rules."

# ── 2. Throttle rule: 100 req/min per IP ──────────────────────────────────
# Priority 1000 (lower number = higher priority than default allow rule 2147483647)
echo "[2/3] Applying throttle rule (100 req/60s per IP)..."
gcloud compute security-policies rules create 1000 \
  --security-policy "${POLICY_NAME}" \
  --project "${GCP_PROJECT}" \
  --expression "true" \
  --action "throttle" \
  --rate-limit-threshold-count 100 \
  --rate-limit-threshold-interval-sec 60 \
  --conform-action "allow" \
  --exceed-action "deny-429" \
  --enforce-on-key "IP" \
  --quiet 2>/dev/null \
  || gcloud compute security-policies rules update 1000 \
       --security-policy "${POLICY_NAME}" \
       --project "${GCP_PROJECT}" \
       --expression "true" \
       --action "throttle" \
       --rate-limit-threshold-count 100 \
       --rate-limit-threshold-interval-sec 60 \
       --conform-action "allow" \
       --exceed-action "deny-429" \
       --enforce-on-key "IP" \
       --quiet
echo "  Throttle rule applied."

# ── 3. Attach to load balancer backend service ────────────────────────────
# This step requires a HTTPS LB backend service named 'beacon-api-backend'.
# Skip with SKIP_ATTACH=1 if LB is not yet set up.
if [[ "${SKIP_ATTACH:-0}" == "1" ]]; then
  echo "[3/3] Skipping backend service attachment (SKIP_ATTACH=1)."
else
  echo "[3/3] Attaching security policy to backend service 'beacon-api-backend'..."
  gcloud compute backend-services update "beacon-api-backend" \
    --project "${GCP_PROJECT}" \
    --global \
    --security-policy "${POLICY_NAME}" \
    --quiet \
    && echo "  Attached to backend service." \
    || echo "  WARNING: Could not attach to 'beacon-api-backend'."
    echo "  If no load balancer exists yet, run: SKIP_ATTACH=1 ./setup_cloud_armor.sh"
    echo "  Then create the LB and attach manually:"
    echo "    gcloud compute backend-services update <BACKEND> --security-policy ${POLICY_NAME} --global"
fi

echo ""
echo "=== Cloud Armor setup complete ==="
echo "Policy '${POLICY_NAME}' is active."
echo "Verify with: gcloud compute security-policies describe ${POLICY_NAME} --project ${GCP_PROJECT}"
