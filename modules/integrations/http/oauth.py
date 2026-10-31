"""Register browser authorization routes for supported connectors."""

from fastapi import FastAPI

from modules.integrations.http.oauth_context import OAuthRouteDependencies
from modules.integrations.http.oauth_github import OAuthGitHubMixin
from modules.integrations.http.oauth_jira import OAuthJiraMixin
from modules.integrations.http.oauth_slack import OAuthSlackMixin
from modules.integrations.http.oauth_start import OAuthStartMixin


class OAuthRouteContext(
    OAuthStartMixin,
    OAuthGitHubMixin,
    OAuthSlackMixin,
    OAuthJiraMixin,
):
    """Combine focused provider handlers with shared OAuth state helpers."""


def register_oauth_routes(app: FastAPI, dependencies: OAuthRouteDependencies) -> None:
    """Register authenticated start routes and unauthenticated callbacks."""
    context = OAuthRouteContext(dependencies)
    app.get("/api/v0/oauth/providers")(context.oauth_providers)
    app.post("/api/v0/oauth/{provider}/start")(context.start_oauth)
    app.get("/api/v0/oauth/github/callback", include_in_schema=False)(
        context.github_callback
    )
    app.get("/api/v0/oauth/github/installation", include_in_schema=False)(
        context.github_installation
    )
    app.get("/api/v0/oauth/slack/callback", include_in_schema=False)(
        context.slack_callback
    )
    app.get("/api/v0/oauth/jira/callback", include_in_schema=False)(
        context.jira_callback
    )
