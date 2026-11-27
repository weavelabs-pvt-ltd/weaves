"""Every message to Slack goes through here: sanitized, scrubbed, classified errors.

``SlackPoster`` is the only code that calls the Slack write methods for the bot.
Each text is scrubbed on its way out and the scrubber **fails closed**: when it
cannot check a message the message is not sent (``PostError(kind="scrubber")``).
The Slack errors are folded into four kinds the caller can act on without
knowing Slack: a dead token, a rate limit (with the wait), a retryable failure
and a permanent one. Nothing here logs message text or the token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from application.ports.secrets import SecretStore
from application.ports.slack import SlackInstallation
from clients.slack.client import SlackApiClient
from clients.slack.errors import SlackApiError, SlackRateLimited, SlackTokenError
from modules.slack.text.mrkdwn import has_unsafe_angle
from modules.slack.text.scrub import Scrubbed, ScrubberUnavailable, scrub
from shared.metrics import SLACK_API_429, SLACK_POSTS, SLACK_SCRUB

logger = logging.getLogger("product.slack")

# Slack answers that no retry can fix: the message or place is gone or forbidden.
PERMANENT_CODES = frozenset(
    {
        "channel_not_found",
        "is_archived",
        "not_in_channel",
        "msg_too_long",
        "message_not_found",
        "cant_update_message",
        "cant_delete_message",
        "edit_window_closed",
        "user_not_in_channel",
        "restricted_action",
        "no_text",
        "thread_not_found",
        "access_denied",
    }
)


class PostError(Exception):
    """A message could not be delivered; ``kind`` says what the caller does next."""

    def __init__(
        self,
        kind: str,
        code: str,
        retry_after: float = 0.0,
    ) -> None:
        """Keep the kind (token, rate_limited, retry, permanent, scrubber) and code."""
        super().__init__(f"{kind}:{code}")
        self.kind = kind
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class FinalText:
    """Text that passed the egress checks and may be sent."""

    text: str
    redactions: int = 0


@dataclass(frozen=True)
class LinkButton:
    """A URL button (``View in Weaves``): fixed label, our own app URL."""

    label: str
    url: str


def message_blocks(text: str, button: LinkButton | None) -> list[dict[str, Any]]:
    """Blocks for a message that may carry a link button.

    Editing a message with ``text`` alone keeps its old blocks, so a message that
    ever had a button is always sent (and edited) with explicit blocks.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    ]
    if button is not None:
        parts = urlsplit(button.url)
        if parts.scheme not in ("http", "https") or parts.username or not parts.netloc:
            raise PostError("permanent", "bad_button_url")
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": button.label[:75]},
                        "url": button.url[:2000],
                    }
                ],
            }
        )
    return blocks


def finalize(text: str, scrubber: Callable[[str], Scrubbed] = scrub) -> FinalText:
    """The egress gate: scrub, then refuse anything that still holds raw markup."""
    try:
        cleaned = scrubber(text)
        if has_unsafe_angle(cleaned.text):
            raise ScrubberUnavailable("unsafe_markup")
    except ScrubberUnavailable as error:
        SLACK_SCRUB.inc(result="refused")
        raise PostError("scrubber", str(error)) from None
    except Exception as error:  # any other failure also fails closed
        SLACK_SCRUB.inc(result="refused")
        raise PostError("scrubber", type(error).__name__) from None
    SLACK_SCRUB.inc(result="redacted" if cleaned.redactions else "clean")
    return FinalText(cleaned.text, cleaned.redactions)


def _classify(error: Exception, method: str) -> PostError:
    if isinstance(error, SlackTokenError):
        return PostError("token", error.code)
    if isinstance(error, SlackRateLimited):
        SLACK_API_429.inc(method=method)
        return PostError("rate_limited", "ratelimited", error.retry_after)
    if isinstance(error, SlackApiError):
        kind = "permanent" if error.code in PERMANENT_CODES else "retry"
        return PostError(kind, error.code)
    return PostError("retry", type(error).__name__)


class SlackPoster:
    """Post, edit, delete and whisper with the installation's own bot token."""

    def __init__(
        self,
        api: SlackApiClient,
        secrets: SecretStore,
        *,
        scrubber: Callable[[str], Scrubbed] = scrub,
    ) -> None:
        """Bind the client, the secret store and the (replaceable) scrubber."""
        self._api = api
        self._secrets = secrets
        self._scrubber = scrubber

    def _token(self, installation: SlackInstallation) -> str:
        if installation.bot_secret_ref is None:
            raise PostError("token", "no_token")
        return self._secrets.get(installation.bot_secret_ref)

    def _call[T](self, method: str, action: Callable[[], T]) -> T:
        try:
            result = action()
        except PostError:
            raise
        except Exception as error:
            failure = _classify(error, method)
            SLACK_POSTS.inc(method=method, outcome=failure.code[:40])
            logger.warning(
                "slack_post_failed method=%s kind=%s code=%s",
                method,
                failure.kind,
                failure.code,
            )
            raise failure from None
        SLACK_POSTS.inc(method=method, outcome="ok")
        return result

    def post(
        self,
        installation: SlackInstallation,
        channel: str,
        text: str,
        *,
        thread_ts: str,
        rich: bool = False,
        button: LinkButton | None = None,
    ) -> str:
        """Post a new message in the thread; returns its ``ts``.

        ``rich`` sends the text as a block (with the optional link button).
        """
        final = finalize(text, self._scrubber)
        blocks = message_blocks(final.text, button) if rich else None
        token = self._token(installation)
        ref = self._call(
            "chat.postMessage",
            lambda: self._api.post_message(
                token, channel, text=final.text, thread_ts=thread_ts, blocks=blocks
            ),
        )
        return ref.ts

    def edit(
        self,
        installation: SlackInstallation,
        channel: str,
        ts: str,
        text: str,
        *,
        rich: bool = False,
        button: LinkButton | None = None,
    ) -> None:
        """Edit one of our messages in place (``rich`` replaces its blocks too)."""
        final = finalize(text, self._scrubber)
        blocks = message_blocks(final.text, button) if rich else None
        token = self._token(installation)
        self._call(
            "chat.update",
            lambda: self._api.update_message(
                token, channel, ts, text=final.text, blocks=blocks
            ),
        )

    def delete(self, installation: SlackInstallation, channel: str, ts: str) -> None:
        """Delete one of our messages."""
        token = self._token(installation)
        self._call("chat.delete", lambda: self._api.delete_message(token, channel, ts))

    def ephemeral(
        self,
        installation: SlackInstallation,
        channel: str,
        user: str,
        text: str,
        *,
        thread_ts: str | None,
    ) -> str | None:
        """Whisper to ``user`` (only they see it); returns Slack's message ts."""
        final = finalize(text, self._scrubber)
        token = self._token(installation)
        return self._call(
            "chat.postEphemeral",
            lambda: self._api.post_ephemeral(
                token, channel, user, text=final.text, thread_ts=thread_ts
            ),
        )

    def native_status(
        self,
        installation: SlackInstallation,
        channel: str,
        thread_ts: str,
        status: str,
    ) -> bool:
        """Set Slack's own "is working" status; ``False`` when it is unsupported."""
        final = finalize(status, self._scrubber) if status else FinalText("")
        token = self._token(installation)
        result = self._call(
            "assistant.threads.setStatus",
            lambda: self._api.set_thread_status(token, channel, thread_ts, final.text),
        )
        return result.status == "ok"
