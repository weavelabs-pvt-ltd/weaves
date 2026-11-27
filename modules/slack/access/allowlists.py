"""Allowed Slack people (with the admin-asserted member mapping) and channels."""

from __future__ import annotations

from typing import Any

from application.ports.repository import Repository
from contracts.v1.slack_access import (
    MAX_ALLOWED_CHANNELS,
    MAX_ALLOWED_PEOPLE,
    SlackAllowedChannel,
    SlackAllowedUser,
    allowed_channel_record_id,
    allowed_user_record_id,
)
from modules.slack.access.context import AccessContext
from modules.slack.access.members import MemberDirectory
from shared.clock import utc_now
from shared.request_context import effective_org_id


class Allowlists:
    """Manage ``slack_allowed_users`` and ``slack_allowed_channels``.

    The Slack-id to Weaves-member mapping exists only because an administrator
    asserted it here: there is no self-link, invitation or email matching, and
    each assertion is audited with the asserting principal.
    """

    def __init__(
        self,
        context: AccessContext,
        *,
        users: Repository[SlackAllowedUser],
        channels: Repository[SlackAllowedChannel],
        members: MemberDirectory,
    ) -> None:
        """Bind the allowlist repositories and the member checks."""
        self._ctx = context
        self.users = users
        self.channels = channels
        self.members = members

    def user_rows(self) -> list[SlackAllowedUser]:
        """The organization's allowed people, ordered by Slack id."""
        rows = self.users.list_scoped(effective_org_id())
        return sorted(rows, key=lambda row: row.slack_user_id)

    def channel_rows(self) -> list[SlackAllowedChannel]:
        """The organization's allowed channels, ordered by channel id."""
        rows = self.channels.list_scoped(effective_org_id())
        return sorted(rows, key=lambda row: row.channel_id)

    def get_user(self, slack_user_id: str) -> SlackAllowedUser | None:
        """The asker's own allowlist row, if any (no permission check: runtime)."""
        org = effective_org_id()
        try:
            return self.users.get_scoped(
                allowed_user_record_id(org, slack_user_id), org
            )
        except KeyError:
            return None

    def get_channel(self, channel_id: str) -> SlackAllowedChannel | None:
        """This channel's allowlist row, if any (no permission check: runtime)."""
        org = effective_org_id()
        try:
            return self.channels.get_scoped(
                allowed_channel_record_id(org, channel_id), org
            )
        except KeyError:
            return None

    def list_users(self) -> list[dict[str, Any]]:
        """Allowed people for the dashboard (manage permission required)."""
        self._ctx.require_manage()
        return [self._user_view(row) for row in self.user_rows()]

    def list_channels(self) -> list[dict[str, Any]]:
        """Allowed channels for the dashboard (manage permission required)."""
        self._ctx.require_manage()
        return [
            {
                "channel_id": row.channel_id,
                "name": row.name_cached,
                "added_by": row.added_by,
                "added_at": row.created_at.isoformat(),
            }
            for row in self.channel_rows()
        ]

    def _user_view(self, row: SlackAllowedUser) -> dict[str, Any]:
        return {
            "slack_user_id": row.slack_user_id,
            "principal_id": row.principal_id,
            "member_name": self.members.display_name(row.principal_id)
            if row.principal_id
            else None,
            "note": row.note,
            "added_by": row.added_by,
            "added_at": row.created_at.isoformat(),
        }

    def add_user(
        self, slack_user_id: str, principal_id: str | None, note: str
    ) -> dict[str, Any]:
        """Allow a Slack member, or update who they are mapped to."""
        actor = self._ctx.require_manage()
        org = effective_org_id()
        if principal_id is not None and not self.members.is_active(principal_id):
            raise ValueError("choose an active member of this organization")
        with self._ctx.mutation():
            record_id = allowed_user_record_id(org, slack_user_id)
            try:
                current: SlackAllowedUser | None = self.users.get_scoped(record_id, org)
            except KeyError:
                current = None
                if len(self.users.list_scoped(org)) >= MAX_ALLOWED_PEOPLE:
                    raise ValueError("too many allowed people") from None
            now = utc_now()
            row = SlackAllowedUser(
                id=record_id,
                org_id=org,
                slack_user_id=slack_user_id,
                principal_id=principal_id,
                note=note.strip(),
                added_by=current.added_by if current else actor,
                created_at=current.created_at if current else now,
                updated_at=now,
            )
            self.users.put(row)
            version = self._ctx.bump(actor).settings_version
            self._ctx.audit(
                actor,
                "slack.access.user_updated" if current else "slack.access.user_added",
                "slack_allowed_user",
                slack_user_id,
                "Slack member mapping asserted by an administrator",
                {
                    "slack_user_id": slack_user_id,
                    "principal_id": principal_id,
                    "previous_principal_id": current.principal_id if current else None,
                    "settings_version": version,
                },
            )
        return self._user_view(row)

    def remove_user(self, slack_user_id: str) -> None:
        """Remove a Slack member from the allowlist (``KeyError`` when absent)."""
        actor = self._ctx.require_manage()
        org = effective_org_id()
        with self._ctx.mutation():
            record_id = allowed_user_record_id(org, slack_user_id)
            current = self.users.get_scoped(record_id, org)
            self.users.delete(record_id)
            version = self._ctx.bump(actor).settings_version
            self._ctx.audit(
                actor,
                "slack.access.user_removed",
                "slack_allowed_user",
                slack_user_id,
                "Slack member removed from the allowlist",
                {
                    "slack_user_id": slack_user_id,
                    "principal_id": current.principal_id,
                    "settings_version": version,
                },
            )

    def add_channel(self, channel_id: str, name: str) -> dict[str, Any]:
        """Allow a Slack channel (idempotent; refreshes the cached name)."""
        actor = self._ctx.require_manage()
        org = effective_org_id()
        with self._ctx.mutation():
            record_id = allowed_channel_record_id(org, channel_id)
            try:
                current: SlackAllowedChannel | None = self.channels.get_scoped(
                    record_id, org
                )
            except KeyError:
                current = None
                if len(self.channels.list_scoped(org)) >= MAX_ALLOWED_CHANNELS:
                    raise ValueError("too many allowed channels") from None
            now = utc_now()
            row = SlackAllowedChannel(
                id=record_id,
                org_id=org,
                channel_id=channel_id,
                name_cached=name.strip(),
                added_by=current.added_by if current else actor,
                created_at=current.created_at if current else now,
                updated_at=now,
            )
            self.channels.put(row)
            version = self._ctx.bump(actor).settings_version
            self._ctx.audit(
                actor,
                "slack.access.channel_added",
                "slack_allowed_channel",
                channel_id,
                "Slack channel allowed",
                {"channel_id": channel_id, "settings_version": version},
            )
        return {
            "channel_id": row.channel_id,
            "name": row.name_cached,
            "added_by": row.added_by,
            "added_at": row.created_at.isoformat(),
        }

    def remove_channel(self, channel_id: str) -> None:
        """Remove a channel from the allowlist (``KeyError`` when absent)."""
        actor = self._ctx.require_manage()
        org = effective_org_id()
        with self._ctx.mutation():
            record_id = allowed_channel_record_id(org, channel_id)
            self.channels.get_scoped(record_id, org)
            self.channels.delete(record_id)
            version = self._ctx.bump(actor).settings_version
            self._ctx.audit(
                actor,
                "slack.access.channel_removed",
                "slack_allowed_channel",
                channel_id,
                "Slack channel removed from the allowlist",
                {"channel_id": channel_id, "settings_version": version},
            )
