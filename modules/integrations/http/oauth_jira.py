"""OAuth Jira route handlers."""

import os
from typing import cast

from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
    OAuthResourceDiscovery,
    _state_string,
)


class OAuthJiraMixin(OAuthContextBase):
    """Implement oauth jira OAuth route operations."""

    def jira_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the jira callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "jira")
            return self.finish("jira", "failed", "authorization_denied")

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
            return self.finish("jira", "failed", "token_exchange")

        access = payload.get("access_token")

        if not isinstance(access, str):
            return self.finish("jira", "failed", "provider_response")

        try:
            bundle = self.token_bundle("jira", payload)
            resources = cast(OAuthResourceDiscovery, self).jira_sites(access)
        except HTTPException:
            return self.finish("jira", "failed", "resource_discovery")
        if not resources:
            return self.finish("jira", "failed", "no_resources")
        try:
            secret_ref = self.dependencies.secret_store.put(bundle)
            try:
                setup = self.state_record(
                    "jira",
                    {
                        "display_name": _state_string(record, "display_name"),
                        "sites": resources,
                    },
                    step="resource_selection",
                    secret_ref=secret_ref,
                )
            except Exception:
                self.dependencies.secret_store.delete(secret_ref)
                raise
        except Exception:
            return self.finish("jira", "failed", "connection_setup")
        return self.setup_redirect("jira", setup.oauth_state_id)
