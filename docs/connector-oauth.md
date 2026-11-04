# Connector browser authorization

GitHub, Slack, and Jira Cloud use their organization-supported browser authorization flows. Weaves stores OAuth access and refresh tokens in its encrypted secret store. MCP connections continue to use the MCP server's own endpoint and optional bearer token.

## Public URLs

Configure these non-secret URLs for the deployed environment:

```text
WEAVES_PUBLIC_API_BASE_URL=<public backend API origin>
WEAVES_PUBLIC_FRONTEND_URL=https://app.weave-studio.in
```

Register these callback URLs exactly with the provider applications:

```text
GitHub user authorization callback: <public backend API origin>/api/v0/oauth/github/callback
GitHub App setup URL:               <public backend API origin>/api/v0/oauth/github/installation
Slack redirect URL:                 <public backend API origin>/api/v0/oauth/slack/callback
Atlassian callback URL:             <public backend API origin>/api/v0/oauth/jira/callback
```

Use the public API origin already configured in the frontend's `WEAVES_API_BASE` (without the `/api/v0` suffix). It can be the backend's Cloud Run URL; a separate `api.weave-studio.in` DNS record is optional. Copy the resulting full callback URLs exactly into each provider app.

## GitHub App

Create a GitHub App and configure its **User authorization callback URL** and **Setup URL** from the list above. Enable the setup URL. Grant read-only repository permissions for **Metadata**, **Issues**, and **Pull requests**. The connection flow first authorizes the initiating GitHub user, then installs the app and verifies that the installation belongs to that user. After GitHub's installation page returns, Weaves discovers the repositories granted to that installation and asks the user to select the connection scope. Runtime API calls use short-lived installation tokens scoped to only those selected repositories.

Set these values in Secret Manager and inject them into the backend service:

```text
WEAVES_GITHUB_APP_ID
WEAVES_GITHUB_APP_SLUG
WEAVES_GITHUB_APP_CLIENT_ID
WEAVES_GITHUB_APP_CLIENT_SECRET
WEAVES_GITHUB_APP_PRIVATE_KEY
```

Use the private key PEM contents for `WEAVES_GITHUB_APP_PRIVATE_KEY`. The app's client secret and private key must never be committed to source control.

## Slack app

Enable OAuth v2 and set the OAuth redirect URL exactly as shown. Weaves requests the user scopes `search:read`, `channels:read`, `groups:read`, `channels:history`, and `groups:history`; Slack displays the authorization screen and the workspace administrator/user can approve or reject it. After authorization, Weaves lists channels visible to that account and lets the user select the connection allowlist.

Set these Secret Manager values:

```text
WEAVES_SLACK_OAUTH_CLIENT_ID
WEAVES_SLACK_OAUTH_CLIENT_SECRET
```

## Atlassian app

Create an OAuth 2.0 (3LO) integration, add the callback URL, and grant `read:jira-work`, `write:jira-work`, and `offline_access`. The write scope supports the platform's approval-governed Jira comment action; agents only receive capabilities explicitly enabled for their installation. After authorization, Weaves retrieves accessible Jira sites and projects, then lets the user select one site and its project allowlist. The selected site is bound by its Atlassian cloud ID.

Set these Secret Manager values:

```text
WEAVES_ATLASSIAN_OAUTH_CLIENT_ID
WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET
```

## Secret Manager and activation

Create a separate Secret Manager secret for every provider credential above, grant both the API and worker runtime service accounts permission to access them, and inject them as environment variables. The API needs the GitHub client credentials, Slack and Atlassian client credentials, app slug, public callback origin, and (for GitHub) app ID and private key. The worker needs the GitHub app ID and private key plus Slack and Atlassian client credentials to mint or refresh runtime tokens. Do not put actual credentials in `.env.example`, container images, broadly readable GitHub Actions variables, or committed deployment files. OAuth buttons are reported as available only when the provider app's required configuration is present.

For local Docker Compose, place values in the ignored local `.env` file. For Cloud Run, add the Secret Manager references to both the API service and worker pool's secret environment configuration after creating the provider app secrets.

The OAuth state is random, stored server-side for ten minutes, and consumed once at each flow transition. Provider callbacks do not require a Weaves session because they originate from the provider redirect; they restore the initiating principal and workspace from the stored state. A final authenticated Weaves step validates selections against the discovered provider resources before creating or updating the workspace connection. Temporary OAuth credentials are removed after completion and cleaned up when expired states are revisited or a new authorization starts.

## Provider references

- [GitHub App installation access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitHub App setup URL security](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url)
- [Slack OAuth v2](https://api.slack.com/authentication/oauth-v2)
- [Atlassian OAuth 2.0 (3LO)](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/)
