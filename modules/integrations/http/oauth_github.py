"""OAuth GitHub route handlers."""

import logging
import os
from typing import Any

import httpx
from fastapi import HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.integrations.http.oauth_context import (
    OAuthContextBase,
)

logger = logging.getLogger(__name__)


class OAuthGitHubMixin(OAuthContextBase):
    """Implement oauth github OAuth route operations."""

    def github_callback(
        self, code: str | None = None, state: str = Query(...), error: str | None = None
    ) -> RedirectResponse:
        """Handle the github callback step of the OAuth flow."""
        if error or not code:
            self.discard_state(state, "github")
            return self.finish("github", "failed", "authorization_denied")

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
            return self.finish("github", "failed", "token_exchange")

        user_token = payload.get("access_token")

        if not isinstance(user_token, str):
            return self.finish("github", "failed", "provider_response")

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
        if setup_action not in {"install", "update"}:
            logger.warning("GitHub OAuth installation rejected: setup action mismatch")
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed", "installation")
        self.request_as(record)
        if not record.secret_ref:
            logger.warning(
                "GitHub OAuth installation rejected: temporary user token missing"
            )
            return self.finish("github", "failed", "provider_response")
        user_token = self.dependencies.secret_store.get(record.secret_ref)
        try:
            installations = self._github_collection(
                "https://api.github.com/user/installations?per_page=100",
                user_token,
                "installations",
            )
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            logger.warning(
                "GitHub OAuth installation rejected: user installations lookup failed "
                "exception=%s status=%s",
                type(exc).__name__,
                status,
            )
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed", "installation")
        if not _owns_installation(installations, installation_id):
            logger.warning(
                "GitHub OAuth installation rejected: installation is not available "
                "to the authorizing user"
            )
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed", "installation")
        try:
            repositories = self._github_installation_repositories(installation_id)
            setup = self.state_record(
                "github",
                {
                    **record.configuration,
                    "github_installation_id": installation_id,
                    "resources": repositories,
                    "account_login": _installation_account(
                        installations, installation_id
                    ),
                },
                step="resource_selection",
            )
        except Exception as exc:
            logger.warning(
                "GitHub OAuth installation rejected while validating repository access "
                "exception=%s code=%s",
                type(exc).__name__,
                getattr(exc, "code", "unknown"),
            )
            self._delete_temporary_secret(record.secret_ref)
            return self.finish("github", "failed", "resource_discovery")
        self._delete_temporary_secret(record.secret_ref)
        return self.setup_redirect("github", setup.oauth_state_id)

    def _github_installation_repositories(
        self, installation_id: str
    ) -> list[dict[str, str]]:
        """List repositories granted to the just-installed GitHub App."""
        token = (
            self.dependencies.github_app_token_provider.installation_discovery_token(
                installation_id
            )
        )
        rows = self._github_collection(
            "https://api.github.com/installation/repositories?per_page=100",
            token,
            "repositories",
        )
        return [
            {
                "full_name": item["full_name"],
                "name": item.get("name", item["full_name"]),
            }
            for item in rows
            if isinstance(item, dict) and isinstance(item.get("full_name"), str)
        ][:500]

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


def _installation_account(rows: list[Any], installation_id: str) -> str | None:
    """Return the GitHub account that owns the authorized installation."""
    installation = next(
        (
            item
            for item in rows
            if isinstance(item, dict) and str(item.get("id")) == installation_id
        ),
        None,
    )
    account = installation.get("account") if isinstance(installation, dict) else None
    login = account.get("login") if isinstance(account, dict) else None
    return login if isinstance(login, str) else None
