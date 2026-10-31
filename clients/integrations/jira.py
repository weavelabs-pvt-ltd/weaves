"""Read-only Jira Cloud issue search scoped to configured projects."""

from __future__ import annotations

import re
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from contracts.v1 import PluginInstallation
from contracts.v1.json_data import thaw_json_value
from shared.errors import PluginGatewayError


class _Resolver(Protocol):
    def resolve(self, auth_ref: str) -> str: ...


class JiraReadPlugin:
    """Search Jira issues using a read-only API token and project allowlist."""

    def __init__(
        self,
        secret_resolver: _Resolver,
        client: httpx.Client,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._secrets = secret_resolver
        self._client = client
        self._timeout_seconds = timeout_seconds

    def healthcheck(self, installation: PluginInstallation) -> dict[str, str]:
        configuration = thaw_json_value(installation.configuration)
        project_keys = self._project_keys(configuration)
        api_base_url = self._api_base_url(configuration)
        email = configuration.get("email")
        if configuration.get("auth_method") != "atlassian_3lo" and (
            not isinstance(email, str) or "@" not in email
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira account email is invalid"
            )
        api_token = self._resolve_token(installation)
        project = self._request(
            f"{api_base_url}/rest/api/3/project/{project_keys[0]}",
            email if isinstance(email, str) else None,
            api_token,
            bearer=configuration.get("auth_method") == "atlassian_3lo",
            timeout=self._timeout_seconds,
        )
        key = project.get("key")
        if not isinstance(key, str) or not key:
            raise PluginGatewayError(
                "plugin.invalid_response", "Jira returned an invalid project"
            )
        return {"project": key}

    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> dict[str, Any]:
        if capability_id == "jira.issues.comment":
            return self._post_comment(payload, installation)
        if capability_id != "jira.issues.search":
            raise PluginGatewayError(
                "plugin.capability_unknown", "Jira capability is not supported"
            )
        configuration = thaw_json_value(installation.configuration)
        project_keys = self._project_keys(configuration)
        site_url = self._site_url(configuration)
        api_base_url = self._api_base_url(configuration)
        email = configuration.get("email")
        is_oauth = configuration.get("auth_method") == "atlassian_3lo"
        if not is_oauth and (not isinstance(email, str) or "@" not in email):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira account email is invalid"
            )
        query = " ".join(payload["query"].split())
        if not query or len(query) > 500:
            raise PluginGatewayError(
                "plugin.input_invalid", "Jira search query is invalid"
            )
        escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')
        project_scope = ", ".join(project_keys)
        jql = f'project in ({project_scope}) AND text ~ "\\"{escaped_query}\\""'
        token = self._resolve_token(installation)
        deadline = monotonic() + self._timeout_seconds
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise PluginGatewayError("plugin.timeout", "Jira request timed out")
        result = self._request(
            f"{api_base_url}/rest/api/3/search/jql",
            email if isinstance(email, str) else None,
            token,
            bearer=is_oauth,
            body={
                "jql": jql,
                "maxResults": 5,
                "fields": ["summary", "description", "status", "project", "issuetype"],
            },
            timeout=min(10.0, remaining),
        )
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            raise PluginGatewayError(
                "plugin.invalid_response", "Jira returned an invalid search response"
            )
        return {"documents": _jira_documents(issues, project_keys, site_url)}

    def _post_comment(
        self, payload: dict[str, Any], installation: PluginInstallation
    ) -> dict[str, Any]:
        configuration = thaw_json_value(installation.configuration)
        project_keys = self._project_keys(configuration)
        site_url = self._site_url(configuration)
        api_base_url = self._api_base_url(configuration)
        email = configuration.get("email")
        is_oauth = configuration.get("auth_method") == "atlassian_3lo"
        issue_key = payload.get("issue_key")
        comment = payload.get("comment")
        if not is_oauth and (not isinstance(email, str) or "@" not in email):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira account email is invalid"
            )
        issue_match = (
            re.fullmatch(r"([A-Z][A-Z0-9_]{0,49})-([1-9][0-9]{0,8})", issue_key)
            if isinstance(issue_key, str)
            else None
        )
        if issue_match is None or issue_match.group(1) not in project_keys:
            raise PluginGatewayError(
                "plugin.action_scope_denied",
                "Jira issue is outside the installation project allowlist",
            )
        if not isinstance(comment, str) or not comment.strip() or len(comment) > 4000:
            raise PluginGatewayError(
                "plugin.input_invalid", "Jira comment must contain 1 to 4000 characters"
            )
        token = self._resolve_token(installation)
        result = self._request(
            f"{api_base_url}/rest/api/3/issue/{issue_key}/comment",
            email if isinstance(email, str) else None,
            token,
            bearer=is_oauth,
            body={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment.strip()}],
                        }
                    ],
                }
            },
            timeout=self._timeout_seconds,
        )
        comment_id = result.get("id")
        if not isinstance(comment_id, (str, int)):
            raise PluginGatewayError(
                "plugin.invalid_response", "Jira returned an invalid comment response"
            )
        return {
            "comment_id": str(comment_id),
            "issue_url": f"{site_url}/browse/{issue_key}",
        }

    def _request(
        self,
        url: str,
        email: str | None,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float,
        bearer: bool = False,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {token}"
        auth: tuple[str, str] | None = None
        if not bearer:
            if email is None:
                raise PluginGatewayError(
                    "plugin.configuration_invalid", "Jira account email is required"
                )
            auth = (email, token)
        try:
            response = self._client.request(
                "POST" if body is not None else "GET",
                url,
                json=body,
                auth=auth,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise PluginGatewayError(
                "plugin.timeout", "Jira request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "Jira request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.upstream_error", f"Jira returned HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "Jira returned an invalid response"
            ) from exc
        if not isinstance(result, dict):
            raise PluginGatewayError(
                "plugin.invalid_response", "Jira returned an invalid response"
            )
        return result

    def _resolve_token(self, installation: PluginInstallation) -> str:
        if not installation.auth_ref:
            raise PluginGatewayError(
                "plugin.credential_missing", "Jira installation has no credential"
            )
        try:
            resolver = getattr(self._secrets, "resolve_for_plugin", None)
            token = (
                resolver(installation.auth_ref, "jira")
                if callable(resolver)
                else self._secrets.resolve(installation.auth_ref)
            )
        except Exception as exc:
            raise PluginGatewayError(
                "plugin.credential_unavailable", "Jira credential could not be resolved"
            ) from exc
        if not token:
            raise PluginGatewayError(
                "plugin.credential_unavailable", "Jira credential could not be resolved"
            )
        return token

    @classmethod
    def _api_base_url(cls, configuration: dict[str, Any]) -> str:
        if configuration.get("auth_method") != "atlassian_3lo":
            return cls._site_url(configuration)
        cloud_id = configuration.get("cloud_id")
        if not isinstance(cloud_id, str) or not re.fullmatch(
            r"[A-Za-z0-9-]{8,80}", cloud_id
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira cloud ID is invalid"
            )
        return f"https://api.atlassian.com/ex/jira/{cloud_id}"

    @staticmethod
    def _site_url(configuration: dict[str, Any]) -> str:
        site_url = configuration.get("site_url")
        if not isinstance(site_url, str):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira site URL is invalid"
            )
        parsed = urlparse(site_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".atlassian.net")
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "Jira site must be an https://<site>.atlassian.net URL",
            )
        return f"https://{parsed.hostname}"

    @staticmethod
    def _project_keys(configuration: dict[str, Any]) -> list[str]:
        project_keys = configuration.get("project_keys", [])
        if (
            not isinstance(project_keys, list)
            or not project_keys
            or len(project_keys) > 50
            or any(
                not isinstance(key, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", key) is None
                for key in project_keys
            )
            or len(project_keys) != len(set(project_keys))
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Jira project allowlist is invalid"
            )
        return project_keys


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_plain_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text")
        if value.get("type") == "text" and isinstance(text, str):
            return text
        return " ".join(_plain_text(item) for item in value.values())
    return ""


def _jira_documents(
    issues: list[Any], project_keys: list[str], site_url: str
) -> list[dict[str, str]]:
    """Convert allowlisted Jira search issues to bounded context documents."""
    documents: list[dict[str, str]] = []
    for issue in issues[:5]:
        if not isinstance(issue, dict):
            continue
        key = issue.get("key")
        fields = issue.get("fields")
        if not isinstance(key, str) or not isinstance(fields, dict):
            continue
        project = fields.get("project") or {}
        project_key = project.get("key") if isinstance(project, dict) else None
        if project_key not in project_keys:
            continue
        summary = fields.get("summary")
        if not isinstance(summary, str):
            summary = key
        status = fields.get("status") or {}
        status_name = status.get("name") if isinstance(status, dict) else None
        issue_type = fields.get("issuetype") or {}
        issue_type_name = (
            issue_type.get("name") if isinstance(issue_type, dict) else None
        )
        metadata = ", ".join(
            value for value in (status_name, issue_type_name) if isinstance(value, str)
        )
        documents.append(
            {
                "source_id": f"jira:{key}",
                "title": f"{key}: {summary}"[:160],
                "text": (
                    f"Project: {project_key}. {metadata}. "
                    f"URL: {site_url}/browse/{key}.\n"
                    f"{_plain_text(fields.get('description'))[:1500]}"
                )[:2000],
            }
        )
    return documents
