"""Post-authorization resource discovery and connector setup completion."""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import OAuthAuthorizationState
from modules.integrations.http.oauth_context import OAuthContextBase


class OAuthResourceSelection(BaseModel):
    """Provider resources selected from the discovered account inventory."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    resources: tuple[str, ...] = Field(min_length=1, max_length=50)
    site_id: str | None = Field(default=None, max_length=128)


class OAuthResourceMixin(OAuthContextBase):
    """Serve protected resource selection endpoints for browser OAuth flows."""

    def oauth_setup(self, state_id: str) -> dict[str, Any]:
        """Return discovered resources for a pending connector setup."""
        try:
            record = self.dependencies.state_service.get_resource_selection(state_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "provider": record.provider,
            "display_name": record.configuration.get("display_name", ""),
            "account_login": record.configuration.get("account_login", ""),
            "resources": record.configuration.get("resources", []),
            "tools": record.configuration.get("resources", [])
            if record.provider == "mcp"
            else [],
            "sites": record.configuration.get("sites", []),
            "include_thread_replies": record.configuration.get(
                "include_thread_replies", False
            ),
        }

    def complete_oauth_setup(
        self, state_id: str, request: OAuthResourceSelection
    ) -> dict[str, Any]:
        """Validate selected resources and create the scoped installation."""
        consumed = None
        try:
            record = self.dependencies.state_service.get_resource_selection(state_id)
            self.dependencies.state_service.authorize_state_principal(record)
            selected = self._validate_selection(record, request)
            consumed = self.dependencies.state_service.consume_state(
                state_id, record.provider, "resource_selection"
            )
            installation = self._save_installation(consumed, request, selected)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            if consumed and consumed.secret_ref:
                self.dependencies.secret_store.delete(consumed.secret_ref)
        view = installation.model_dump(mode="json")
        view.pop("auth_ref", None)
        view["authentication_method"] = (
            "github_app" if record.provider == "github" else f"{record.provider}_oauth"
        )
        view["has_credentials"] = record.provider == "github" or bool(
            consumed and consumed.secret_ref
        )
        return {"installation": view}

    def _validate_selection(
        self, record: OAuthAuthorizationState, request: OAuthResourceSelection
    ) -> tuple[str, ...]:
        offered_value = record.configuration.get("resources")
        offered = offered_value if isinstance(offered_value, list) else []
        if record.provider == "jira":
            sites_value = record.configuration.get("sites")
            sites = sites_value if isinstance(sites_value, list) else []
            site = next(
                (
                    item
                    for item in sites
                    if isinstance(item, dict) and item.get("id") == request.site_id
                ),
                None,
            )
            if site is None:
                raise ValueError("Choose an Atlassian site available to this account")
            projects = site.get("projects")
            offered = projects if isinstance(projects, list) else []
        names = {
            item.get("full_name") or item.get("key") or item.get("name")
            for item in offered
            if isinstance(item, dict)
        }
        if (
            not request.resources
            or len(set(request.resources)) != len(request.resources)
            or not set(request.resources).issubset(names)
        ):
            raise ValueError("Select resources from the provider's available list")
        return request.resources

    def _save_installation(
        self,
        record: OAuthAuthorizationState,
        request: OAuthResourceSelection,
        selected: tuple[str, ...],
    ) -> Any:
        config = record.configuration
        if record.provider == "github":
            return self.dependencies.connection_service.create_github_app_installation(
                display_name=str(config["display_name"]),
                github_installation_id=str(config["github_installation_id"]),
                repositories=selected,
                account_login=_config_string(config, "account_login"),
                requested_by_principal_id=record.principal_id,
            )
        if not record.secret_ref:
            raise ValueError("Provider credentials expired; reconnect this tool")
        bundle = self.dependencies.secret_store.get(record.secret_ref)
        if record.provider == "slack":
            return self.dependencies.connection_service.create_slack_oauth_installation(
                display_name=str(config["display_name"]),
                token_bundle=bundle,
                channels=selected,
                team_id=_config_string(config, "team_id"),
                team_name=_config_string(config, "team_name"),
                include_thread_replies=bool(config.get("include_thread_replies")),
                requested_by_principal_id=record.principal_id,
            )
        if record.provider == "mcp":
            return self.dependencies.connection_service.create_mcp_installation(
                display_name=str(config["display_name"]),
                endpoint_url=str(config["endpoint"]),
                read_only_tool_names=selected,
                oauth_token_bundle=self.dependencies.secret_store.get(
                    str(record.secret_ref)
                ),
                catalog_connector_id=(
                    _config_string(config, "catalog_connector_id") or "custom-mcp"
                ),
                requested_by_principal_id=record.principal_id,
            )
        sites_value = config.get("sites")
        sites = sites_value if isinstance(sites_value, list) else []
        site = next(
            item
            for item in sites
            if isinstance(item, dict) and item.get("id") == request.site_id
        )
        return self.dependencies.connection_service.create_jira_oauth_installation(
            display_name=str(config["display_name"]),
            token_bundle=bundle,
            site_url=str(site["url"]),
            cloud_id=str(site["id"]),
            project_keys=selected,
            requested_by_principal_id=record.principal_id,
        )

    def slack_resources(self, token: str) -> list[dict[str, Any]]:
        """List public and private Slack channels visible to the OAuth user."""
        result: list[dict[str, Any]] = []
        cursor = ""
        while True:
            try:
                response = self.client.get(
                    "https://slack.com/api/conversations.list",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "limit": 200,
                        "cursor": cursor,
                        "types": "public_channel,private_channel",
                    },
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(
                    status_code=502, detail="Slack channel discovery failed"
                ) from exc
            if payload.get("ok") is not True:
                raise HTTPException(
                    status_code=502,
                    detail="Slack could not list accessible channels",
                )
            result.extend(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "is_private": bool(item.get("is_private")),
                }
                for item in payload.get("channels", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("name"), str)
            )
            cursor = (payload.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor or len(result) >= 500:
                return result[:500]

    def jira_sites(self, token: str) -> list[dict[str, Any]]:
        """Discover accessible Jira sites and their visible projects."""
        try:
            response = self.client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=20,
            )
            response.raise_for_status()
            resources = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail="Jira site discovery failed"
            ) from exc
        sites: list[dict[str, Any]] = []
        for site in resources[:20]:
            if not isinstance(site, dict) or not all(
                isinstance(site.get(key), str) for key in ("id", "url", "name")
            ):
                continue
            projects = self._jira_projects(token, site["id"])
            sites.append(
                {
                    "id": site["id"],
                    "url": site["url"],
                    "name": site["name"],
                    "projects": projects,
                }
            )
        return sites

    def _jira_projects(self, token: str, cloud_id: str) -> list[dict[str, str]]:
        """List project keys accessible to the authorized Jira account."""
        projects: list[dict[str, str]] = []
        start_at = 0
        while len(projects) < 500:
            try:
                response = self.client.get(
                    f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/search",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    params={"maxResults": 100, "startAt": start_at},
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(
                    status_code=502, detail="Jira project discovery failed"
                ) from exc
            values = payload.get("values", [])
            projects.extend(
                {
                    "key": item["key"],
                    "name": (
                        item["name"]
                        if isinstance(item.get("name"), str)
                        else item["key"]
                    ),
                }
                for item in values
                if isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", item["key"])
            )
            if not values or len(values) < 100 or payload.get("isLast") is True:
                break
            start_at += len(values)
        return projects[:500]


def _config_string(configuration: dict[str, Any], key: str) -> str | None:
    """Return a validated optional string from OAuth state configuration."""
    value = configuration.get(key)
    return value if isinstance(value, str) else None
