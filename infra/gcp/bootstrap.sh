#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-weaves-platform-prod-2026}"
REGION="${REGION:-asia-south1}"
AR_REPOSITORY="${AR_REPOSITORY:-weaves}"
SQL_INSTANCE="${SQL_INSTANCE:-weaves-db}"
WIF_POOL="${WIF_POOL:-weaves-github-pool}"
WIF_PROVIDER="${WIF_PROVIDER:-github-main}"
DEPLOYER_SA="${DEPLOYER_SA:-weaves-github-deploy}"
API_SA="${API_SA:-weaves-api}"
WORKER_SA="${WORKER_SA:-weaves-worker}"
FRONTEND_SA="${FRONTEND_SA:-weaves-frontend}"
GCLOUD="${GCLOUD:-$(command -v gcloud || true)}"
if [[ -z "$GCLOUD" && -x /opt/homebrew/share/google-cloud-sdk/bin/gcloud ]]; then
  GCLOUD=/opt/homebrew/share/google-cloud-sdk/bin/gcloud
fi
if [[ -z "$GCLOUD" ]]; then
  echo "gcloud CLI was not found" >&2
  exit 1
fi

BACKEND_REPO="weavelabs-pvt-ltd/weaves"
FRONTEND_REPO="weavelabs-pvt-ltd/weaves-app"
SA_DOMAIN="${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_EMAIL="${DEPLOYER_SA}@${SA_DOMAIN}"
API_EMAIL="${API_SA}@${SA_DOMAIN}"
WORKER_EMAIL="${WORKER_SA}@${SA_DOMAIN}"
FRONTEND_EMAIL="${FRONTEND_SA}@${SA_DOMAIN}"

secrets=(
  weaves-db-root-password
  weaves-db-password
  weaves-database-url
  weaves-fernet-key
)

ensure_secret() {
  local name="$1"
  local value="$2"
  if ! "$GCLOUD" secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    "$GCLOUD" secrets create "$name" \
      --project="$PROJECT_ID" \
      --replication-policy=automatic \
      --quiet >/dev/null
    printf '%s' "$value" | "$GCLOUD" secrets versions add "$name" \
      --project="$PROJECT_ID" --data-file=- >/dev/null
  fi
}

ensure_service_account() {
  local account="$1"
  local display_name="$2"
  local account_email="${account}@${SA_DOMAIN}"
  if ! "$GCLOUD" iam service-accounts describe "$account_email" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
    "$GCLOUD" iam service-accounts create "$account" \
      --project="$PROJECT_ID" --display-name="$display_name" --quiet >/dev/null
  fi
}

grant_project_role() {
  local member="$1"
  local role="$2"
  "$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$member" --role="$role" --condition=None --quiet >/dev/null
}

grant_secret_access() {
  local name="$1"
  local member="$2"
  "$GCLOUD" secrets add-iam-policy-binding "$name" \
    --project="$PROJECT_ID" \
    --member="$member" \
    --role=roles/secretmanager.secretAccessor \
    --quiet >/dev/null
}

"$GCLOUD" config set project "$PROJECT_ID" --quiet >/dev/null
"$GCLOUD" services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com \
  dns.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="$PROJECT_ID" --quiet

if ! "$GCLOUD" artifacts repositories describe "$AR_REPOSITORY" \
  --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  "$GCLOUD" artifacts repositories create "$AR_REPOSITORY" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --repository-format=docker \
    --description="Weaves production container images" \
    --quiet >/dev/null
fi

if ! "$GCLOUD" secrets describe weaves-db-root-password \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  root_password="$(openssl rand -hex 32)"
  ensure_secret weaves-db-root-password "$root_password"
else
  root_password="$("$GCLOUD" secrets versions access latest \
    --secret=weaves-db-root-password --project="$PROJECT_ID")"
fi

if ! "$GCLOUD" sql instances describe "$SQL_INSTANCE" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  "$GCLOUD" sql instances create "$SQL_INSTANCE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --database-version=POSTGRES_16 \
    --edition=ENTERPRISE \
    --cpu=1 \
    --memory=3840MiB \
    --storage-type=SSD \
    --storage-size=20 \
    --storage-auto-increase \
    --storage-auto-increase-limit=100 \
    --availability-type=REGIONAL \
    --backup-start-time=18:00 \
    --retained-backups-count=14 \
    --enable-point-in-time-recovery \
    --retained-transaction-log-days=7 \
    --require-ssl \
    --deletion-protection \
    --root-password="$root_password" \
    --quiet
else
  "$GCLOUD" sql users set-password postgres \
    --project="$PROJECT_ID" --instance="$SQL_INSTANCE" \
    --password="$root_password" --quiet >/dev/null
fi

if ! "$GCLOUD" secrets describe weaves-db-password \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  database_password="$(openssl rand -hex 32)"
  ensure_secret weaves-db-password "$database_password"
else
  database_password="$("$GCLOUD" secrets versions access latest \
    --secret=weaves-db-password --project="$PROJECT_ID")"
fi

