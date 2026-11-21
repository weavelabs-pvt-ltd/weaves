#!/usr/bin/env bash
# Update an existing public deployment without recreating its database or owner.
set -euo pipefail
: "${BACKEND_IMAGE:?Supply the immutable backend image reference}"
: "${FRONTEND_IMAGE:?Supply the immutable frontend image reference}"
PROJECT_ID="${PROJECT_ID:-weaves-510819}"
ACCOUNT="${ACCOUNT:-shivamsawarn15@gmail.com}"
REGION="${REGION:-asia-south1}"
if [[ "$PROJECT_ID" == "weaves-510819" ]]; then
  printf 'This project now uses infra/gcp/singlevm/deploy.py; managed deployment is retired.\n' >&2
  exit 1
fi
APP_ORIGIN="${APP_ORIGIN:-https://app.weave-studio.in}"
WEBSITE_ORIGIN="${WEBSITE_ORIGIN:-https://weave-studio.in}"
API_ORIGIN="${API_ORIGIN:-https://api.weave-studio.in}"
flags=(--project "$PROJECT_ID" --account "$ACCOUNT" --region "$REGION" --quiet)

gcloud run deploy weaves-frontend "${flags[@]}" --image "$FRONTEND_IMAGE" \
  --port 8080 --cpu 1 --memory 256Mi --max 3 --max-instances 3 --min 0 \
  --no-traffic --tag public-release \
  --update-env-vars "^|^WEAVES_API_BASE=${API_ORIGIN}/api/v0|WEAVES_PUBLIC_REGISTRATION=false|WEAVES_APP_ORIGIN=${APP_ORIGIN}|WEAVES_WEBSITE_ORIGIN=${WEBSITE_ORIGIN}"
# Read the tag through JSON rather than relying on gcloud's list projections.
frontend_preview="$(gcloud run services describe weaves-frontend "${flags[@]}" --format=json | \
  python3 -c 'import json,sys; print(next(t["url"] for t in json.load(sys.stdin)["status"]["traffic"] if t.get("tag")=="public-release"))')"
frontend_url="$(gcloud run services describe weaves-frontend "${flags[@]}" --format='value(status.url)')"

gcloud run deploy weaves-api "${flags[@]}" --image "$BACKEND_IMAGE" \
  --port 8000 --cpu 1 --memory 1Gi --max 3 --max-instances 3 --min 0 \
  --concurrency 20 --timeout 900 --no-traffic --tag public-release \
  --update-env-vars "^|^WEAVES_PUBLIC_FRONTEND_URL=${APP_ORIGIN}|WEAVES_PUBLIC_API_BASE_URL=${API_ORIGIN}|CORS_ORIGINS=${APP_ORIGIN},${frontend_url},${frontend_preview}|WEAVES_DB_POOL_MAX_SIZE=4"

python3 "$(dirname "$0")/update_worker.py" --project "$PROJECT_ID" \
  --account "$ACCOUNT" --region "$REGION" --image "$BACKEND_IMAGE"

printf 'Staged both service revisions; public traffic has not been switched.\n'
printf 'Run the API and browser verification, then update traffic to the verified revisions.\n'
