# Code quality checks

Use Python 3.12 and install `requirements-dev.txt`. CI runs these checks before
building or deploying the backend:

```
ruff check .
ruff format --check .
python scripts/check_python_size.py
mypy
pytest
```

The type check covers `shared`, `contracts`, `infrastructure`, `clients`,
`modules`, `application` and `apps` in strict mode. PyYAML stubs are an explicit
development dependency. Existing Ruff compatibility exceptions are listed in
`pyproject.toml`; this cleanup did not add per-file exemptions or suppress type
errors. File limits remain 500 lines and function limits remain 80 lines.

For full PostgreSQL coverage, provide a disposable database connection through
`WEAVES_TEST_DATABASE_URL`. Each integration test creates and drops a unique
schema. CI supplies a PostgreSQL service and runs these tests automatically.
Without that variable, the PostgreSQL tests are skipped.

## Cleanup performed

- Typed the workflow studio, repository composition and worker
  dependencies; added a typed canvas step-progress contract with the same JSON
  field names used by existing records.
- Split workflow publication, execution and scheduling into bounded modules
  behind a typed shared context. Split provider persistence from the model
  service and reduced oversized model route registration.
- Matched the legacy execution delegate to the runtime skill-snapshot signature.
- Removed the reported type errors and code-size violations without changing
  the configured quality thresholds.
- Repaired stale fixtures left after Knowledge/Memory removal and updated the
  deployment smoke test to exercise the real task worker and transcript.
- Extended CI path filters to include scripts, infrastructure and Docker context
  changes, so edits to those files also trigger verification.

Frontend quality checks live in the sibling `weaves-app` repository. `npm run
check` runs ESLint with zero warnings and JavaScript syntax checks. CI enforces
both before the frontend container build. Browser globals are declared as an
explicit script-loading contract; obsolete renderers, unused local variables
and unnecessary catch bindings were removed.

## Verified cleanup results

- `ruff check .`: passed.
- `ruff format --check .`: passed.
- `python scripts/check_python_size.py`: passed at the existing 500/80 limits.
- `mypy`: passed, zero errors in 273 source files.
- Full pytest suite with PostgreSQL enabled: 284 passed, no skipped tests.
- Frontend `npm run check`: ESLint and syntax validation passed.
- `npm audit --audit-level=high`: zero reported vulnerabilities.
- Browser verification: 86 enabled cases passed across the full run and the
  real-backend Skills rerun. Two unrelated opt-in integration cases remained
  skipped. The initial Skills browser failures came from the isolated API not
  allowing the test frontend's alternate origin; after correcting that fixture
  environment, all four real Skills integration tests passed.

These results describe the current local source. They do not claim that every
future bug or possible refactoring has been eliminated, or that the existing
compatibility exceptions in the Ruff configuration have all been retired.

Legacy schema purge: migration 22 removes retired sign-in rows, indexes and the
SSO trigger. The reader-rollback compatibility set is gone, migration version
checks derive from the migration list, and tests pin the purge, the index
inventory and the collection registry so schema drift fails the suite. A
`vulture` pass at 80% confidence found only false positives and one unused
route parameter; lower-confidence findings were reviewed and the verified dead
items removed.

The production bootstrap change subsequently passed 288 tests with PostgreSQL
enabled, including the legacy owner-role upgrade and three clean-bootstrap
checks. Strict mypy still passes all 273 checked source files. The actual VM
also initialized all 18 migrations on a fresh PostgreSQL volume, preserved one
owner across API/worker restarts, and kept all work lists empty. Source-copy
and fresh-backup restore checks matched every application COPY row.
