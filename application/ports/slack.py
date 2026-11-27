"""Slack installation and event-inbox port (migration 32).

Both collections are global: an event arrives for a ``team_id`` before any
organization is known, so lookups by team run under the named system purpose
``slack``. Per-organization reads (the admin status view) use the tenant scope.
Nothing here holds a token: the bot token lives only in the secret store and an
installation keeps its opaque reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

InstallationStatus = Literal["active", "revoked", "uninstalled", "suspended"]
InboxState = Literal["pending", "processing", "done", "dropped"]
# States in which a workspace is bound to its organization (the unique rule).
HELD_STATUSES: tuple[str, ...] = ("active", "suspended")


@dataclass(frozen=True)
class SlackInstallation:
    """One Slack workspace bound to one Weaves organization."""

    installation_id: str
    team_id: str
    org_id: str
    workspace_id: str
    bot_user_id: str
    installed_by_principal_id: str
    installed_at: datetime
    updated_at: datetime
    status: InstallationStatus = "active"
    team_name: str | None = None
    enterprise_id: str | None = None
    app_id: str | None = None
    bot_secret_ref: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()
    installed_by_name: str | None = None
    installed_by_slack_user: str | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of binding a workspace: created, updated in place or refused."""

    outcome: Literal["created", "updated", "conflict"]
    installation: SlackInstallation | None = None
    previous_secret_ref: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InboxEvent:
    """One accepted Slack event; only what the dispatcher needs, bounded.

    ``text`` is kept for ``app_mention`` only (the question), capped, and is
    never logged. Dropped rows keep no text at all.
    """

    event_id: str
    team_id: str
    event_type: str
    received_at: datetime
    state: InboxState = "pending"
    org_id: str | None = None
    installation_id: str | None = None
    drop_reason: str | None = None
    attempts: int = 1
    request_id: str | None = None
    channel_id: str | None = None
    user_id: str | None = None
    message_ts: str | None = None
    thread_ts: str | None = None
    text: str | None = field(default=None, repr=False)
    reaction: str | None = None


class SlackStore(Protocol):
    """Persistence for installations and the event inbox."""

    def find_by_team(self, team_id: str) -> SlackInstallation | None:
        """The workspace's held (active or suspended) installation, if any."""
        ...

    def list_for_org(self) -> list[SlackInstallation]:
        """The current organization's installations, newest first."""
        ...

    def list_held(self, limit: int = 500) -> list[SlackInstallation]:
        """Active and suspended installations of every organization (purpose slack).

        For the worker's connector health tick only; it binds each organization's
        own tenant context before reading anything of theirs.
        """
        ...

    def get_for_org(self, installation_id: str) -> SlackInstallation | None:
        """One installation of the current organization."""
        ...

    def claim(self, candidate: SlackInstallation) -> ClaimResult:
        """Bind ``candidate.team_id`` to ``candidate.org_id`` atomically.

        Refused (``conflict``) when another organization holds the workspace;
        the same organization's row is updated in place (reinstall).
        """
        ...

    def set_status(
        self,
        installation_id: str,
        status: InstallationStatus,
        now: datetime,
        *,
        clear_secret: bool = False,
    ) -> SlackInstallation | None:
        """Change the status; ``clear_secret`` drops the token reference."""
        ...

    def record_event(self, event: InboxEvent) -> bool:
        """Insert once; ``False`` when the event id or message was seen."""
        ...

    def list_events(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[InboxEvent]:
        """Inbox rows oldest first (the dispatcher's claim, and tests)."""
        ...

    def purge_events(self, before: datetime) -> int:
        """Delete non-pending inbox rows received before ``before``."""
        ...
