# Read-only connectors in local v0

The product API exposes local knowledge, GitHub, Slack, and Jira Cloud as
read-only context capabilities. Every remote connector goes through the same
plugin gateway, input/output schema validation, tool invocation records, and
run provenance path.

## Setup flow

1. In the dashboard, open **Connections → Connect a tool**, choose GitHub, Slack, or Jira, name the workspace connection, and continue to provider authorization.
2. After authorization, Weaves discovers resources available to that account. GitHub users select repositories in GitHub's installation screen and then choose the Weaves connection scope. Slack and Jira users select channels or a site and its projects in Weaves.
3. Weaves validates the selection against the provider's discovered resources and creates or updates the workspace connection.
4. Run `POST /api/v0/plugin-installations/{id}/healthcheck`.
5. Create an agent with the matching capability ID in
   `allowed_capability_ids`, or PATCH an existing agent to publish a new
   version with that capability.
6. Start work with `POST /api/v0/runs`, then inspect the run, invocations,
   artifacts, and audit event.

OAuth tokens and refresh tokens are stored encrypted in PostgreSQL and never
returned from the API. Configure provider apps and callback URLs using
[connector-oauth.md](connector-oauth.md). Legacy API-token create requests
remain supported for existing integrations; dashboard connections use OAuth.

`GET /api/v0/plugin-installations` returns installation status, whether a
credential exists, and the last health-check time; it never returns the secret
reference. A successful health check marks an installation active (or leaves a
disabled installation disabled). A failed check marks it `error` and returns
a sanitized gateway error. Rotate a credential with
`PUT /api/v0/plugin-installations/{id}/credential`, then run the health check
again to restore an errored connection. Local knowledge health checks report
the document count for that installation's workspace. External checks contact
the provider and test the configured scope.

## GitHub

The dashboard uses a GitHub App authorization flow. The user authorizes their
GitHub account, installs the Weaves App, selects repositories in GitHub, then
chooses the Weaves connection scope from repositories GitHub granted. Weaves
checks the installation against the initiating user and mints short-lived
tokens restricted to selected repositories. Reconnecting an existing app
installation updates its scope instead of creating a duplicate. Configure the app as described in
[connector-oauth.md](connector-oauth.md).

The API retains a legacy fine-grained-token request for existing integrations:

```json
{
  "plugin_id": "github",
  "display_name": "Engineering GitHub",
  "api_token": "<fine-grained-token>",
  "repositories": ["acme/service", "acme/web"]
}
```

The token needs read access to Issues and Pull requests in the selected
repositories. Search qualifiers cannot replace the configured repository
allowlist. The health check verifies access to the first configured repository.

## Slack

The dashboard uses Slack OAuth v2. Users authorize the Weaves app for a
workspace, and Weaves stores the returned user token and refresh token encrypted.
The channel allowlist remains enforced by the connector.

```json
{
  "plugin_id": "slack",
  "display_name": "Engineering Slack",
  "user_token": "<xoxp-user-token>",
  "channels": ["engineering", "incidents"],
  "include_thread_replies": true
}
```

Slack search uses a user token with `search:read`; resource discovery requests
`channels:read` and `groups:read` to list the channels the account can access.
Search is issued once per configured channel and results are checked against
the saved workspace connection allowlist.
Thread reply retrieval is opt-in. When enabled, the adapter fetches replies for
at most the top threaded search match, only after its channel name passes the
installation allowlist. The user token also needs `channels:history` for public
channels or `groups:history` for private channels. Slack applies a one-request-
per-minute limit to `conversations.replies` for some non-Marketplace apps, so
this option is intended for low-volume evaluation. If thread access fails or is
rate-limited, Weaves keeps the matching message and continues. See Slack's
[`search.messages`](https://api.slack.com/methods/search.messages) and
[`conversations.replies`](https://api.slack.com/methods/conversations.replies)
references.

## Jira Cloud

The dashboard uses Atlassian OAuth 2.0 (3LO). Users authorize a Jira site and
Weaves binds the installation to its Atlassian cloud ID. Use browser OAuth for
new connections; Atlassian recommends OAuth for cloud app integrations.

```json
{
  "plugin_id": "jira",
  "display_name": "Engineering Jira",
  "api_token": "<atlassian-api-token>",
  "site_url": "https://acme.atlassian.net",
  "email": "automation@acme.com",
  "project_keys": ["ENG", "OPS"]
}
```

Jira uses an Atlassian account email and API token. The API restricts sites to
HTTPS `*.atlassian.net` hosts and scopes JQL search to the configured project
keys. The account must be able to browse those projects. The adapter uses
Jira's [enhanced JQL search endpoint](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/).

## Capability IDs

- `knowledge.search`
- `github.issues.search`
- `slack.messages.search`
- `jira.issues.search`
- `jira.issues.comment` (explicit approval required)

Remote results are capped at five items per capability. Their text is treated
as untrusted evidence when passed to the model, and each source is attached to
the completed artifact's provenance.

## Approval-backed Jira comment

The Jira installation exposes `jira.issues.comment` as a separately enabled
capability. Add it to the installation's `enabled_capability_ids`, then create
or update an agent to include it in its allowlist. A normal run never dispatches
write capabilities. Propose a comment against a successful run:

```json
{
  "capability_id": "jira.issues.comment",
  "issue_key": "ENG-123",
  "comment": "The release checklist is ready for review.",
  "agent_run_id": "<optional-workflow-step-id>"
}
```

Send that body to `POST /api/v0/runs/{run_id}/actions`. The API records a
pending approval and invocation without contacting Jira. In a multi-agent run,
provide `agent_run_id` when more than one step is allowed to comment. A reviewer reads
`GET /api/v0/approvals`, then approves or rejects through
`POST /api/v0/approvals/{approval_id}/decision`:

```json
{"approved": true, "decision_comment": "Looks good."}
```

Only the approved decision path invokes Jira. Denial is terminal and does not
send the comment. The seeded v0 policy requires approval for medium, high, and
critical risk actions. The demo principal is allowed to approve its own
proposal for local evaluation; production identity policy must disable
self-approval. The action is synchronous and approval plus execution is not an
atomic multi-record transaction.
