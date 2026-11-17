# SSO configuration and release checklist

This branch adds the enterprise identity and browser session foundation. The live provider routes still use deployment-wide environment settings and return bearer sessions through a popup. The new browser session issuer is not connected to those routes yet. Setting credentials does not complete the enterprise rollout.

## Shared server configuration

| Variable | Purpose | Where to obtain it |
| --- | --- | --- |
| `WEAVES_PUBLIC_FRONTEND_URL` | Exact app origin. Production: `https://app.weave-studio.in`; the standalone local API explorer uses `http://localhost:8001`. | Your frontend deployment URL. No path or trailing query. |
| `WEAVES_PUBLIC_API_BASE_URL` | Base used by existing OAuth routes to construct callback URLs. Current production: `https://api.weave-studio.in`. | Your API deployment URL. Keep this on the API origin until the coordinated app-origin callback cutover. |
| `WEAVES_SSO_SESSION_SECRET` | Signs the short-lived OAuth browser state cookie; at least 32 characters. | Generate a new random value per environment; keep it stable across API restarts/replicas. |
| `WEAVES_BROWSER_SESSION_SECRET` | Session-bound CSRF token key for the new browser session path; at least 32 bytes. Optional while that path is inactive. | Generate a separate random value per environment. |
| `CORS_ORIGINS` | Explicit allowed frontend origins for the current bearer API. | Deployment origins; current production uses `https://app.weave-studio.in`. This does not enable cross-origin cookie sessions. |
| `WEAVES_SECRET_ENCRYPTION_KEY` | Encrypts durable secret-store records. Existing production key must be preserved. | For a new environment, generate a Fernet key. Retrieve the existing key through the deployment's secret-management process; never replace it casually. |
| `DATABASE_URL` | PostgreSQL record storage. | The backend deployment injects this from its database configuration. Single-VM Compose constructs it from `POSTGRES_PASSWORD`. |

Generate each signing/CSRF secret independently on a trusted machine:

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

For a **new** environment's encryption key, using the installed backend environment:

```sh
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## Provider settings for the existing routes

| Provider | Variables | How to obtain values |
| --- | --- | --- |
| Google | `WEAVES_SSO_GOOGLE_CLIENT_ID`, `WEAVES_SSO_GOOGLE_CLIENT_SECRET` | In Google Cloud Console, configure Google Auth Platform branding/audience and create an OAuth client of type **Web application**. Copy the client ID and secret from that client. |
| Microsoft Entra | `WEAVES_SSO_MICROSOFT_CLIENT_ID`, `WEAVES_SSO_MICROSOFT_CLIENT_SECRET`, `WEAVES_SSO_MICROSOFT_TENANT_ID` | In Entra admin center, create an App registration. Copy **Application (client) ID** and **Directory (tenant) ID** from Overview. Configure the **Web** platform redirect URI. Under Certificates & secrets, create a client secret and capture its **Value**, not its secret ID. Record the expiry date and rotate before expiry. |
| Microsoft legacy account mapping | `WEAVES_SSO_MICROSOFT_SUBJECTS` | JSON object mapping `tenant-id:user-object-id` to an existing provisioned Weaves email. Obtain the user Object ID from the Entra user record; tenant ID must match the configured tenant. Example: `{"TENANT_UUID:USER_OBJECT_UUID":"member@example.com"}`. This temporary mapping is required by the existing Microsoft callback; the planned external-identity flow replaces it. |
| Zoho | `WEAVES_SSO_ZOHO_CLIENT_ID`, `WEAVES_SSO_ZOHO_CLIENT_SECRET` | In the Zoho API Console, register a **Server-based Application**, configure its homepage and redirect URI, and copy the client ID/secret. Confirm which Zoho data center owns the client and user accounts. |

Official setup references: [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [Entra app registration](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app), [Entra web redirect configuration](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri), [Zoho app registration](https://www.zoho.com/developer/oauth/register-app.html), [Zoho OIDC](https://www.zoho.com/developer/oauth/sign-in-using-zoho-oidc.html).

Use separate registrations/credentials for development, staging, and production. Configure a test audience where applicable. The current scope is `openid email profile`; no Google Workspace, Microsoft Graph, or Zoho business-data permissions are needed merely to sign in. Provider policy may require tenant-admin approval or consent-screen publication before wider use.

The existing Zoho code pins `accounts.zoho.com`. There is no regional environment variable implemented yet. An India/EU/other data-center client requires the planned issuer selection and interoperability test before enablement. Google and Zoho also currently require `email_verified=true`; validate actual provider claims in staging. All providers require a provisioned active Weaves user and workspace access; credentials alone do not provision accounts.

## Exact callback URLs for the current routes

Register the following URLs in the corresponding provider app. These are generated from `WEAVES_PUBLIC_API_BASE_URL` and must match exactly.

| Provider | Current production | Local development |
| --- | --- | --- |
| Google | `https://api.weave-studio.in/api/v0/auth/sso/google/callback` | `http://localhost:8001/api/v0/auth/sso/google/callback` |
| Microsoft | `https://api.weave-studio.in/api/v0/auth/sso/microsoft/callback` | `http://localhost:8001/api/v0/auth/sso/microsoft/callback` |
| Zoho | `https://api.weave-studio.in/api/v0/auth/sso/zoho/callback` | `http://localhost:8001/api/v0/auth/sso/zoho/callback` |

