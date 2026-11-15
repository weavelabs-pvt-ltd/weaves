#!/usr/bin/env bash
set -euo pipefail
exec gcloud compute ssh weaves-personal --project=weaves-510819 \
  --account=shivamsawarn15@gmail.com --zone=asia-south1-a --tunnel-through-iap \
  -- -N -L 127.0.0.1:18080:127.0.0.1:8080 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
