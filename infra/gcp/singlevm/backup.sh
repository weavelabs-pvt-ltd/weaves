#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/weaves/singlevm
install -d -m 0700 /opt/weaves/backups/singlevm
out="/opt/weaves/backups/singlevm/weaves-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
trap 'rm -f "$out.partial"' EXIT
docker compose --env-file runtime.env exec -T postgres \
  pg_dump --no-owner --no-acl -U weaves -d weaves | gzip > "$out.partial"
gzip -t "$out.partial"
mv "$out.partial" "$out"
find /opt/weaves/backups/singlevm -name 'weaves-*.sql.gz' -mtime +7 -delete
