#!/usr/bin/env bash
set -euo pipefail
cd /opt/weaves/backend/infra/gcp/personal
python3 configure.py
set -a
. /opt/weaves/runtime.env
set +a
compose=(docker compose --env-file /opt/weaves/runtime.env)
"${compose[@]}" build
WEAVES_ALLOW_ORGANIZATION_ONBOARDING=true "${compose[@]}" up -d --wait --wait-timeout 240 postgres product-api frontend
WEAVES_API_BASE=http://localhost:8080/api/v0 WEAVES_ONBOARDING_TOKEN="$WEAVES_ORGANIZATION_ONBOARDING_TOKEN" python3 /opt/weaves/backend/infra/gcp/bootstrap_owner.py
# Close the one-time onboarding gate before starting background execution.
WEAVES_ALLOW_ORGANIZATION_ONBOARDING=false "${compose[@]}" up -d --wait --wait-timeout 240
install -m 0700 backup.sh /opt/weaves/backup.sh
printf '15 20 * * * root /opt/weaves/backup.sh >> /var/log/weaves-backup.log 2>&1\n' > /etc/cron.d/weaves-backup
/opt/weaves/backup.sh
curl --fail --silent http://localhost:8080/api/v0/health
