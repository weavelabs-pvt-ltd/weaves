#!/usr/bin/env bash
# Point this clone at the repository's hooks so the fast local checks run before every push.
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "Pre-push hook installed (checks: scripts/check_local.sh). Remove with: git config --unset core.hooksPath"
