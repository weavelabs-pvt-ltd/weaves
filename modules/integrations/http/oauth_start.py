"""OAuth Start route handlers."""

import os
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
)
from modules.integrations.http.oauth_schemas import _OAuthStart


class OAuthStartMixin(OAuthContextBase):
    """Implement oauth start OAuth route operations."""

    def oauth_providers(self) -> dict[str, bool]:
        """Handle the oauth providers step of the OAuth flow."""
        self.dependencies.state_service.authorize_current_principal()

        public_api_configured = bool(
            os.environ.get("WEAVES_PUBLIC_API_BASE_URL", "").strip()
        )

        return {
            "github": public_api_configured
            and bool(
                os.environ.get("WEAVES_GITHUB_APP_CLIENT_ID", "").strip()
                and os.environ.get("WEAVES_GITHUB_APP_CLIENT_SECRET", "").strip()
                and os.environ.get("WEAVES_GITHUB_APP_SLUG", "").strip()
                and os.environ.get("WEAVES_GITHUB_APP_ID", "").strip()
                and os.environ.get("WEAVES_GITHUB_APP_PRIVATE_KEY", "").strip()
            ),
            "slack": public_api_configured
            and bool(
                os.environ.get("WEAVES_SLACK_OAUTH_CLIENT_ID", "").strip()
                and os.environ.get("WEAVES_SLACK_OAUTH_CLIENT_SECRET", "").strip()
            ),
            "jira": public_api_configured
            and bool(
                os.environ.get("WEAVES_ATLASSIAN_OAUTH_CLIENT_ID", "").strip()
                and os.environ.get("WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET", "").strip()
            ),
        }

    def start_oauth(self, provider: str, request: _OAuthStart) -> dict[str, str]:
        """Validate the requested connector and return its authorization URL."""
        self.dependencies.state_service.authorize_current_principal()
        handlers = {
            "github": self._start_github,
            "slack": self._start_slack,
            "jira": self._start_jira,
        }
        handler = handlers.get(provider)
        if handler is None:
            raise HTTPException(
                status_code=404, detail="OAuth connector is not supported"
            )
        return handler(request)

    def _start_github(self, request: _OAuthStart) -> dict[str, str]:
        """Build the Github provider authorization request."""
        configuration: dict[str, Any] = {"display_name": request.display_name}
        client_id = os.environ.get("WEAVES_GITHUB_APP_CLIENT_ID", "").strip()
        if not all(
            (
                client_id,
                os.environ.get("WEAVES_GITHUB_APP_CLIENT_SECRET", "").strip(),
                os.environ.get("WEAVES_GITHUB_APP_SLUG", "").strip(),
                os.environ.get("WEAVES_GITHUB_APP_ID", "").strip(),
                os.environ.get("WEAVES_GITHUB_APP_PRIVATE_KEY", "").strip(),
            )
        ):
            raise HTTPException(
                status_code=503, detail="GitHub App OAuth is not configured"
            )
        if not request.repositories:
            raise HTTPException(
                status_code=422, detail="Select at least one repository"
            )
        configuration["repositories"] = list(request.repositories)
        redirect = self.callback_url("github/callback")
        state = self.state_record("github", configuration)
        return {
            "authorization_url": "https://github.com/login/oauth/authorize?"
            + urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "state": state.oauth_state_id,
                }
            )
        }

    def _start_slack(self, request: _OAuthStart) -> dict[str, str]:
        """Build the Slack provider authorization request."""
        configuration: dict[str, Any] = {"display_name": request.display_name}
        client_id = os.environ.get("WEAVES_SLACK_OAUTH_CLIENT_ID", "").strip()
        if (
            not client_id
            or not os.environ.get("WEAVES_SLACK_OAUTH_CLIENT_SECRET", "").strip()
        ):
            raise HTTPException(status_code=503, detail="Slack OAuth is not configured")
        if not request.channels:
            raise HTTPException(
                status_code=422, detail="Select at least one Slack channel"
            )
        configuration.update(
            {
                "channels": list(request.channels),
                "include_thread_replies": request.include_thread_replies,
            }
        )
        redirect = self.callback_url("slack/callback")
        state = self.state_record("slack", configuration)
        scopes = "search:read,channels:history,groups:history"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": state.oauth_state_id,
            "user_scope": scopes,
        }
        return {
            "authorization_url": "https://slack.com/oauth/v2/authorize?"
            + urlencode(params)
        }

    def _start_jira(self, request: _OAuthStart) -> dict[str, str]:
        """Build the Jira provider authorization request."""
        configuration: dict[str, Any] = {"display_name": request.display_name}
        client_id = os.environ.get("WEAVES_ATLASSIAN_OAUTH_CLIENT_ID", "").strip()
        if (
            not client_id
            or not os.environ.get("WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET", "").strip()
        ):
            raise HTTPException(
                status_code=503, detail="Atlassian OAuth is not configured"
            )
        site_url = (request.site_url or "").rstrip("/")
        if re.fullmatch("https://[A-Za-z0-9-]+\\.atlassian\\.net", site_url) is None:
            raise HTTPException(
                status_code=422, detail="Enter a valid Atlassian site URL"
            )
        if not request.project_keys:
            raise HTTPException(
                status_code=422, detail="Select at least one Jira project"
            )
        configuration.update(
            {"site_url": site_url, "project_keys": list(request.project_keys)}
        )
        redirect = self.callback_url("jira/callback")
        state = self.state_record("jira", configuration)
        params = {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": "read:jira-work write:jira-work offline_access",
            "redirect_uri": redirect,
            "state": state.oauth_state_id,
            "response_type": "code",
            "prompt": "consent",
        }
        return {
            "authorization_url": "https://auth.atlassian.com/authorize?"
            + urlencode(params)
        }
