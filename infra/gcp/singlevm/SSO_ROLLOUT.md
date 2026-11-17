# SSO compatibility rollout

The enterprise SSO foundation must be released after its compatibility baseline.
The baseline knows migration 19 is additive, but only applies versions 1–18.
It can read both explicit session channels and the legacy session shape. Browser
records remain explicit and cannot authenticate as bearer sessions. Ordinary
bearer writes omit their default channel so the baseline itself can still roll
back to the earlier production image.

1. Test and deploy the compatibility baseline from main. Check its workflow,
   running image/revision, health and existing owner login. Production must still
   have schema 18 at this point. Preserve the database and encryption key.
2. Against a disposable PostgreSQL instance, apply the exact migration 19 from
   the SSO release, write both bearer and browser sessions, then restart using
   the baseline image. Verify session reads, bearer rejection of browser tokens,
   revocation and unchanged row counts. Unknown migrations above 19 still fail.
3. Merge and automatically deploy the SSO foundation only after those checks.
   Its migration 19 adds identity indexes and an update guard for a new
   collection. It does not rewrite existing application records.
4. Check the deployed revision, schema 19, API/worker health, owner login and
   frontend paths. The previous healthy image is now the compatible baseline;
   the existing release handler can restore its image/configuration without
   dropping schema 19 or restoring an older database dump.

Keep the baseline digest as the minimum rollback target for this release. The
pre-baseline image rejects schema 19 and is no longer a supported rollback.
Never reset or restore the production database as an automatic image rollback.
Later schema changes need their own compatibility assessment; the allowance for
19 does not accept arbitrary future migration versions.

Regression coverage lives in `test_session_compatibility.py`,
`test_repository_persistence.py`, and the PostgreSQL
`test_sso_rollback_compatibility.py`. Ordinary bearer writes are also checked
against a frozen pre-browser session reader. Existing tokens and credentials
continue to work; malformed or unknown session channels are not accepted.

This release deploys the PR's foundation. Provider callbacks, cookie issuance
from OIDC, frontend cookie adoption and organization SSO enforcement remain
separate work described in `ENTERPRISE_SSO_PLAN.md` and `SSO_CONFIGURATION.md`.

## Verified compatibility baseline

Baseline revision: `a2d0b0e6dddfacc6b9484e30c4f29451b7c1367c`.
Image: `asia-south1-docker.pkg.dev/weaves-510819/weaves/backend@sha256:1f3f40efc545db34e4c97f2efd7033e5f98168e1295f7065a88ccac5ce930c61`.
[Baseline CI/deployment](https://github.com/weavelabs-pvt-ltd/weaves/actions/runs/44169542182)
passed, production stayed on schema 18, and owner login/read-only app checks
passed before the foundation cutover.

`verify_sso_rollback.py` ran inside that actual image on an isolated temporary
PostgreSQL 16 container with the PR's exact migration and contract source files.
Schema 19, two sessions and one external identity survived baseline restart;
bearer authentication/revocation and browser-token rejection passed, with
non-audit collection counts preserved. The temporary container/network were
removed. The rehearsal never received the production database URL or credentials.
The host retains its root-only proof at
`/opt/weaves/singlevm/sso-rollback-verification.json`.

The script requires `WEAVES_ROLLBACK_TEST_DATABASE_URL` and rejects every database
name except `weaves_rollback_test` before connecting. Supply the future source
files as `store.py`, `auth.py` and `sso.py` in `--future-source`; execute it inside
the baseline image, not the future release. Never point this test at production.
