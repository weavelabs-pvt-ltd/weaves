"""Accept one verified button press into the inbox (the API side of the endpoint).

One lookup of the team's held installation and one insert, so the ack stays far
inside Slack's three seconds; the press is carried out later by the worker tick
(``modules.slack.interactions.tick``). The inbox row is the dedupe record
(``event_type=interaction``, the key of ``press_event_id``) and, until it is
settled, the work queue: the button's opaque reference is kept in ``text`` and
the action in ``reaction`` (both are cleared or ignored once settled; neither is
message text).

Unlike events, a press is **not** refused for a paused platform, a paused bot or
a quarantined organization: Stop must keep working then (design 3.12), and the
worker's gates decide what Try again may do. What is dropped here is only what
cannot be answered at all: Slack disabled, an unknown or ended workspace (acked
and dropped, no oracle) and an organization that is being deleted.

Outcomes (the metric label): ``accepted``, ``duplicate``, ``unknown_team``,
``disabled``, ``dropped``, ``ignored``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from application.ports.slack import InboxEvent, SlackStore
from modules.slack.events import tenant_of
from modules.slack.interactions.payload import (
    Press,
    press_event_id,
    press_request_id,
)

INTERACTION = "interaction"


class SlackInteractionIngest:
    """Store presses for installed workspaces; refuse the rest."""

    def __init__(
        self,
        *,
        store: SlackStore,
        enabled: bool,
        org_present: Callable[[str], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the store and the organization-state check."""
        self._store = store
        self._enabled = enabled
        self._org_present = org_present
        self._clock = clock

    def handle(self, press: Press) -> str:
        """Store one press once; returns the outcome label."""
        if not self._enabled:
            return "disabled"
        installation = self._store.find_by_team(press.team_id)
        if installation is None:
            return "unknown_team"
        if press.user_id == installation.bot_user_id:
            return "ignored"  # never react to ourselves
        with tenant_of(installation):
            if not self._org_present(installation.org_id):
                return "dropped"
        received = self._clock()
        event_id = press_event_id(press, received)
        row = InboxEvent(
            event_id=event_id,
            team_id=installation.team_id,
            event_type=INTERACTION,
            received_at=received,
            org_id=installation.org_id,
            installation_id=installation.installation_id,
            request_id=press_request_id(event_id),
            channel_id=press.channel_id,
            user_id=press.user_id,
            message_ts=press.message_ts,
            thread_ts=press.thread_ts,
            text=press.value,
            reaction=press.action_id,
        )
        return "accepted" if self._store.record_event(row) else "duplicate"
