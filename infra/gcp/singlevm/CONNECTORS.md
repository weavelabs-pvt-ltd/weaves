# Connectors on the single VM

Connectors are off until the owner registers the provider apps and adds their
credentials to `/opt/weaves/singlevm/runtime.env`. `compose.yaml` forwards the
variables below to the containers with an empty default, so a missing value
only makes that connector show as unavailable. Never commit any of these
values.

Public URLs (already set in `compose.yaml`):

- API base: `https://api.weave-studio.in` (`WEAVES_PUBLIC_API_BASE_URL`). The
  provider redirects land on this host, which Caddy proxies to `product-api`.
  Callbacks need no Weaves session, so the host-bound cookie rule does not
  apply to them.
- Frontend: `https://app.weave-studio.in` (`WEAVES_PUBLIC_FRONTEND_URL`).

## 1. Settings and who reads them

`runtime.env` is passed with `--env-file`, and `compose.yaml` lists each
variable explicitly under `environment:`. A variable in `runtime.env` that is
not listed there never reaches a container.

| Variable | product-api | product-worker | Purpose |
| --- | --- | --- | --- |
| `WEAVES_GITHUB_APP_ID` | yes | yes | App JWT (API: readiness and install flow; worker: installation tokens) |
| `WEAVES_GITHUB_APP_PRIVATE_KEY` | yes | yes | Signs the App JWT |
| `WEAVES_GITHUB_APP_CLIENT_ID` | yes | no | User authorization |
| `WEAVES_GITHUB_APP_CLIENT_SECRET` | yes | no | User authorization |
| `WEAVES_GITHUB_APP_SLUG` | yes | no | Installation redirect |
| `WEAVES_SLACK_OAUTH_CLIENT_ID` | yes | yes | Authorize, code exchange (API); token refresh (worker) |
| `WEAVES_SLACK_OAUTH_CLIENT_SECRET` | yes | yes | Same |
| `WEAVES_ATLASSIAN_OAUTH_CLIENT_ID` | yes | yes | Authorize, code exchange (API); token refresh (worker) |
| `WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET` | yes | yes | Same |
| `WEAVES_MCP_ALLOWED_HOSTS` | yes | yes | Exact-host allowlist for MCP endpoints and their OAuth hosts |

Connector readiness (the catalog) is computed in the API and also requires
`WEAVES_PUBLIC_API_BASE_URL`. `WEAVES_SECRET_ENCRYPTION_KEY`, which encrypts the
stored OAuth tokens, is already required by the deployment. Notion and Datadog
need no other key.

| Connector | Needs |
| --- | --- |
| GitHub | all five `WEAVES_GITHUB_APP_*` values |
| Slack | the two `WEAVES_SLACK_OAUTH_*` values |
| Jira | the two `WEAVES_ATLASSIAN_OAUTH_*` values |
| Notion | `WEAVES_MCP_ALLOWED_HOSTS` containing `mcp.notion.com` |
| Datadog | `WEAVES_MCP_ALLOWED_HOSTS` containing `mcp.datadoghq.com` and `app.datadoghq.com` |
| Custom MCP | any non-empty `WEAVES_MCP_ALLOWED_HOSTS`, only for the hosts listed |

### Slack bot settings

The Slack bot (`docs/slack-bot-design.md`) adds three optional settings.
`compose.yaml` forwards `WEAVES_SLACK_SIGNING_SECRET` and `WEAVES_SLACK_ENABLED`
to `product-api` only, both with an empty default (empty means off), so nothing
changes until the operator sets them in `runtime.env` on the VM.

