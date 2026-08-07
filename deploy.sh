#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Gmail MCP — Cloud Run Deploy Script
# ============================================================
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Project set: gcloud config set project YOUR_PROJECT_ID
#   - GMAIL_CLIENT_ID env var set (Google OAuth client id)
#   - token_cache.json seeded with a valid refresh token for non-interactive deploys
# ============================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-gmail-mcp}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: No Google Cloud project set."
  echo "  Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

if [ -z "${GMAIL_CLIENT_ID:-}" ]; then
  echo "ERROR: GMAIL_CLIENT_ID is not set."
  echo "  Export it before running: export GMAIL_CLIENT_ID=xxxx.apps.googleusercontent.com"
  exit 1
fi

if [ -z "${GMAIL_CLIENT_SECRET:-}" ]; then
  echo "WARNING: GMAIL_CLIENT_SECRET is not set. Token refresh in the container "
  echo "  may fail. Export it before running to be safe:"
  echo "  export GMAIL_CLIENT_SECRET=xxxx"
fi

echo "=== Deploying $SERVICE_NAME to $PROJECT_ID ($REGION) ==="
echo "Optimized for Google Cloud Free Tier:"
echo "  - 256Mi memory (within 360K GB-seconds free)"
echo "  - 1 max instance (deterministic scheduled sends)"
echo ""

EXTRA_ENV=""
if [ -n "${GMAIL_CLIENT_SECRET:-}" ]; then
  EXTRA_ENV="${EXTRA_ENV},GMAIL_CLIENT_SECRET=${GMAIL_CLIENT_SECRET}"
fi
if [ -n "${GMAIL_FULL_ACCESS:-}" ]; then
  EXTRA_ENV="${EXTRA_ENV},GMAIL_FULL_ACCESS=${GMAIL_FULL_ACCESS}"
fi

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --memory 256Mi \
  --cpu 1 \
  --concurrency 10 \
  --min-instances 0 \
  --max-instances 1 \
  --timeout 300 \
  --set-env-vars "GMAIL_CLIENT_ID=${GMAIL_CLIENT_ID}${EXTRA_ENV}"

echo ""
echo "=== Deployment complete ==="
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format 'value(status.url)')
echo "Service URL: $SERVICE_URL"
echo "MCP endpoint: $SERVICE_URL/mcp"
