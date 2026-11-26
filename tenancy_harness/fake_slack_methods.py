"""Web API method handlers for the fake Slack server.

Each handler takes the request parameters and the caller's token record and
returns the response body, or raises ``Fail`` for an ``ok: false`` answer.
Behaviour follows Slack's documented error codes for the cases Weaves relies on.
"""

import json
import math
from collections.abc import Callable
from typing import Any

from tenancy_harness.fake_slack_state import (
    REPLIES_INTERVAL_SECONDS,
    STATE,
    STATUS_TTL_SECONDS,
)

REPLIES_MAX = 15


class Fail(Exception):
    """An ``ok: false`` answer with Slack's error code."""

    def __init__(self, error: str, status: int = 200, headers: dict | None = None):
        """Keep Slack error code, HTTP status and headers."""
        super().__init__(error)
        self.error = error
        self.status = status
        self.headers = headers or {}


Handler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _channel(params):
    channel = params.get("channel", "")
    if channel not in STATE.channels:
        raise Fail("channel_not_found")
    return channel


def _find(channel, ts):
    for message in STATE.messages.get(channel, []):
        if message["ts"] == ts:
            return message
    raise Fail("message_not_found")


def _content(params):
    if not params.get("text") and not params.get("blocks"):
        raise Fail("no_text")
    blocks = params.get("blocks")
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            raise Fail("invalid_blocks") from None
    return params.get("text", ""), blocks


def oauth_v2_access(params, _token):
    """Handle ``oauth.v2.access``."""
    code = STATE.codes.get(params.get("code", ""))
    if code is None:
        raise Fail("invalid_code")
    team = code["team"]
    serial = STATE.next_id("")
    bot, user = f"xoxb-fixture-{serial}", f"xoxp-fixture-{serial}"
    STATE.tokens[bot] = {"team": team, "kind": "bot", "revoked": False}
    STATE.tokens[user] = {"team": team, "kind": "user", "revoked": False}
    enterprise = code["enterprise"]
    return {
        "app_id": "A0FAKE001",
        "access_token": bot,
        "token_type": "bot",
        "scope": "app_mentions:read,chat:write",
        "bot_user_id": "U0FAKEBOT",
        "team": None if enterprise else {"id": team, "name": "Fixture Workspace"},
        "enterprise": {"id": "E0FAKE001", "name": "Fixture Grid"}
        if enterprise
        else None,
        "is_enterprise_install": enterprise,
        "authed_user": {"id": "U0INSTALL", "access_token": user, "token_type": "user"},
    }


def auth_test(_params, token):
    """Call ``auth.test``: who the token belongs to."""
    return {
        "url": "https://fixture.example/",
        "team_id": token["team"],
        "user_id": "U0FAKEBOT",
    }


def auth_revoke(_params, token):
    """Call ``auth.revoke``: invalidate the token."""
    token["revoked"] = True
    return {"revoked": True}


def post_message(params, token):
    """Handle ``post.message``."""
    channel = _channel(params)
    text, blocks = _content(params)
    thread_ts = params.get("thread_ts") or None
    if thread_ts and not any(
        m["ts"] == thread_ts for m in STATE.messages.get(channel, [])
    ):
        raise Fail("thread_not_found")
    message = {
        "ts": STATE.next_ts(),
        "thread_ts": thread_ts,
        "text": text,
        "blocks": blocks,
        "user": "U0FAKEBOT",
        "bot": True,
        "token": token["team"],
    }
    STATE.messages.setdefault(channel, []).append(message)
    if thread_ts:  # a reply clears the thread's native status
        STATE.statuses.pop((channel, thread_ts), None)
    return {"channel": channel, "ts": message["ts"], "message": _public(message)}


def update_message(params, _token):
    """Edit a message with ``chat.update``."""
    channel = _channel(params)
    message = _find(channel, params.get("ts", ""))
    if not message["bot"]:
        raise Fail("cant_update_message")
    text, blocks = _content(params)
    message["text"], message["blocks"] = text, blocks
    return {"channel": channel, "ts": message["ts"], "text": text}