Keep the same hostname throughout local start/callback; `localhost` and `127.0.0.1` have different cookies. Local HTTP callback acceptance depends on provider policy. Use a registered HTTPS staging URL when required. For the new cookie flow, use HTTPS for browser verification, matching the production cookie flags.

The final organization-scoped callback paths are pending implementation. Register their app-origin URLs only after the routes exist; then coordinate provider registration, API callback generation, frontend API base (`/api/v0`), and gateway routing. Do not change the shared API base early: other connector OAuth flows also use it.

## Where to store and inject settings

For local use, export variables into the API process or use `uvicorn --env-file` with an ignored `.env` file. The backend does not automatically load `.env` just because it exists. `.env.example` contains names/placeholders only.

The current production target is the single VM documented in `infra/gcp/singlevm/README.md`. Its root-only `/opt/weaves/singlevm/runtime.env` supplies Compose substitutions. The Compose service must explicitly forward variables into the API container: the existing file forwards the state secret and optional browser secret, but **does not yet forward the Google/Microsoft/Zoho client variables**. That wiring and the secure credential update procedure are deployment prerequisites. Merely adding them to `runtime.env` will not enable the providers. Recreate the API after changing credentials.

Keep signing keys and client secrets in managed secret storage with controlled server injection. Do not put them in frontend build variables, PRs, issues, screenshots, or GitHub deployment logs. The `WEAVES_GITHUB_APP_*` variables configure the GitHub connector; GitHub CLI account `Snigdhchandra23` is used for repository work and does not provide Google/Microsoft/Zoho credentials.

The planned enterprise connection records store per-organization provider, client ID, pinned issuer/tenant, and a client-secret reference. The external-identity records store approved subject-to-user bindings. There are no public connection administration routes yet. Customer credentials move into the secret store behind those routes once implemented; avoid designing more deployment-wide customer variables.

## Deployment and pending work

Pushing/merging to `main` automatically deploys the backend to the single VM after CI. A draft PR does not deploy. The historical managed Cloud Run deployment is retired and is not a release target.

- [x] Organization-scoped connection and immutable external-identity contracts/repositories.
- [x] PostgreSQL migration 19: unique subject/user bindings and update immutability.
- [x] Admin-controlled identity binding service and runtime composition.
- [x] Browser session channel, cookie helper, CSRF validation, inspection/logout routes.
- [x] Single-VM app-origin API routing configuration and optional browser-secret injection.
- [x] Local tests: 302 passed with real PostgreSQL; lint, format, mypy, architecture and size checks passed. Local environment used Python 3.14; CI must verify Python 3.12.
- [ ] Connection create/test/enable/disable APIs, secret storage integration, and admin UI.
- [ ] Server-side one-use SSO transactions, organization-scoped start/callback routes, and strict issuer/tenant checks for all three providers, including Zoho regional handling.
- [ ] Connect validated callbacks to approved external identities and issue browser cookies without returning bearer credentials.
- [ ] Frontend cookie session, CSRF header, success handling, and same-origin `/api/v0` integration.
- [ ] Organization sign-in policy, password/email-code enforcement, session policy and connection-disable revocation, audited recovery and domain verification.
- [ ] Secure production credential injection and provider callback registration.
- [ ] Staging browser tests with real Google/Microsoft/Zoho accounts; CI on Python 3.12; backup/rollback rehearsal.
- [ ] Pilot one organization before broader SSO enforcement.
- [ ] Subsequent customer-required SCIM, group mapping/JIT policy, and SAML delivery.

Release the foundation with browser SSO inactive, then cut over after the pending first-release items pass. The backend deployment backs up PostgreSQL and can restore previous images/configuration. Migration 19 remains applied on rollback; never reset the production database to undo an application release. The current single VM has restart downtime and no high availability.
