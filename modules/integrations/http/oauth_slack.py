"""OAuth Slack route handlers."""

import os

from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
    _state_bool,
    _state_string,
    _state_strings,
)


class OAuthSlackMixin(OAuthContextBase):
    """Implement oauth slack OAuth route operations."""

    def slack_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the slack callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "slack")

            return self.finish("slack", "failed")

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
            return self.finish("slack", "failed")

        if payload.get("ok") is not True:
            return self.finish("slack", "failed")

        user = payload.get("authed_user") or {}

        access_token = user.get("access_token") if isinstance(user, dict) else None

        if not isinstance(access_token, str):
            return self.finish("slack", "failed")

        bundle = self.token_bundle("slack", user, access_token=access_token)

        try:
            self.dependencies.connection_service.create_slack_oauth_installation(
                display_name=_state_string(record, "display_name"),
                token_bundle=bundle,
                channels=_state_strings(record, "channels"),
                include_thread_replies=_state_bool(record, "include_thread_replies"),
                requested_by_principal_id=record.principal_id,
            )

        except Exception:
            return self.finish("slack", "failed")

        return self.finish("slack", "success")
