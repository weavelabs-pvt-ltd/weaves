"""OAuth Jira route handlers."""

import os
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
    _state_string,
    _state_strings,
)


class OAuthJiraMixin(OAuthContextBase):
    """Implement oauth jira OAuth route operations."""

    def jira_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the jira callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "jira")

            return self.finish("jira", "failed")

        record = self.take_state(state, "jira")

        self.request_as(record)

        client_id = os.environ["WEAVES_ATLASSIAN_OAUTH_CLIENT_ID"].strip()

        client_secret = os.environ["WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET"].strip()

        redirect = self.callback_url("jira/callback")

        try:
            payload = self.exchange(
                "https://auth.atlassian.com/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect,
                },
                json_payload=True,
            )

        except HTTPException:
            return self.finish("jira", "failed")

        access = payload.get("access_token")

        if not isinstance(access, str):
            return self.finish("jira", "failed")

        site_url = _state_string(record, "site_url")
        cloud_id = self._jira_cloud_id(access, site_url)
        if cloud_id is None:
            return self.finish("jira", "failed")

        try:
            self.dependencies.connection_service.create_jira_oauth_installation(
                display_name=_state_string(record, "display_name"),
                token_bundle=self.token_bundle("jira", payload),
                site_url=site_url,
                cloud_id=cloud_id,
                project_keys=_state_strings(record, "project_keys"),
                requested_by_principal_id=record.principal_id,
            )

        except Exception:
            return self.finish("jira", "failed")

        return self.finish("jira", "success")

    def _jira_cloud_id(self, access_token: str, site_url: str) -> str | None:
        """Resolve Atlassian's cloud ID matching the configured site URL."""
        try:
            response = self.client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=15,
            )
            response.raise_for_status()
            resources = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        expected_host = urlparse(site_url).hostname
        resource = next(
            (
                item
                for item in resources
                if isinstance(item, dict)
                and urlparse(item.get("url", "")).hostname == expected_host
            ),
            None,
        )
        cloud_id = resource.get("id") if isinstance(resource, dict) else None
        return cloud_id if isinstance(cloud_id, str) else None
