#!/usr/bin/env bash
# Fast local gate: run this before pushing. CI repeats these checks and also runs the slow real-stack suite on pull requests.
# Usage: scripts/check_local.sh            (lint, format, size, types, unit tests without PostgreSQL)
#        WEAVES_TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/db scripts/check_local.sh   (also the PostgreSQL tests)
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check --exclude .claude .
ruff format --check --exclude .claude .
python scripts/check_python_size.py
mypy
for file in tenancy_harness/*.cjs; do node --check "$file"; done
pytest -q -x
echo "Local checks passed. For harness or UI-facing changes also run: python -m tenancy_harness.run --backend . --frontend <weaves-app checkout at the pinned revision> --candidate backend"
