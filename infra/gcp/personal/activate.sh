#!/usr/bin/env bash
set -euo pipefail
cd /opt/weaves/backend/infra/gcp/personal
python3 configure.py
set -a
. /opt/weaves/runtime.env
set +a
compose=(docker compose --env-file /opt/weaves/runtime.env)
"${compose[@]}" build
"${compose[@]}" up -d --wait --wait-timeout 240 postgres product-api frontend
# Create (idempotently) the account owner with database owner access; no secret is printed.
"${compose[@]}" exec -T product-api python - \
  --email "$WEAVES_OWNER_EMAIL" --organization-name "$WEAVES_OWNER_ORGANIZATION" \
  < /opt/weaves/backend/infra/gcp/singlevm/bootstrap_account_owner.py
"${compose[@]}" up -d --wait --wait-timeout 240
install -m 0700 backup.sh /opt/weaves/backup.sh
printf '15 20 * * * root /opt/weaves/backup.sh >> /var/log/weaves-backup.log 2>&1\n' > /etc/cron.d/weaves-backup
/opt/weaves/backup.sh
curl --fail --silent http://localhost:8080/api/v0/health
