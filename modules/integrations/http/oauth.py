"""Register browser authorization routes for supported connectors."""

from fastapi import FastAPI

from modules.integrations.http.oauth_context import OAuthRouteDependencies
from modules.integrations.http.oauth_github import OAuthGitHubMixin
from modules.integrations.http.oauth_jira import OAuthJiraMixin
from modules.integrations.http.oauth_mcp import McpOAuthMixin
from modules.integrations.http.oauth_resources import OAuthResourceMixin
from modules.integrations.http.oauth_slack import OAuthSlackMixin
from modules.integrations.http.oauth_start import OAuthStartMixin


class OAuthRouteContext(
    OAuthStartMixin,
    OAuthGitHubMixin,
    OAuthSlackMixin,
    OAuthJiraMixin,
    McpOAuthMixin,
    OAuthResourceMixin,
):
    """Combine focused provider handlers with shared OAuth state helpers."""


def register_oauth_routes(app: FastAPI, dependencies: OAuthRouteDependencies) -> None:
    """Register authenticated start routes and unauthenticated callbacks."""
    context = OAuthRouteContext(dependencies)
    app.get("/api/v0/oauth/setup/{state_id}")(context.oauth_setup)
    app.post("/api/v0/oauth/setup/{state_id}/complete")(context.complete_oauth_setup)
    app.post("/api/v0/oauth/mcp/start")(context.start_mcp_oauth)
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
    app.get("/api/v0/oauth/mcp/callback", include_in_schema=False)(
        context.mcp_oauth_callback
    )
