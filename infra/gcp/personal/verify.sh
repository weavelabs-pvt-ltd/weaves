#!/usr/bin/env bash
set -euo pipefail
set -a
. /opt/weaves/runtime.env
set +a
export WEAVES_E2E_API_BASE=http://localhost:8080/api/v0
export WEAVES_E2E_EMAIL="$WEAVES_OWNER_EMAIL"
export WEAVES_E2E_PASSWORD="$WEAVES_OWNER_PASSWORD"
python3 /opt/weaves/backend/scripts/e2e_smoke.py
status="$(curl --silent --output /dev/null --write-out '%{http_code}' http://localhost:8080/api/v0/skills)"
test "$status" = 401
cd /opt/weaves/backend/infra/gcp/personal
compose=(docker compose --env-file /opt/weaves/runtime.env)
test "$("${compose[@]}" exec -T product-api python -c 'import os; print(os.environ["WEAVES_ALLOW_ORGANIZATION_ONBOARDING"])')" = false
# Verify artifact persistence directly: the retired /artifacts API is not exposed.
artifact_present="$("${compose[@]}" exec -T postgres psql -U weaves -d weaves -t -A -c "SELECT EXISTS (SELECT 1 FROM product_records a JOIN product_records turns ON a.payload->>'agent_run_id'=turns.payload->>'agent_run_id' JOIN product_records tasks ON turns.payload->>'task_id'=tasks.payload->>'task_id' WHERE a.collection='artifacts' AND turns.collection='task_turns' AND tasks.collection='tasks' AND tasks.payload->>'title'='Deployment verification' AND turns.payload->>'status'='succeeded')")"
test "$artifact_present" = t
printf 'PASS task artifact persisted in PostgreSQL\n'
/opt/weaves/backup.sh
"${compose[@]}" ps
printf '\nPASS authentication enforced, onboarding disabled, database backup created\n'
