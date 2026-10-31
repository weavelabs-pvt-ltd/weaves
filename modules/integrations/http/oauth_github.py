"""OAuth GitHub route handlers."""

import os
from typing import Any

import httpx
from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from contracts.v1 import OAuthAuthorizationState
from modules.integrations.http.oauth_context import (
    OAuthContextBase,
    _state_string,
    _state_strings,
)


class OAuthGitHubMixin(OAuthContextBase):
    """Implement oauth github OAuth route operations."""

    def github_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the github callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "github")

            return self.finish("github", "failed")

        record = self.take_state(state, "github")

        self.request_as(record)

        app_id = os.environ["WEAVES_GITHUB_APP_CLIENT_ID"].strip()

        app_secret = os.environ["WEAVES_GITHUB_APP_CLIENT_SECRET"].strip()

        try:
            payload = self.exchange(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": code,
                    "redirect_uri": self.callback_url("github/callback"),
                    "state": state,
                },
            )

        except HTTPException:
            return self.finish("github", "failed")

        user_token = payload.get("access_token")

        if not isinstance(user_token, str):
            return self.finish("github", "failed")

        secret_ref = self.dependencies.secret_store.put(user_token)

        try:
            install_state = self.state_record(
                "github",
                dict(record.configuration),
                step="github_install",
                secret_ref=secret_ref,
            )

        except Exception:
            self.dependencies.secret_store.delete(secret_ref)

            return self.finish("github", "failed")

        slug = os.environ["WEAVES_GITHUB_APP_SLUG"].strip()

        return RedirectResponse(
            f"https://github.com/apps/{slug}/installations/new?state={install_state.oauth_state_id}",
            status_code=303,
        )

    def github_installation(
        self,
        installation_id: str = Query(...),
        state: str = Query(...),
        setup_action: str = Query(default="install"),
    ) -> RedirectResponse:
        """Validate installation ownership and save the selected repositories."""
        record = self.take_state(state, "github", "github_install")
        if setup_action != "install":
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed")
        self.request_as(record)
        if not record.secret_ref:
            return self.finish("github", "failed")
        user_token = self.dependencies.secret_store.get(record.secret_ref)
        try:
            installations = self._github_collection(
                "https://api.github.com/user/installations?per_page=100",
                user_token,
                "installations",
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed")
        if not _owns_installation(installations, installation_id):
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed")
        repositories = _state_strings(record, "repositories")
        try:
            self._save_github_installation(record, installation_id, repositories)
        except Exception:
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed")
        self._delete_temporary_secret(record.secret_ref)
        return self.finish("github", "success")

    def _save_github_installation(
        self,
        record: OAuthAuthorizationState,
        installation_id: str,
        repositories: tuple[str, ...],
    ) -> None:
        """Verify the GitHub App allowlist before saving its installation."""
        token = self.dependencies.github_app_token_provider.installation_token(
            installation_id, repositories
        )
        rows = self._github_collection(
            "https://api.github.com/installation/repositories?per_page=100",
            token,
            "repositories",
        )
        available = {item.get("full_name") for item in rows if isinstance(item, dict)}
        if not set(repositories).issubset(available):
            raise ValueError("requested repo outside installation")
        self.dependencies.connection_service.create_github_app_installation(
            display_name=_state_string(record, "display_name"),
            github_installation_id=installation_id,
            repositories=repositories,
            requested_by_principal_id=record.principal_id,
        )

    def _github_collection(
        self, url: str, token: str, collection_key: str
    ) -> list[Any]:
        """Fetch every page of a GitHub installation collection."""
        rows: list[Any] = []
        while url:
            response = self.client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=15,
            )
            response.raise_for_status()
            rows.extend(response.json().get(collection_key, []))
            next_page = response.links.get("next")
            url = next_page["url"] if next_page else ""
        return rows

    def _delete_temporary_secret(self, secret_ref: str | None) -> None:
        """Delete the temporary user token when a GitHub flow ends."""
        if secret_ref:
            self.dependencies.secret_store.delete(secret_ref)


def _owns_installation(rows: list[Any], installation_id: str) -> bool:
    """Return whether GitHub reports the requested installation for the user."""
    return any(
        str(item.get("id")) == installation_id
        for item in rows
        if isinstance(item, dict)
    )
