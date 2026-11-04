"""OAuth Slack route handlers."""

import os
from typing import Any, cast

from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
    OAuthResourceDiscovery,
    _state_bool,
    _state_string,
)


class OAuthSlackMixin(OAuthContextBase):
    """Implement oauth slack OAuth route operations."""

    def slack_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the slack callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "slack")
            return self.finish("slack", "failed", "authorization_denied")

        record = self.take_state(state, "slack")

        self.request_as(record)

        client_id = os.environ["WEAVES_SLACK_OAUTH_CLIENT_ID"].strip()

        client_secret = os.environ["WEAVES_SLACK_OAUTH_CLIENT_SECRET"].strip()

        redirect = self.callback_url("slack/callback")

        try:
            payload = self.exchange(
                "https://slack.com/api/oauth.v2.access",
                data={"code": code, "redirect_uri": redirect},
                auth=(client_id, client_secret),
            )

        except HTTPException:
            return self.finish("slack", "failed", "token_exchange")

        if payload.get("ok") is not True:
            return self.finish("slack", "failed", "provider_response")

        user = payload.get("authed_user") or {}

        access_token = user.get("access_token") if isinstance(user, dict) else None

        if not isinstance(access_token, str):
            return self.finish("slack", "failed", "provider_response")

        bundle = self.token_bundle("slack", user, access_token=access_token)

        try:
            resources = cast(OAuthResourceDiscovery, self).slack_resources(access_token)
        except HTTPException:
            return self.finish("slack", "failed", "resource_discovery")
        if not resources:
            return self.finish("slack", "failed", "no_resources")
        try:
            secret_ref = self.dependencies.secret_store.put(bundle)
            try:
                setup = self.state_record(
                    "slack",
                    {
                        "display_name": _state_string(record, "display_name"),
                        "resources": resources,
                        "include_thread_replies": _state_bool(
                            record, "include_thread_replies"
                        ),
                        "team_id": _nested_string(payload, "team", "id"),
                        "team_name": _nested_string(payload, "team", "name"),
                    },
                    step="resource_selection",
                    secret_ref=secret_ref,
                )
            except Exception:
                self.dependencies.secret_store.delete(secret_ref)
                raise
        except Exception:
            return self.finish("slack", "failed", "connection_setup")
        return self.setup_redirect("slack", setup.oauth_state_id)


def _nested_string(payload: dict[str, Any], parent: str, key: str) -> str | None:
    """Return a nested provider string field when its shape is valid."""
    value = payload.get(parent)
    field = value.get(key) if isinstance(value, dict) else None
    return field if isinstance(field, str) else None
