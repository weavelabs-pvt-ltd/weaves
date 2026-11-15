#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/weaves/backend/infra/gcp/personal
out="/opt/weaves/backups/weaves-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
trap 'rm -f "$out.partial"' EXIT
docker compose --env-file /opt/weaves/runtime.env exec -T postgres pg_dump -U weaves -d weaves | gzip > "$out.partial"
gzip -t "$out.partial"
mv "$out.partial" "$out"
find /opt/weaves/backups -name 'weaves-*.sql.gz' -mtime +7 -delete
