#!/usr/bin/env bash
# Restore a backup into a disposable database and compare every COPY row.
set -euo pipefail
umask 077
: "${1:?Supply the gzip SQL backup to verify}"
cd /opt/weaves/singlevm
compose=(docker compose --env-file runtime.env)
verification_db="restore_check_$(date -u +%Y%m%d%H%M%S)"
"${compose[@]}" exec -T postgres createdb -U weaves "$verification_db"
trap '"${compose[@]}" exec -T postgres dropdb -U weaves "$verification_db"' EXIT
gzip -dc "$1" | "${compose[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U weaves -d "$verification_db" > restore-check.log 2>&1
python3 verify_restore.py "$1" --database "$verification_db"
