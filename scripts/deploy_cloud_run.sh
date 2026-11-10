#!/usr/bin/env bash
# Programmatic Cloud Run deployment using `adk deploy cloud_run` with Cloud Trace
# enabled and zero hardcoded credentials.
#
# CLOUD_RUN_REGION is where the service runs. VERTEX_AI_LOCATION is the Gemini
# endpoint: `adk deploy cloud_run` bakes the Cloud Run region into
# GOOGLE_CLOUD_LOCATION, so the app applies VERTEX_AI_LOCATION on startup
# (Gemini 3.x models are only served from the `global` endpoint).
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT before deploying}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
VERTEX_AI_LOCATION="${VERTEX_AI_LOCATION:-global}"
SERVICE_NAME="${SERVICE_NAME:-trippilot-travel-concierge}"
ADK_BIN="${ADK_BIN:-adk}"

if [[ -x ".venv/bin/adk" ]]; then
  ADK_BIN=".venv/bin/adk"
fi

echo "Deploying ${SERVICE_NAME} to project=${GOOGLE_CLOUD_PROJECT} region=${CLOUD_RUN_REGION} (Gemini endpoint: ${VERTEX_AI_LOCATION})..."

"${ADK_BIN}" deploy cloud_run \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${CLOUD_RUN_REGION}" \
  --service_name="${SERVICE_NAME}" \
  --app_name="travel_concierge" \
  --env "VERTEX_AI_LOCATION=${VERTEX_AI_LOCATION}" \
  --trace_to_cloud \
  ./travel_concierge
