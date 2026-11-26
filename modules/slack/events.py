"""Turn one verified Slack ``event_callback`` into an inbox row or a lifecycle change.

Runs after the signature check and never calls Slack. The work is one lookup by
``team_id``, the org-state checks and one insert, so the ack stays far inside
Slack's three seconds. Message text is kept only for ``app_mention`` (capped);
dropped rows keep none, and nothing here logs text.

Outcomes (the ``slack_events_total`` label): ``accepted``, ``duplicate``,
``dropped`` (a stored row, reason on it), ``unknown_team``, ``ignored``,
``malformed``, ``disabled``, ``handled`` (uninstall or revoke).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from application.ports.slack import InboxEvent, SlackInstallation, SlackStore
from modules.slack.installation_service import SlackInstallationService
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")
MAX_TEXT_CHARS = 4000
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
_REACTION = re.compile(r"(\+1|-1|thumbsup|thumbsdown)(::skin-tone-[2-6])?")
STORED_TYPES = frozenset({"app_mention", "reaction_added"})
LIFECYCLE_TYPES = frozenset({"app_uninstalled", "tokens_revoked"})
METRIC_TYPES = STORED_TYPES | LIFECYCLE_TYPES | {"url_verification"}


@dataclass(frozen=True)
class Ingested:
    """What the HTTP layer needs: the outcome and an optional follow-up."""

    outcome: str
    event_type: str
    ack_reaction: tuple[SlackInstallation, str, str] | None = None


def metric_type(raw: object) -> str:
    """A bounded metric label for an event type."""
    return raw if isinstance(raw, str) and raw in METRIC_TYPES else "other"


def _valid(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


@contextmanager
def tenant_of(installation: SlackInstallation) -> Iterator[None]:
    """Run under the installation's organization (derived from our own row)."""
    token = set_request_identity(
        installation.installed_by_principal_id,
        installation.org_id,
        installation.workspace_id,
    )
    try:
        yield
    finally:
        reset_request_principal(token)


class SlackEventIngest:
    """Accept events for installed, healthy organizations; refuse the rest."""

    def __init__(
        self,
        *,
        store: SlackStore,
        installs: SlackInstallationService,
        enabled: bool,
        platform_paused: Callable[[], bool],
        org_active: Callable[[str], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the store, lifecycle handler and the state checks."""
        self._store = store
        self._installs = installs
        self._enabled = enabled
        self._platform_paused = platform_paused
        self._org_active = org_active
        self._clock = clock

    def handle(
        self, payload: dict[str, Any], *, retry_num: int = 0, request_id: str | None
    ) -> Ingested:
        """Process one ``event_callback`` payload."""
        event = payload.get("event")
        body: dict[str, Any] = event if isinstance(event, dict) else {}
        event_type = metric_type(body.get("type"))
        team_id = _valid(payload.get("team_id"), _ID)
        event_id = _valid(payload.get("event_id"), _ID)
        if payload.get("type") != "event_callback" or not team_id or not event_id:
            return Ingested("malformed", event_type)
        if event_type == "other":
            return Ingested("ignored", event_type)
        if not self._enabled:
            return Ingested("disabled", event_type)
        installation = self._store.find_by_team(team_id)
        if installation is None:
            return Ingested("unknown_team", event_type)
        with tenant_of(installation):
            if event_type in LIFECYCLE_TYPES:
                return self._lifecycle(installation, event_type, body)
            return self._store_event(
                installation, event_id, event_type, body, retry_num, request_id
            )

    def _lifecycle(
        self, installation: SlackInstallation, event_type: str, body: dict[str, Any]
    ) -> Ingested:
        """``app_uninstalled`` and bot ``tokens_revoked`` end the installation."""
        if event_type == "tokens_revoked":
            tokens = body.get("tokens")
            bot = tokens.get("bot") if isinstance(tokens, dict) else None
            if not bot:  # only user tokens went: the bot is unaffected
                return Ingested("ignored", event_type)
        status = "uninstalled" if event_type == "app_uninstalled" else "revoked"
        self._installs.mark_lost(
            installation, status, actor=f"slack:{installation.team_id}"
        )
        return Ingested("handled", event_type)

    def _drop_reason(self, installation: SlackInstallation) -> str | None:
        if self._platform_paused():
            return "platform_paused"
        if installation.status != "active":
            return "installation_suspended"
        if not self._org_active(installation.org_id):
            return "org_unavailable"
        return None

    def _store_event(
        self,
        installation: SlackInstallation,
        event_id: str,
        event_type: str,
        body: dict[str, Any],
        retry_num: int,
        request_id: str | None,
    ) -> Ingested:
        user = _valid(body.get("user"), _ID)
        if user == installation.bot_user_id:
            return Ingested("ignored", event_type)  # never react to ourselves
        if event_type == "reaction_added" and not _is_feedback(body.get("reaction")):
            return Ingested("ignored", event_type)  # only thumbs are feedback
        fields = _event_fields(event_type, body)
        if fields is None or user is None:
            return Ingested("malformed", event_type)
        reason = self._drop_reason(installation)
        row = InboxEvent(
            event_id=event_id,
            team_id=installation.team_id,
            event_type=event_type,
            received_at=self._clock(),
            state="pending" if reason is None else "dropped",
            org_id=installation.org_id,
            installation_id=installation.installation_id,
            drop_reason=reason,
            attempts=retry_num + 1,
            request_id=request_id,
            user_id=user,
            **(fields if reason is None else {**fields, "text": None}),
        )
        if not self._store.record_event(row):
            return Ingested("duplicate", event_type)
        if reason is not None:
            return Ingested("dropped", event_type)
        reaction = None
        if event_type == "app_mention" and row.channel_id and row.message_ts:
            reaction = (installation, row.channel_id, row.message_ts)
        return Ingested("accepted", event_type, reaction)


def _is_feedback(reaction: object) -> bool:
    """Thumbs up or down (any skin tone); no other reaction is ever stored."""
    return isinstance(reaction, str) and _REACTION.fullmatch(reaction) is not None


def _event_fields(event_type: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """The bounded fields kept for a stored event type, or ``None`` if malformed."""
    if event_type == "app_mention":
        channel = _valid(body.get("channel"), _ID)
        ts = _valid(body.get("ts"), _TS)
        if not channel or not ts:
            return None
        text = body.get("text")
        thread = _valid(body.get("thread_ts"), _TS)
        return {
            "channel_id": channel,
            "message_ts": ts,
            "thread_ts": thread,
            "text": text[:MAX_TEXT_CHARS] if isinstance(text, str) else None,
        }
    item = body.get("item")
    reaction = body.get("reaction")
    if not isinstance(item, dict) or item.get("type") != "message":
        return None
    channel = _valid(item.get("channel"), _ID)
    ts = _valid(item.get("ts"), _TS)
    if not channel or not ts or not isinstance(reaction, str):
        return None
    return {"channel_id": channel, "message_ts": ts, "reaction": reaction}
