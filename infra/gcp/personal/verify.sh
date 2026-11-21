#!/usr/bin/env bash
set -euo pipefail
set -a
. /opt/weaves/runtime.env
set +a
export WEAVES_E2E_API_BASE=http://localhost:8080/api/v0
# A short-lived break-glass account session; the token is never logged.
cd /opt/weaves/backend/infra/gcp/personal
compose=(docker compose --env-file /opt/weaves/runtime.env)
WEAVES_E2E_TOKEN="$("${compose[@]}" exec -T product-api python - \
  --email "$WEAVES_OWNER_EMAIL" --minutes 15 \
  < /opt/weaves/backend/infra/gcp/singlevm/issue_account_session.py)"
export WEAVES_E2E_TOKEN
WEAVES_E2E_ORGANIZATION_ID="$("${compose[@]}" exec -T postgres psql -U weaves -d weaves -t -A -c "SELECT org_id FROM product_records WHERE collection='memberships' ORDER BY created_at LIMIT 1")"
export WEAVES_E2E_ORGANIZATION_ID
python3 /opt/weaves/backend/scripts/e2e_smoke.py
status="$(curl --silent --output /dev/null --write-out '%{http_code}' http://localhost:8080/api/v0/skills)"
test "$status" = 401
# Verify artifact persistence directly: the retired /artifacts API is not exposed.
artifact_present="$("${compose[@]}" exec -T postgres psql -U weaves -d weaves -t -A -c "SELECT EXISTS (SELECT 1 FROM product_records a JOIN product_records turns ON a.payload->>'agent_run_id'=turns.payload->>'agent_run_id' JOIN product_records tasks ON turns.payload->>'task_id'=tasks.payload->>'task_id' WHERE a.collection='artifacts' AND turns.collection='task_turns' AND tasks.collection='tasks' AND tasks.payload->>'title'='Deployment verification' AND turns.payload->>'status'='succeeded')")"
test "$artifact_present" = t
printf 'PASS task artifact persisted in PostgreSQL\n'
/opt/weaves/backup.sh
"${compose[@]}" ps
printf '\nPASS authentication enforced, database backup created\n'