if ! "$GCLOUD" sql users list --project="$PROJECT_ID" --instance="$SQL_INSTANCE" \
  --format='value(name)' | rg -qx 'weaves'; then
  "$GCLOUD" sql users create weaves \
    --project="$PROJECT_ID" --instance="$SQL_INSTANCE" \
    --password="$database_password" --quiet >/dev/null
fi
if ! "$GCLOUD" sql databases describe weaves \
  --project="$PROJECT_ID" --instance="$SQL_INSTANCE" >/dev/null 2>&1; then
  "$GCLOUD" sql databases create weaves \
    --project="$PROJECT_ID" --instance="$SQL_INSTANCE" --quiet >/dev/null
fi

connection_name="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
database_url="postgresql://weaves:${database_password}@/weaves?host=/cloudsql/${connection_name}"
ensure_secret weaves-database-url "$database_url"

if ! "$GCLOUD" secrets describe weaves-fernet-key \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  fernet_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  ensure_secret weaves-fernet-key "$fernet_key"
fi

ensure_service_account "$DEPLOYER_SA" "Weaves production GitHub deployer"
ensure_service_account "$API_SA" "Weaves production API runtime"
ensure_service_account "$WORKER_SA" "Weaves production worker runtime"
ensure_service_account "$FRONTEND_SA" "Weaves production frontend runtime"

grant_project_role "serviceAccount:${DEPLOYER_EMAIL}" roles/run.admin
grant_project_role "serviceAccount:${DEPLOYER_EMAIL}" roles/serviceusage.serviceUsageConsumer
grant_project_role "serviceAccount:${API_EMAIL}" roles/cloudsql.client
grant_project_role "serviceAccount:${WORKER_EMAIL}" roles/cloudsql.client

"$GCLOUD" artifacts repositories add-iam-policy-binding "$AR_REPOSITORY" \
  --project="$PROJECT_ID" --location="$REGION" \
  --member="serviceAccount:${DEPLOYER_EMAIL}" \
  --role=roles/artifactregistry.writer --quiet >/dev/null

for runtime_email in "$API_EMAIL" "$WORKER_EMAIL" "$FRONTEND_EMAIL"; do
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$runtime_email" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role=roles/iam.serviceAccountUser \
    --quiet >/dev/null
done

for secret_name in \
  weaves-database-url \
  weaves-fernet-key; do
  grant_secret_access "$secret_name" "serviceAccount:${API_EMAIL}"
  grant_secret_access "$secret_name" "serviceAccount:${WORKER_EMAIL}"
done

if ! "$GCLOUD" iam workload-identity-pools describe "$WIF_POOL" \
  --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  "$GCLOUD" iam workload-identity-pools create "$WIF_POOL" \
    --project="$PROJECT_ID" --location=global \
    --display-name="Weaves GitHub Actions production" --quiet >/dev/null
fi
if ! "$GCLOUD" iam workload-identity-pools providers describe "$WIF_PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$WIF_POOL" \
  >/dev/null 2>&1; then
  "$GCLOUD" iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
    --project="$PROJECT_ID" \
    --location=global \
    --workload-identity-pool="$WIF_POOL" \
    --display-name="GitHub Weaves repositories" \
    --issuer-uri=https://token.actions.githubusercontent.com \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository_owner == 'weavelabs-pvt-ltd' && assertion.ref == 'refs/heads/main'" \
    --quiet >/dev/null
else
  "$GCLOUD" iam workload-identity-pools providers update-oidc "$WIF_PROVIDER" \
    --project="$PROJECT_ID" \
    --location=global \
    --workload-identity-pool="$WIF_POOL" \
    --display-name="GitHub Weaves repositories" \
    --issuer-uri=https://token.actions.githubusercontent.com \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository_owner == 'weavelabs-pvt-ltd' && assertion.ref == 'refs/heads/main'" \
    --quiet >/dev/null
fi

project_number="$("$GCLOUD" projects describe "$PROJECT_ID" --format='value(projectNumber)')"
provider_resource="projects/${project_number}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"
for repository in "$BACKEND_REPO" "$FRONTEND_REPO"; do
  principal="principalSet://iam.googleapis.com/projects/${project_number}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${repository}"
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$DEPLOYER_EMAIL" \
    --project="$PROJECT_ID" \
    --member="$principal" \
    --role=roles/iam.workloadIdentityUser \
    --quiet >/dev/null
done

for repository in "$BACKEND_REPO" "$FRONTEND_REPO"; do
  gh variable set GCP_PROJECT_ID --repo "$repository" --body "$PROJECT_ID"
  gh variable set GCP_REGION --repo "$repository" --body "$REGION"
  gh variable set GCP_ARTIFACT_REPOSITORY --repo "$repository" --body "$AR_REPOSITORY"
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$repository" --body "$provider_resource"
  gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --repo "$repository" --body "$DEPLOYER_EMAIL"
done

printf 'Production project resources and GitHub OIDC are prepared.\n'
printf 'Region: %s\nArtifact Registry: %s-docker.pkg.dev/%s/%s\n' \
  "$REGION" "$REGION" "$PROJECT_ID" "$AR_REPOSITORY"
