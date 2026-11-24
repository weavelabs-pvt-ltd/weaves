# Runbook: connector app registration

Connectors (GitHub, Slack, Jira, Notion, Datadog, custom MCP) are off until the **owner**
registers each provider's OAuth app and adds its credentials to
`/opt/weaves/singlevm/runtime.env`. The full, step-by-step procedure, callback URLs, scopes
and verification checks live in
**[infra/gcp/singlevm/CONNECTORS.md](../../infra/gcp/singlevm/CONNECTORS.md)**; this page is
the operator checklist around it and does not repeat it. Registering apps is an
owner-account action in third-party consoles; an engineer or agent must not create apps or
paste credentials without the owner present.

## Which settings, which process

| Connector | Needs (in `runtime.env`) | Read by |
|---|---|---|
| GitHub | `WEAVES_GITHUB_APP_ID`, `_PRIVATE_KEY`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_SLUG` | API (all) and worker (ID, private key) |
| Slack | `WEAVES_SLACK_OAUTH_CLIENT_ID`, `_CLIENT_SECRET` | API and worker |
| Jira | `WEAVES_ATLASSIAN_OAUTH_CLIENT_ID`, `_CLIENT_SECRET` | API and worker |
| Notion, Datadog, custom MCP | `WEAVES_MCP_ALLOWED_HOSTS` (exact hosts; keep minimal) | API and worker |

Callback and setup URLs (copy exactly, no trailing slash) are all under
`https://api.weave-studio.in/api/v0/oauth/...`; the table is in CONNECTORS.md section 2.
A variable missing from `compose.yaml`'s `environment:` list never reaches a container, and
a missing value only shows the connector as unavailable.

## Checklist

1. Owner registers the app (CONNECTORS.md section 2) and keeps the credentials in the
   owner's own vault. Note the registration date and who owns the app account.
2. On the VM add the values with `sudoedit` and single quotes, back up first, validate with
   `docker compose ... config -q`, apply to API and worker only (CONNECTORS.md section 3).
   Never use `echo` with a secret; never put one in a command line.
3. Verify without printing values (CONNECTORS.md section 4): the catalog shows
   `"available": true`, the consent screen's `redirect_uri` matches, and the worker reports
   each variable `set`.
4. Record in the ticket: connector, date, app owner, which scopes. No values.

## Rotating or removing an app

* Rotate a client secret: create the new secret in the provider console, update
  `runtime.env`, `dc up -d --no-deps product-api product-worker`, test a Connect flow, then
  delete the old secret ([secrets-rotation.md](secrets-rotation.md)).
* Remove a connector for everyone: delete its variables, restart API and worker. Existing
  tenant tokens stay encrypted in the database and stop working until the app is restored;
  tell customers.
* Compromised credentials: rotate at the provider **first**, then update `runtime.env`.
  Customers' stored OAuth tokens for the app may need re-authorization.

## Failures

| Symptom | Likely cause |
|---|---|
| Connector shows "OAuth app setup is required" | variable empty, not forwarded by `compose.yaml`, or containers not restarted |
| Consent screen error `redirect_uri` mismatch | registered URL differs by a character |
| Notion/Datadog "Ask your administrator to allow this MCP host" | host not in `WEAVES_MCP_ALLOWED_HOSTS` (Datadog also needs `app.datadoghq.com`) |
| Connect works, runs fail | worker lacks the value (check section 4 step 3), or a token expired and refresh failed: read worker logs ([incident-response.md](incident-response.md) section 2) |
| After a restore, connectors need re-auth | tokens are encrypted with the Fernet key; a different key makes them unreadable ([secrets-rotation.md](secrets-rotation.md)) |

Kill switch for a misbehaving connector tenant: quarantine the organization
([kill-switch-and-quarantine.md](kill-switch-and-quarantine.md)); there is no per-connector
platform switch.
