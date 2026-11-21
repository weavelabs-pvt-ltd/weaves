#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-weaves-platform-prod-2026}"
REGION="${REGION:-asia-south1}"
AR_REPOSITORY="${AR_REPOSITORY:-weaves}"
SQL_INSTANCE="${SQL_INSTANCE:-weaves-db}"
API_SERVICE="weaves-api"
FRONTEND_SERVICE="weaves-frontend"
WORKER_POOL="weaves-worker"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_IMAGE="${BACKEND_IMAGE:-$(git -C "$ROOT" rev-parse --short=12 HEAD)}"
FRONTEND_DIR="${FRONTEND_DIR:-../weaves-app}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-$(git -C "$FRONTEND_DIR" rev-parse --short=12 HEAD)}"
GCLOUD="${GCLOUD:-$(command -v gcloud || true)}"
if [[ -z "$GCLOUD" && -x /opt/homebrew/share/google-cloud-sdk/bin/gcloud ]]; then
  GCLOUD=/opt/homebrew/share/google-cloud-sdk/bin/gcloud
fi
if [[ -z "$GCLOUD" ]]; then
  echo "gcloud CLI was not found" >&2
  exit 1
fi

registry="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}"
backend_ref="${registry}/backend:${BACKEND_IMAGE}"
frontend_ref="${registry}/frontend:${FRONTEND_IMAGE}"
connection_name="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

"$GCLOUD" auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker buildx build --platform=linux/amd64 --no-cache --provenance=false --push \
  -t "$backend_ref" "$ROOT"
docker buildx build --platform=linux/amd64 --no-cache --provenance=false --push \
  -t "$frontend_ref" "$FRONTEND_DIR"

"$GCLOUD" run deploy "$FRONTEND_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --image "$frontend_ref" \
  --port 8080 --cpu 1 --memory 256Mi --max 3 --no-invoker-iam-check \
  --service-account "weaves-frontend@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "^|^WEAVES_API_BASE=/api/v0|WEAVES_PUBLIC_REGISTRATION=false"
frontend_url="$("$GCLOUD" run services describe "$FRONTEND_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"

"$GCLOUD" run deploy "$API_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --image "$backend_ref" \
  --port 8000 --cpu 1 --memory 1Gi --max 3 --concurrency 20 --timeout 900 \
  --no-invoker-iam-check \
  --service-account "weaves-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --add-cloudsql-instances "$connection_name" \
  --set-secrets "DATABASE_URL=weaves-database-url:1,WEAVES_SECRET_ENCRYPTION_KEY=weaves-fernet-key:1" \
  --set-env-vars "CORS_ORIGINS=${frontend_url}"
api_url="$("$GCLOUD" run services describe "$API_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
"$GCLOUD" run services update "$FRONTEND_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --update-env-vars "^|^WEAVES_API_BASE=${api_url}/api/v0"

# The first account owner is created out of band with
# infra/gcp/singlevm/bootstrap_account_owner.py from a host that has
# database owner access; there is no public signup or password login.

"$GCLOUD" run worker-pools deploy "$WORKER_POOL" \
  --project "$PROJECT_ID" --region "$REGION" --image "$backend_ref" \
  --instances 1 --cpu 1 --memory 512Mi \
  --service-account "weaves-worker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --command python --args=-m,apps.worker.main \
  --add-cloudsql-instances "$connection_name" \
  --set-secrets "DATABASE_URL=weaves-database-url:1,WEAVES_SECRET_ENCRYPTION_KEY=weaves-fernet-key:1"

if [ -n "${WEAVES_E2E_TOKEN:-}" ]; then
  export WEAVES_E2E_API_BASE="${api_url}/api/v0"
  python3 "$ROOT/scripts/e2e_smoke.py"
fi

printf 'Production is deployed.\nFrontend: %s\nAPI: %s\n' "$frontend_url" "$api_url"
