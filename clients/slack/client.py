"""A thin Slack Web API client: bounded retry, mapped errors, no secret in logs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from clients.slack.errors import (
    TOKEN_ERROR_CODES,
    SlackApiError,
    SlackRateLimited,
    SlackTokenError,
)
from clients.slack.models import ChannelPage, MessageRef, RepliesPage, StatusResult

logger = logging.getLogger(__name__)
MAX_REPLIES_LIMIT = 15
UNSUPPORTED_STATUS_ERRORS = frozenset({"method_not_supported_for_channel_type"})
Params = dict[str, str]


class SlackApiClient:
    """Calls ``{api_base_url}/{method}`` with form bodies and a bearer token.

    The caller owns the ``httpx.Client`` (proxies, TLS). Redirects are never
    followed. A ``ratelimited`` answer is retried once when Slack's
    ``Retry-After`` is at most ``max_retry_wait_seconds``; otherwise (or on a
    second 429) ``SlackRateLimited`` is raised so the caller can reschedule.
    Errors and logs carry the method name and Slack's error code only.
    """

    def __init__(
        self,
        http: httpx.Client,
        *,
        api_base_url: str = "https://slack.com/api",
        timeout_seconds: float = 10.0,
        max_retry_wait_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Keep the client, base URL, limits and sleeper."""
        self._http = http
        self._base = api_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_wait = max_retry_wait_seconds
        self._sleep = sleep

    # -- OAuth and identity ------------------------------------------------

    def oauth_v2_access(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Exchange an install code; client credentials go in HTTP Basic."""
        params = {"code": code}
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        return self._call(
            "oauth.v2.access",
            None,
            params,
            auth=httpx.BasicAuth(client_id, client_secret),
        )

    def auth_test(self, token: str) -> dict[str, Any]:
        """Call ``auth.test``: who the token belongs to."""
        return self._call("auth.test", token, {})

    def auth_revoke(self, token: str) -> dict[str, Any]:
        """Call ``auth.revoke``: invalidate the token."""
        return self._call("auth.revoke", token, {})

    # -- Messages -----------------------------------------------------------

    def post_message(
        self,
        token: str,
        channel: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> MessageRef:
        """Post with ``parse=none`` and no unfurling (design 3.7)."""
        params = _message_params(channel, text, blocks)
        params.update(
            {"parse": "none", "unfurl_links": "false", "unfurl_media": "false"}
        )
        if thread_ts:
            params["thread_ts"] = thread_ts
        return _ref(self._call("chat.postMessage", token, params), "chat.postMessage")

    def update_message(
        self,
        token: str,
        channel: str,
        ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Edit a message with ``chat.update``."""
        params = _message_params(channel, text, blocks)
        params["ts"] = ts
        return _ref(self._call("chat.update", token, params), "chat.update")

    def delete_message(self, token: str, channel: str, ts: str) -> None:
        """Delete a message with ``chat.delete``."""
        self._call("chat.delete", token, {"channel": channel, "ts": ts})

    def post_ephemeral(
        self,
        token: str,
        channel: str,
        user: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Post a message only ``user`` sees; returns Slack's ``message_ts``."""
        params = _message_params(channel, text, blocks)
        params["user"] = user
        params["parse"] = "none"
        if thread_ts:
            params["thread_ts"] = thread_ts
        data = self._call("chat.postEphemeral", token, params)
        ts = data.get("message_ts")
        return ts if isinstance(ts, str) else None

    def add_reaction(self, token: str, channel: str, ts: str, name: str) -> bool:
        """Add a reaction; False when it was already there (not an error)."""
        params = {"channel": channel, "timestamp": ts, "name": name}
        try:
            self._call("reactions.add", token, params)
        except SlackApiError as error:
            if error.code == "already_reacted":
                return False
            raise
        return True

    def set_thread_status(
        self, token: str, channel: str, thread_ts: str, status: str
    ) -> StatusResult:
        """Set the native thinking status; unsupported channel types are a result."""
        params = {"channel_id": channel, "thread_ts": thread_ts, "status": status}
        try:
            self._call("assistant.threads.setStatus", token, params)
        except SlackTokenError:
            raise
        except SlackRateLimited:
            raise
        except SlackApiError as error:
            if error.code in UNSUPPORTED_STATUS_ERRORS:
                return StatusResult("unsupported", error.code)
            return StatusResult("error", error.code)
        return StatusResult("ok")

    # -- Reads --------------------------------------------------------------

    def conversations_replies(
        self,
        token: str,
        channel: str,
        ts: str,
        *,
        limit: int = MAX_REPLIES_LIMIT,
        cursor: str | None = None,
    ) -> RepliesPage:
        """Fetch one page of a thread; ``limit`` is capped at 15 (design 3.7)."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        params = {
            "channel": channel,
            "ts": ts,
            "limit": str(min(limit, MAX_REPLIES_LIMIT)),
        }
        if cursor:
            params["cursor"] = cursor
        data = self._call("conversations.replies", token, params)
        return RepliesPage(
            messages=_dicts(data.get("messages")),
            has_more=data.get("has_more") is True,
            next_cursor=_cursor(data),
        )

    def conversations_list(
        self,
        token: str,
        *,
        types: str = "public_channel,private_channel",
        limit: int = 200,
        cursor: str | None = None,
    ) -> ChannelPage:
        """List channels one page at a time."""
        params = {"types": types, "limit": str(max(1, min(limit, 1000)))}
        if cursor:
            params["cursor"] = cursor
        data = self._call("conversations.list", token, params)
        return ChannelPage(_dicts(data.get("channels")), _cursor(data))

    # -- Transport ----------------------------------------------------------

    def _call(
        self,
        method: str,
        token: str | None,
        params: Params,
        *,
        auth: httpx.Auth | None = None,
    ) -> dict[str, Any]:
        for attempt in (1, 2):
            response = self._post(method, token, params, auth)
            if response.status_code != 429:
                return _parse(method, response)
            retry_after = _retry_after(response)
            logger.warning("slack_rate_limited method=%s attempt=%d", method, attempt)
            if attempt == 2 or retry_after > self._max_wait:
                raise SlackRateLimited(retry_after, method=method)
            self._sleep(retry_after)
        raise AssertionError("unreachable")  # pragma: no cover

    def _post(
        self,
        method: str,
        token: str | None,
        params: Params,
        auth: httpx.Auth | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            return self._http.post(
                f"{self._base}/{method}",
                data=params,
                headers=headers,
                auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            # Never chain: the transport error text may echo request details.
            logger.warning(
                "slack_transport_error method=%s kind=%s", method, type(error).__name__
            )
            raise SlackApiError("network_error", method=method) from None


def _message_params(
    channel: str, text: str, blocks: list[dict[str, Any]] | None
) -> Params:
    params = {"channel": channel, "text": text}
    if blocks is not None:
        params["blocks"] = json.dumps(blocks, separators=(",", ":"))
    return params


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return 1.0


def _parse(method: str, response: httpx.Response) -> dict[str, Any]:
    if 300 <= response.status_code < 400:
        raise SlackApiError("redirect_refused", method=method)
    if response.status_code >= 400:
        raise SlackApiError(f"http_{response.status_code}", method=method)
    try:
        data = response.json()
    except ValueError:
        raise SlackApiError("invalid_response", method=method) from None
    if not isinstance(data, dict):
        raise SlackApiError("invalid_response", method=method)
    if data.get("ok") is True:
        return data
    error = data.get("error")
    code = error if isinstance(error, str) and error else "unknown_error"
    if code in TOKEN_ERROR_CODES:
        raise SlackTokenError(code, method=method)
    raise SlackApiError(code, method=method)


def _ref(data: dict[str, Any], method: str) -> MessageRef:
    channel, ts = data.get("channel"), data.get("ts")
    if not isinstance(channel, str) or not isinstance(ts, str):
        raise SlackApiError("invalid_response", method=method)
    return MessageRef(channel, ts)


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _cursor(data: dict[str, Any]) -> str | None:
    meta = data.get("response_metadata")
    cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
    return cursor if isinstance(cursor, str) and cursor else None