def delete_message(params, _token):
    """Delete a message with ``chat.delete``."""
    channel = _channel(params)
    message = _find(channel, params.get("ts", ""))
    if not message["bot"]:
        raise Fail("cant_delete_message")
    STATE.messages[channel].remove(message)
    return {"channel": channel, "ts": message["ts"]}


def post_ephemeral(params, _token):
    """Handle ``post.ephemeral``."""
    channel = _channel(params)
    if not params.get("user"):
        raise Fail("user_not_in_channel")
    text, blocks = _content(params)
    entry = {
        "channel": channel,
        "user": params["user"],
        "text": text,
        "blocks": blocks,
        "thread_ts": params.get("thread_ts") or None,
        "ts": STATE.next_ts(),
    }
    STATE.ephemerals.append(entry)
    return {"message_ts": entry["ts"]}


def reactions_add(params, token):
    """Handle ``reactions.add``."""
    channel = _channel(params)
    message = _find(channel, params.get("timestamp", ""))
    name = params.get("name", "")
    if not name:
        raise Fail("invalid_name")
    key = (channel, message["ts"], name, token["team"])
    if key in STATE.reactions:
        raise Fail("already_reacted")
    STATE.reactions.add(key)
    return {}


def set_status(params, _token):
    """Handle ``set.status``."""
    if STATE.status_mode != "supported":
        raise Fail("method_not_supported_for_channel_type")
    channel = params.get("channel_id", "")
    if channel not in STATE.channels:
        raise Fail("channel_not_found")
    thread_ts = params.get("thread_ts", "")
    _find(channel, thread_ts)
    status = params.get("status", "")
    if status:
        expires = STATE.clock() + STATUS_TTL_SECONDS
        STATE.statuses[(channel, thread_ts)] = {"status": status, "expires_at": expires}
    else:
        STATE.statuses.pop((channel, thread_ts), None)
    return {}


def conversations_replies(params, token):
    """Handle ``conversations.replies``."""
    channel = _channel(params)
    now = STATE.clock()
    team = token["team"]
    last = STATE.last_replies.get(team)
    if last is not None and now - last < REPLIES_INTERVAL_SECONDS:
        wait = math.ceil(REPLIES_INTERVAL_SECONDS - (now - last))
        raise Fail("ratelimited", 429, {"Retry-After": str(wait)})
    STATE.last_replies[team] = now
    parent = _find(channel, params.get("ts", ""))
    thread = [parent] + [
        m for m in STATE.messages[channel] if m["thread_ts"] == parent["ts"]
    ]
    limit = max(1, min(int(params.get("limit") or REPLIES_MAX), REPLIES_MAX))
    start = int(params.get("cursor") or 0)
    page = thread[start : start + limit]
    more = start + limit < len(thread)
    return {
        "messages": [_public(m) for m in page],
        "has_more": more,
        "response_metadata": {"next_cursor": str(start + limit) if more else ""},
    }


def conversations_list(params, _token):
    """List channels one page at a time."""
    types = set((params.get("types") or "public_channel").split(","))
    wanted = [
        c
        for c in STATE.channels.values()
        if ("private_channel" if c["is_private"] else "public_channel") in types
    ]
    limit = max(1, min(int(params.get("limit") or 100), 1000))
    start = int(params.get("cursor") or 0)
    more = start + limit < len(wanted)
    return {
        "channels": wanted[start : start + limit],
        "response_metadata": {"next_cursor": str(start + limit) if more else ""},
    }


def _public(message):
    return {k: v for k, v in message.items() if k not in {"bot", "token"}}


HANDLERS: dict[str, Handler] = {
    "oauth.v2.access": oauth_v2_access,
    "auth.test": auth_test,
    "auth.revoke": auth_revoke,
    "chat.postMessage": post_message,
    "chat.update": update_message,
    "chat.delete": delete_message,
    "chat.postEphemeral": post_ephemeral,
    "reactions.add": reactions_add,
    "assistant.threads.setStatus": set_status,
    "conversations.replies": conversations_replies,
    "conversations.list": conversations_list,
}