| Variable | product-api | product-worker | Purpose |
| --- | --- | --- | --- |
| `WEAVES_SLACK_SIGNING_SECRET` | yes | no | Verifies `X-Slack-Signature` on events and interactions. Optional; unset means `POST /api/v0/slack/events` answers 503 and does nothing |
| `WEAVES_SLACK_ENABLED` | yes | not yet | Kill switch, default off. `true` without the signing secret stops the process at start. While off, events are acked and dropped, and the install cannot start |
| `WEAVES_SLACK_API_BASE_URL` | not forwarded | not forwarded | Slack API origin, default `https://slack.com/api`. Used only by the test harness to point at the fake Slack server |
| `WEAVES_SLACK_NATIVE_STATUS` | no | yes | `on` shows Slack's own "is working" status instead of one edited working message. Default off: unverified for channel threads (design 2.3, owner's dev-workspace test); an unsupported answer falls back to the message |
| `WEAVES_SLACK_REPLY_DEADLINE` | no | yes | Seconds (60 to 3600, default 900) before an unanswered request fails with a short reference |
| `WEAVES_SLACK_QA_RETENTION_DAYS` | no | yes | Days (1 to 365, default 30) before a thread's hidden Q&A conversation is deleted |

With the worker running, `WEAVES_SLACK_ENABLED='true'` also starts the three Slack
ticks (dispatch and reply every second, retention every 15 minutes). An operator
`worker_paused` switch holds the dispatcher (the inbox keeps its rows); replies
for work that already ran still go out.

The install also needs `WEAVES_SLACK_OAUTH_CLIENT_ID`, `WEAVES_SLACK_OAUTH_CLIENT_SECRET`
(the same Slack app as the read connector) and `WEAVES_PUBLIC_API_BASE_URL`.

Enable it (owner, on the VM, after the backend with S1/S2 is deployed): copy the
signing secret from the Slack app's Basic Information page (Signing Secret) into
`runtime.env` as `WEAVES_SLACK_SIGNING_SECRET='<secret>'` (single quotes, never in
Git, never printed) and set `WEAVES_SLACK_ENABLED='true'`, recreate `product-api`,
then in the Slack app switch Event Subscriptions on (the Request URL below
verifies against the running endpoint). The signing secret is a different value
from `WEAVES_SLACK_OAUTH_CLIENT_SECRET`.

Slack app settings for the bot install and events (design section 5.1):

