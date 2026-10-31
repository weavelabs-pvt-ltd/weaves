"""Plugin installation and MCP discovery API routes."""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from contracts.v1 import PluginInstallationStatus
from modules.integrations.admin_service import IntegrationAdminService
from modules.integrations.connection_service import IntegrationConnectionService
from modules.models.http.credentials import ProviderCredentialRequest
from shared.errors import PluginGatewayError, SecretStoreUnavailable


class PluginInstallationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled_capability_ids: tuple[str, ...] | None = None
    status: PluginInstallationStatus | None = None


class GitHubInstallationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plugin_id: Literal["github"]
    display_name: str = Field(min_length=2, max_length=160)
    api_token: SecretStr = Field(min_length=1, max_length=4096)
    repositories: tuple[str, ...] = Field(min_length=1, max_length=50)


class SlackInstallationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plugin_id: Literal["slack"]
    display_name: str = Field(min_length=2, max_length=160)
    user_token: SecretStr = Field(min_length=1, max_length=4096)
    channels: tuple[str, ...] = Field(min_length=1, max_length=50)
    include_thread_replies: bool = False


class JiraInstallationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plugin_id: Literal["jira"]
    display_name: str = Field(min_length=2, max_length=160)
    api_token: SecretStr = Field(min_length=1, max_length=4096)
    site_url: str = Field(
        min_length=12,
        max_length=256,
        pattern=r"^https://[A-Za-z0-9-]+\.atlassian\.net/?$",
    )
    email: str = Field(min_length=3, max_length=320)
    project_keys: tuple[str, ...] = Field(min_length=1, max_length=50)


class McpToolDiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    endpoint_url: str = Field(min_length=12, max_length=2048)
    bearer_token: SecretStr | None = Field(default=None, max_length=4096)


class McpInstallationCreateRequest(McpToolDiscoverRequest):
    plugin_id: Literal["mcp"]
    display_name: str = Field(min_length=2, max_length=160)
    read_only_tool_names: tuple[str, ...] = Field(min_length=1, max_length=100)


def _installation_view(
    installation: Any, dump: Callable[[BaseModel], dict[str, Any]]
) -> dict[str, Any]:
    result = dump(installation)
    result.pop("auth_ref", None)
    configuration = result.get("configuration") or {}
    auth_method = (
        configuration.get("auth_method") if isinstance(configuration, dict) else None
    )
    result["authentication_method"] = auth_method or (
        "token" if installation.auth_ref else "none"
    )
    result["has_credentials"] = (
        bool(installation.auth_ref) or auth_method == "github_app"
    )
    return result


def register_plugin_routes(
    app: FastAPI,
    integration_admin_service: IntegrationAdminService,
    integration_connection_service: IntegrationConnectionService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register plugin installation, credential, health, and MCP endpoints."""
    _register_plugin_routes_group_1(app, integration_admin_service, dump)
    _register_plugin_routes_group_2(app, integration_connection_service, dump)
    _register_plugin_routes_group_3(app, integration_admin_service, dump)


def _register_plugin_routes_group_1(
    app: FastAPI,
    integration_admin_service: IntegrationAdminService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register plugin installation, credential, health, and MCP endpoints."""
    _dump = dump

    def installation_view(installation: Any) -> dict[str, Any]:
        return _installation_view(installation, _dump)

    def list_plugin_installations() -> list[dict[str, Any]]:
        return [
            installation_view(item)
            for item in integration_admin_service.list_plugin_installations()
        ]

    def discover_mcp_tools(request: McpToolDiscoverRequest) -> list[dict[str, Any]]:
        try:
            return list(
                integration_admin_service.discover_mcp_tools(
                    endpoint_url=request.endpoint_url,
                    bearer_token=(
                        request.bearer_token.get_secret_value()
                        if request.bearer_token
                        else None
                    ),
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    app.get("/api/v0/plugin-installations")(list_plugin_installations)
    app.post("/api/v0/mcp/discover")(discover_mcp_tools)


def _register_plugin_routes_group_2(
    app: FastAPI,
    integration_connection_service: IntegrationConnectionService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register plugin installation, credential, health, and MCP endpoints."""
    _dump = dump

    def installation_view(installation: Any) -> dict[str, Any]:
        return _installation_view(installation, _dump)

    def create_plugin_installation(
        request: GitHubInstallationCreateRequest
        | SlackInstallationCreateRequest
        | JiraInstallationCreateRequest
        | McpInstallationCreateRequest,
    ) -> dict[str, Any]:
        try:
            if isinstance(request, GitHubInstallationCreateRequest):
                installation = (
                    integration_connection_service.create_github_installation(
                        display_name=request.display_name,
                        api_token=request.api_token.get_secret_value(),
                        repositories=request.repositories,
                    )
                )
            elif isinstance(request, SlackInstallationCreateRequest):
                installation = integration_connection_service.create_slack_installation(
                    display_name=request.display_name,
                    user_token=request.user_token.get_secret_value(),
                    channels=request.channels,
                    include_thread_replies=request.include_thread_replies,
                )
            elif isinstance(request, McpInstallationCreateRequest):
                installation = integration_connection_service.create_mcp_installation(
                    display_name=request.display_name,
                    endpoint_url=request.endpoint_url,
                    bearer_token=(
                        request.bearer_token.get_secret_value()
                        if request.bearer_token
                        else None
                    ),
                    read_only_tool_names=request.read_only_tool_names,
                )
            else:
                installation = integration_connection_service.create_jira_installation(
                    display_name=request.display_name,
                    api_token=request.api_token.get_secret_value(),
                    site_url=request.site_url,
                    email=request.email,
                    project_keys=request.project_keys,
                )
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return installation_view(installation)

    app.post("/api/v0/plugin-installations", status_code=201)(
        create_plugin_installation
    )


def _register_plugin_routes_group_3(
    app: FastAPI,
    integration_admin_service: IntegrationAdminService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register plugin installation, credential, health, and MCP endpoints."""
    _dump = dump

    def installation_view(installation: Any) -> dict[str, Any]:
        return _installation_view(installation, _dump)

    def rotate_plugin_credential(
        installation_id: str, request: ProviderCredentialRequest
    ) -> dict[str, Any]:
        try:
            installation = integration_admin_service.rotate_plugin_credential(
                installation_id, request.api_key.get_secret_value()
            )
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Plugin installation not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return installation_view(installation)

    def healthcheck_plugin_installation(installation_id: str) -> dict[str, str]:
        try:
            details = integration_admin_service.healthcheck_plugin_installation(
                installation_id
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Plugin installation not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PluginGatewayError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        return {"status": "ok", **details}

    def update_plugin_installation(
        installation_id: str, patch: PluginInstallationPatchRequest
    ) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=400, detail="No changes supplied")
        try:
            installation = integration_admin_service.update_plugin_installation(
                installation_id, **changes
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Plugin installation not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return installation_view(installation)

    app.put("/api/v0/plugin-installations/{installation_id}/credential")(
        rotate_plugin_credential
    )
    app.post("/api/v0/plugin-installations/{installation_id}/healthcheck")(
        healthcheck_plugin_installation
    )
    app.patch("/api/v0/plugin-installations/{installation_id}")(
        update_plugin_installation
    )
