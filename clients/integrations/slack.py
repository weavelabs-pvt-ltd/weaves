"""Read-only Slack message search scoped to configured channel names."""

from __future__ import annotations

import re
from time import monotonic
from typing import Any, Protocol

import httpx

from contracts.v1 import PluginInstallation
from contracts.v1.json_data import thaw_json_value
from shared.errors import PluginGatewayError


class _Resolver(Protocol):
    def resolve_for_plugin(self, auth_ref: str, provider: str) -> str: ...


class SlackReadPlugin:
    """Search messages visible to a Slack OAuth grant in allowed channels."""

    def __init__(
        self,
        secret_resolver: _Resolver,
        client: httpx.Client,
        *,
        api_base_url: str = "https://slack.com/api",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._secrets = secret_resolver
        self._client = client
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def healthcheck(self, installation: PluginInstallation) -> dict[str, str]:
        token = self._resolve_token(installation)
        data = self._request("auth.test", token)
        team = data.get("team")
        if not isinstance(team, str) or not team:
            raise PluginGatewayError(
                "plugin.invalid_response", "Slack returned an invalid response"
            )
        return {"workspace": team}

    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> dict[str, Any]:
        if capability_id != "slack.messages.search":
            raise PluginGatewayError(
                "plugin.capability_unknown", "Slack capability is not supported"
            )
        configuration = thaw_json_value(installation.configuration)
        channels = configuration.get("channels", [])
        include_thread_replies = configuration.get("include_thread_replies") is True
        if not channels or any(
            not isinstance(channel, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", channel) is None
            for channel in channels
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid", "Slack channel configuration is invalid"
            )
        query = " ".join(payload["query"].split())
        if not query:
            raise PluginGatewayError(
                "plugin.input_invalid", "Slack search query is invalid"
            )
        if re.search(r"(?i)\b(?:in|from|to|before|after|during):", query):
            raise PluginGatewayError(
                "plugin.query_scope_denied",
                "Channel scope is controlled by the installation",
            )
        token = self._resolve_token(installation)
        # Quoting the query prevents Slack search operators from widening the
        # configured `in:` channel scope.
        phrase = query.replace("\\", "\\\\").replace('"', '\\"')
        deadline = monotonic() + self._timeout_seconds
        documents, candidate = self._search_documents(
            channels, phrase, token, deadline, include_thread_replies
        )
        self._append_thread_context(documents, candidate, token, deadline)
        return {"documents": documents}

    def _search_documents(
        self,
        channels: list[Any],
        phrase: str,
        token: str,
        deadline: float,
        include_thread_replies: bool,
    ) -> tuple[list[dict[str, str]], tuple[int, str, str, str] | None]:
        """Search each allowed channel and return one optional thread candidate."""
        documents: list[dict[str, str]] = []
        candidate = None
        for channel in channels:
            remaining = deadline - monotonic()
            if remaining <= 0 or len(documents) >= 5:
                break
            data = self._request(
                "search.messages",
                token,
                params={"query": f'in:{channel} "{phrase}"', "count": 5},
                timeout=min(5.0, remaining),
            )
            messages = data.get("messages", {}).get("matches", [])
            for item in messages:
                if not isinstance(item, dict):
                    continue
                found_channel = item.get("channel")
                if (
                    not isinstance(found_channel, dict)
                    or str(found_channel.get("name", "")).casefold()
                    != channel.casefold()
                ):
                    continue
                document, thread = _slack_document(item, channel)
                if document is None:
                    continue
                documents.append(document)
                if include_thread_replies and candidate is None and thread is not None:
                    candidate = (len(documents) - 1, thread[0], thread[1], channel)
                if len(documents) >= 5:
                    break
        return documents, candidate

    def _append_thread_context(
        self,
        documents: list[dict[str, str]],
        candidate: tuple[int, str, str, str] | None,
        token: str,
        deadline: float,
    ) -> None:
        """Add replies for the first matching thread while time remains."""
        if candidate is None:
            return
        document_index, channel_id, thread_timestamp, channel_name = candidate
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        try:
            data = self._request(
                "conversations.replies",
                token,
                params={"channel": channel_id, "ts": thread_timestamp, "limit": 10},
                timeout=min(5.0, remaining),
            )
        except PluginGatewayError:
            # Search results remain useful when optional thread access is unavailable.
            return
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            return
        lines = _thread_lines(messages)
        if lines:
            source = documents[document_index]["text"].split("\n", 1)[0]
            documents[document_index]["text"] = (
                f"{source}\nThread context from #{channel_name}:\n" + "\n".join(lines)
            )[:2000]

    def _request(
        self,
        method: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self._api_base_url}/{method}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout or self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise PluginGatewayError(
                "plugin.timeout", "Slack request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "Slack request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.upstream_error", f"Slack returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "Slack returned an invalid response"
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise PluginGatewayError(
                "plugin.upstream_error", "Slack rejected the request or credential"
            )
        return payload

    def _resolve_token(self, installation: PluginInstallation) -> str:
        if not installation.auth_ref:
            raise PluginGatewayError(
                "plugin.credential_missing", "Slack installation has no credential"
            )
        try:
            token = self._secrets.resolve_for_plugin(installation.auth_ref, "slack")
        except Exception as exc:
            raise PluginGatewayError(
                "plugin.credential_unavailable",
                "Slack credential could not be resolved",
            ) from exc
        if not token:
            raise PluginGatewayError(
                "plugin.credential_unavailable",
                "Slack credential could not be resolved",
            )
        return token


def _slack_document(
    item: dict[str, Any], channel: str
) -> tuple[dict[str, str] | None, tuple[str, str] | None]:
    """Convert one Slack match and capture its optional reply thread."""
    timestamp = item.get("ts")
    thread_timestamp = item.get("thread_ts", timestamp)
    body = item.get("text", "")
    channel_data = item.get("channel")
    channel_id = channel_data.get("id") if isinstance(channel_data, dict) else None
    if (
        not isinstance(timestamp, str)
        or not isinstance(thread_timestamp, str)
        or not isinstance(body, str)
    ):
        return None, None
    username = item.get("username")
    author = username if isinstance(username, str) else "Slack user"
    document = {
        "source_id": f"slack:{channel}:{timestamp}"[:128],
        "title": f"#{channel} · {author}"[:160],
        "text": (f"URL: {item.get('permalink') or 'unavailable'}.\n{body[:1800]}")[
            :2000
        ],
    }
    reply_count = item.get("reply_count", 0)
    thread = (
        (channel_id, thread_timestamp)
        if isinstance(channel_id, str)
        and channel_id
        and isinstance(reply_count, int)
        and reply_count > 0
        else None
    )
    return document, thread


def _thread_lines(messages: list[Any]) -> list[str]:
    """Return bounded author-prefixed text for well-formed Slack replies."""
    lines = []
    for message in messages[:10]:
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        user = message.get("user")
        author = user if isinstance(user, str) else "Slack user"
        lines.append(f"{author}: {text[:500]}")
    return lines
