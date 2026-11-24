#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/weaves/singlevm
install -d -m 0700 /opt/weaves/backups/singlevm
out="/opt/weaves/backups/singlevm/weaves-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
trap 'rm -f "$out.partial" "$out.tombstone-head.partial"' EXIT
# Record the tombstone log head BEFORE the dump so restore_reconcile.py can prove
# the log is not older than this backup. Best effort: without it a restore needs
# --no-head-check.
docker compose --env-file runtime.env exec -T product-api \
  python -m modules.recovery.tombstones head --dir /var/lib/weaves/tombstones \
  > "$out.tombstone-head.partial" || rm -f "$out.tombstone-head.partial"
docker compose --env-file runtime.env exec -T postgres \
  pg_dump --no-owner --no-acl -U weaves -d weaves | gzip > "$out.partial"
gzip -t "$out.partial"
mv "$out.partial" "$out"
if [ -s "$out.tombstone-head.partial" ]; then
  mv "$out.tombstone-head.partial" "$out.tombstone-head"
fi
find /opt/weaves/backups/singlevm -name 'weaves-*.sql.gz*' -mtime +7 -delete