| Slack setting | Value |
| --- | --- |
| OAuth & Permissions, Redirect URLs | add `https://api.weave-studio.in/api/v0/oauth/slack/install/callback` (keep the connector redirect `.../oauth/slack/callback`) |
| OAuth & Permissions, Bot Token Scopes | `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `channels:read`, `groups:read`, `reactions:read`, `reactions:write` |
| Event Subscriptions, Request URL | `https://api.weave-studio.in/api/v0/slack/events` (turn on only after S2 is deployed and the secret is set) |
| Event Subscriptions, bot events | `app_mention`, `reaction_added`, `app_uninstalled`, `tokens_revoked` |
| Interactivity & Shortcuts, Interactivity | **On**, Request URL `https://api.weave-studio.in/api/v0/slack/interactions`. Switch it on only after the S4b backend is deployed and `WEAVES_SLACK_SIGNING_SECRET` is set (Slack verifies the URL when you save it); until then it stays off, and no workspace should be allowed to ask (the Stop and Try again buttons would show Slack's "not configured for interactivity" error) |
| Slash commands, shortcuts, token rotation | leave off |

The interactions endpoint (`POST /api/v0/slack/interactions`) uses the same signing secret
and the same five-minute replay window as events and answers 503 while the secret is unset.
It only stores the press (one lookup and one insert, acked in well under Slack's three
seconds); the worker's `slack_interactions` tick carries out Stop and Try again, so the
worker needs no new setting. Who may press: the person who asked, or an admin whose Slack
user is on the Slack access page's people list mapped to a Weaves member with
`slack.manage`; everyone else is told privately. `@Weaves stop` and `@Weaves retry` in the
thread work without Interactivity and follow the same rules.

An organization admin then opens Connections in the Weaves app and chooses Add to
Slack. Enterprise Grid org-wide installs are refused; one Slack workspace can be
bound to one Weaves organization at a time, and Disconnect (or an
`app_uninstalled` event) frees it.

### MCP allowlist value for production

```
WEAVES_MCP_ALLOWED_HOSTS='mcp.notion.com,mcp.datadoghq.com,app.datadoghq.com'
```

The code checks the MCP endpoint host and also the authorization, token and
registration hosts found in the server's OAuth metadata. Notion serves all of
them from `mcp.notion.com`. Datadog's MCP endpoint is `mcp.datadoghq.com`, but
its metadata (checked 2026-11) points authorization, token and registration at
`app.datadoghq.com`, so that host is required too. Notion and Datadog use
dynamic client registration with PKCE, so they need no owner-created OAuth app
or credentials; each user authorizes in the provider's own consent screen. If
the Datadog account lives on another Datadog site, the hosts differ; the
curated endpoint is fixed to `mcp.datadoghq.com`. Any host added here also
becomes reachable through "Custom MCP server", so keep the list minimal.

## 2. Register the provider apps (owner steps)

Callback URLs, derived from `oauth_start.py` and `oauth.py` (copy exactly, no
trailing slash):

| Provider | Field | URL |
| --- | --- | --- |
| GitHub | Callback URL (user authorization) | `https://api.weave-studio.in/api/v0/oauth/github/callback` |
| GitHub | Setup URL | `https://api.weave-studio.in/api/v0/oauth/github/installation` |
| Slack | Redirect URL | `https://api.weave-studio.in/api/v0/oauth/slack/callback` |
| Atlassian | Callback URL | `https://api.weave-studio.in/api/v0/oauth/jira/callback` |
| Notion / Datadog (MCP) | registered automatically | `https://api.weave-studio.in/api/v0/oauth/mcp/callback` |

### GitHub App

1. On GitHub open the owning organization (or your account), then Settings,
   Developer settings, GitHub Apps, New GitHub App.
2. Name it (this becomes the slug in `github.com/apps/<slug>`), set Homepage URL
   to `https://app.weave-studio.in`.
3. Identifying and authorizing users: set Callback URL to the GitHub callback
   above. Leave "Expire user authorization tokens" and "Request user
   authorization (OAuth) during installation" at their defaults; Weaves does a
   separate authorization first.
4. Post installation: set Setup URL to the installation URL above and tick
   "Redirect on update".
5. Webhook: untick Active (Weaves does not use webhooks).
6. Repository permissions: Contents Read-only, Metadata Read-only, Issues
   Read-only, Pull requests Read-only. No account or organization permissions.
   Contents is needed because the connector searches code (`/search/code`) and
   reads file contents through the git trees and blobs APIs
   (`clients/integrations/github.py`); without it only issue search works on
   private repositories.
7. Where can this app be installed: choose "Any account" if customers will
   install it, otherwise "Only on this account". Create the app.
8. On the app page copy the App ID (`WEAVES_GITHUB_APP_ID`) and the slug from
   the public link (`WEAVES_GITHUB_APP_SLUG`). Under Client secrets, Generate a
   new client secret and copy it with the Client ID
   (`WEAVES_GITHUB_APP_CLIENT_ID`, `WEAVES_GITHUB_APP_CLIENT_SECRET`).
9. Under Private keys, Generate a private key; a `.pem` downloads
   (`WEAVES_GITHUB_APP_PRIVATE_KEY`). Keep it off shared drives.

### Slack app

1. Open `https://api.slack.com/apps`, Create New App, From scratch; pick a name
   and a development workspace.
2. OAuth & Permissions: under Redirect URLs, Add New Redirect URL with the Slack
   URL above, then Save URLs.
3. Under Scopes, **User Token Scopes** (not Bot Token Scopes), add exactly the
   scopes the code requests: `search:read`, `channels:read`, `groups:read`,
   `channels:history`, `groups:history`.
4. Basic Information, App Credentials: copy Client ID and Client Secret
   (`WEAVES_SLACK_OAUTH_CLIENT_ID`, `WEAVES_SLACK_OAUTH_CLIENT_SECRET`).
5. To let workspaces other than the development one install it, open Manage
   Distribution and activate public distribution once the checklist is green.
6. Optional: token rotation (OAuth & Permissions) is supported; the worker
   refreshes rotated tokens with the same client credentials.

### Atlassian (Jira) OAuth 2.0 (3LO)

1. Open `https://developer.atlassian.com/console/myapps/`, Create, OAuth 2.0
   integration; name it and accept the terms.
2. Permissions: add the Jira API and Configure it. Enable the classic scopes
   `read:jira-work` and `write:jira-work` (the code requests these plus
   `offline_access`, which is implicit for authorization-code grants).
3. Authorization: add OAuth 2.0 (3LO) and set the Callback URL to the Jira URL
   above.
4. Settings: copy Client ID and Secret (`WEAVES_ATLASSIAN_OAUTH_CLIENT_ID`,
   `WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET`).
5. Distribution: until the app is shared, only its owner can authorize. Set
   Distribution to Sharing and complete the vendor and privacy fields for other
   Atlassian sites.

## 3. Add the values on the VM safely

1. SSH to the VM (do this yourself; nothing in this repository touches it).
   Work as root and avoid putting secrets in shell arguments or history. Do not
   use `echo` with a secret.
2. Back up and edit with an editor:

   ```
   cd /opt/weaves/singlevm
   cp -p runtime.env runtime.env.bak   # delete once verified
   sudoedit runtime.env                # or: sudo -e runtime.env
   ```

3. Append lines using single quotes. Single quotes keep `$`, `#` and spaces
   literal in both Compose and the `shlex` parsing in `configure.py`. The
   private key is pasted as real multi-line text inside the quotes:

   ```
   WEAVES_GITHUB_APP_ID='123456'
   WEAVES_GITHUB_APP_SLUG='<slug>'
   WEAVES_GITHUB_APP_CLIENT_ID='<client id>'
   WEAVES_GITHUB_APP_CLIENT_SECRET='<client secret>'
   WEAVES_GITHUB_APP_PRIVATE_KEY='-----BEGIN RSA PRIVATE KEY-----
   <pem body lines>
   -----END RSA PRIVATE KEY-----'
   WEAVES_SLACK_OAUTH_CLIENT_ID='<id>'
   WEAVES_SLACK_OAUTH_CLIENT_SECRET='<secret>'
   WEAVES_ATLASSIAN_OAUTH_CLIENT_ID='<id>'
   WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET='<secret>'
   WEAVES_MCP_ALLOWED_HOSTS='mcp.notion.com,mcp.datadoghq.com,app.datadoghq.com'
   ```

   Do not add duplicate keys. Keep `runtime.env` mode 0600 and owned by root.
   Releases only rewrite the `BACKEND_IMAGE` and `FRONTEND_IMAGE` lines, so
   these values survive deploys.
4. Validate without printing values (the command exits non-zero on error):

   ```
   docker compose --project-name weaves-single --project-directory /opt/weaves/singlevm \
     --env-file runtime.env --file compose.yaml config -q
   ```

   Make sure the compose file on the VM is the version that forwards these
   variables (deploy a backend release first; it installs `compose.yaml`).
5. Apply to the API and worker only (this recreates just those containers):

   ```
   docker compose --project-name weaves-single --project-directory /opt/weaves/singlevm \
     --env-file runtime.env --file compose.yaml up -d --no-deps product-api product-worker
   ```

   Values are read at process start, so a restart is required after every
   change. Delete `runtime.env.bak` after verifying.

## 4. Verify

1. Sign in at `https://app.weave-studio.in` and open Connectors in the
   product, or call the catalog with an account session:
   `GET https://app.weave-studio.in/api/v0/connectors/catalog` (with
   `X-Organization-Id`). Each configured connector shows `"available": true`
   and `"unavailable_reason": null`; GitHub, Slack and Jira otherwise report
   "OAuth app setup is required." and Notion/Datadog "Ask your administrator to
   allow this MCP host."
2. Click Connect on each connector. The browser should reach the provider's
   consent screen with a `redirect_uri` equal to the table above. A
   `redirect_uri` mismatch error means the registered URL differs by a
   character.
3. Check the worker received its values without printing them:
   `docker compose ... exec product-worker sh -c 'for v in WEAVES_GITHUB_APP_ID WEAVES_SLACK_OAUTH_CLIENT_ID WEAVES_ATLASSIAN_OAUTH_CLIENT_ID WEAVES_MCP_ALLOWED_HOSTS; do [ -n "$(printenv $v)" ] && echo "$v set" || echo "$v MISSING"; done'`
