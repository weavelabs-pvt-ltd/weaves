#!/usr/bin/env bash
# Personal project only: no shared repository variables or production resources.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-weaves-510819}"
ACCOUNT="${ACCOUNT:-shivamsawarn15@gmail.com}"
ZONE="${ZONE:-asia-south1-a}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
flags=(--project "$PROJECT_ID" --account "$ACCOUNT" --quiet)
gcloud compute networks describe weaves-personal "${flags[@]}" >/dev/null 2>&1 || \
  gcloud compute networks create weaves-personal --subnet-mode=custom "${flags[@]}"
gcloud compute networks subnets describe weaves-personal --region asia-south1 "${flags[@]}" >/dev/null 2>&1 || \
  gcloud compute networks subnets create weaves-personal --network weaves-personal --range 10.73.0.0/24 --region asia-south1 "${flags[@]}"
gcloud compute firewall-rules describe weaves-personal-iap-ssh "${flags[@]}" >/dev/null 2>&1 || \
  gcloud compute firewall-rules create weaves-personal-iap-ssh --network weaves-personal \
    --direction INGRESS --action ALLOW --rules tcp:22 --source-ranges 35.235.240.0/20 \
    --target-tags weaves-personal "${flags[@]}"
gcloud compute instances describe weaves-personal --zone "$ZONE" "${flags[@]}" >/dev/null 2>&1 || \
  gcloud compute instances create weaves-personal --zone "$ZONE" --machine-type e2-medium \
    --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB \
    --boot-disk-type pd-balanced --no-boot-disk-auto-delete --network weaves-personal \
    --subnet weaves-personal --tags weaves-personal --no-service-account --no-scopes \
    --metadata enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
    --metadata-from-file "startup-script=$ROOT/startup.sh" "${flags[@]}"
gcloud compute resource-policies describe weaves-personal-daily --region asia-south1 "${flags[@]}" >/dev/null 2>&1 || \
  gcloud compute resource-policies create snapshot-schedule weaves-personal-daily \
    --region asia-south1 --daily-schedule --start-time 20:00 --max-retention-days 7 \
    --on-source-disk-delete keep-auto-snapshots "${flags[@]}"
gcloud compute disks add-resource-policies weaves-personal --zone "$ZONE" \
  --resource-policies weaves-personal-daily "${flags[@]}"
