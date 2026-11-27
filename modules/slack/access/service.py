"""The Slack access settings service behind ``/organization/slack/access``."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, ModelProfile, Permission, PluginInstallation
from contracts.v1.slack_access import (
    SlackAccessSettings,
    SlackAccessView,
    SlackSettingsFields,
)
from modules.slack.access.allowlists import Allowlists
from modules.slack.access.context import AccessContext, SlackAccessConflict
from modules.slack.access.decision_audit import (
    EXECUTE_ALLOWED_ACTION,
    EXECUTE_BLOCKED_ACTION,
)
from modules.slack.access.models import MemberStanding, PolicySettings
from shared.clock import utc_now
from shared.request_context import effective_org_id

ATTEMPTS_LIMIT = 50
_EDITABLE = tuple(SlackSettingsFields.model_fields)
_CHECKED_PERMISSIONS = frozenset(
    {
        Permission.TASKS_CREATE.value,
        Permission.AGENTS_RUN.value,
        Permission.WORKFLOWS_RUN.value,
    }
)


class SlackAccessService:
    """Read and change one organization's Slack access policy."""

    def __init__(
        self,
        context: AccessContext,
        allowlists: Allowlists,
        *,
        installations: Repository[PluginInstallation],
        model_profiles: Repository[ModelProfile],
        audit_events: Repository[AuditEvent],
    ) -> None:
        """Bind validation sources and the member permission probe."""
        self._ctx = context
        self.allowlists = allowlists
        self._installations = installations
        self._profiles = model_profiles
        self._audit = audit_events

    # --- administrator API -------------------------------------------------

    def view(self) -> dict[str, Any]:
        """The settings page state; defaults (version 0) before the first save."""
        self._ctx.require_manage()
        return self._view(self._ctx.current()).model_dump(mode="json")

    def _view(self, record: SlackAccessSettings | None) -> SlackAccessView:
        base = record
        fields = (
            SlackSettingsFields(**{name: getattr(base, name) for name in _EDITABLE})
            if base is not None
            else SlackSettingsFields()
        )
        return SlackAccessView(
            **{name: getattr(fields, name) for name in _EDITABLE},
            settings_version=base.settings_version if base else 0,
            paused=base.paused if base else False,
            updated_by=base.updated_by if base else "",
            updated_at=base.updated_at.isoformat() if base else None,
            allowed_people_count=len(self.allowlists.user_rows()),
            allowed_channel_count=len(self.allowlists.channel_rows()),
        )

    def update(
        self, fields: SlackSettingsFields, expected_version: int
    ) -> dict[str, Any]:
        """Replace the editable settings if ``expected_version`` is current."""
        actor = self._ctx.require_manage()
        self._validate(fields)
        with self._ctx.mutation():
            current = self._ctx.current()
            have = current.settings_version if current else 0
            if expected_version != have:
                raise SlackAccessConflict(
                    "The Slack access settings changed; reload and try again"
                )
            changes = {name: getattr(fields, name) for name in _EDITABLE}
            changed = sorted(
                name
                for name in _EDITABLE
                if current is None or getattr(current, name) != changes[name]
            )
            saved = self._ctx.bump(actor, **changes)
            self._ctx.audit(
                actor,
                "slack.access.settings_updated",
                "slack_access_settings",
                effective_org_id(),
                "Slack access settings changed",
                {
                    "previous_version": have,
                    "settings_version": saved.settings_version,
                    "changed": changed,
                    "rules": saved.rules.model_dump(),
                    "ask_enabled": saved.ask_enabled,
                    "partial_answers": saved.partial_answers,
                },
            )
        return self._view(saved).model_dump(mode="json")

    def set_paused(self, paused: bool) -> dict[str, Any]:
        """Pause or resume the bot; no version check, so it works in a hurry."""
        actor = self._ctx.require_manage()
        with self._ctx.mutation():
            saved = self._ctx.bump(actor, paused=paused)
            self._ctx.audit(
                actor,
                "slack.access.paused" if paused else "slack.access.resumed",
                "slack_access_settings",
                effective_org_id(),
                "Slack bot paused" if paused else "Slack bot resumed",
                {"settings_version": saved.settings_version, "paused": paused},
            )
        return self._view(saved).model_dump(mode="json")

    def recent_attempts(self) -> list[dict[str, Any]]:
        """The last 50 allowed/blocked Execute attempts (ids and codes only)."""
        self._ctx.require_manage()
        org = effective_org_id()
        events: list[AuditEvent] = []
        for action in (EXECUTE_ALLOWED_ACTION, EXECUTE_BLOCKED_ACTION):
            events.extend(
                self._audit.list_audit_page(
                    org,
                    None,
                    action=action,
                    target_type=None,
                    target_id=None,
                    actor_principal_id=None,
                    created_after=None,
                    created_before=None,
                    cursor_created_at=None,
                    cursor_event_id=None,
                    limit=ATTEMPTS_LIMIT,
                )
            )
        events.sort(key=lambda e: (e.created_at, e.audit_event_id), reverse=True)
        return [self._attempt(e) for e in events[:ATTEMPTS_LIMIT]]

    @staticmethod
    def _attempt(event: AuditEvent) -> dict[str, Any]:
        meta = event.metadata
        return {
            "at": event.created_at.isoformat(),
            "allowed": event.action == EXECUTE_ALLOWED_ACTION,
            "action": meta.get("action"),
            "outcome": meta.get("outcome"),
            "reason": meta.get("reason"),
            "slack_user_id": meta.get("slack_user_id"),
            "channel_id": meta.get("channel_id"),
            "principal_id": meta.get("principal_id"),
            "repeat_count": meta.get("repeat_count", 1),
            "settings_version": meta.get("settings_version"),
        }

    def _validate(self, fields: SlackSettingsFields) -> None:
        org = effective_org_id()
        known = {i.plugin_installation_id for i in self._installations.list_scoped(org)}
        unknown = set(fields.connector_audience) - known
        if unknown:
            raise ValueError("connector_audience names an unknown connector")
        if fields.planner_profile_id is not None:
            try:
                self._profiles.get_scoped(fields.planner_profile_id, org)
            except KeyError:
                raise ValueError("planner_profile_id is not a model profile") from None

    # --- runtime (dispatcher and worker call these under tenant context) ----

    def policy_settings(self) -> PolicySettings:
        """The engine's view of the current settings."""
        return PolicySettings.from_record(self._ctx.current())

    def qa_ids(self) -> tuple[str | None, str | None]:
        """The Q&A service principal and the first Q&A agent version, if provisioned."""
        record = self._ctx.current()
        if record is None:
            return None, None
        return record.qa_principal_id, record.qa_agent_version_id

    def remember_qa(self, principal_id: str, agent_version_id: str) -> None:
        """Store the provisioned Q&A ids without bumping ``settings_version``.

        Provisioning is not an administrator's settings change and must not make
        an open settings page conflict or cancel queued turns. Call inside
        ``mutation()``.
        """
        record = self._ctx.current()
        ids = {"qa_principal_id": principal_id, "qa_agent_version_id": agent_version_id}
        if record is None:
            self._ctx.bump("system:slack", **ids)
            return
        self._ctx.settings.put(
            record.model_copy(update={**ids, "updated_at": utc_now()})
        )

    def mutation(self) -> AbstractContextManager[None]:
        """Serialize this organization's Slack access writes (one transaction)."""
        return self._ctx.mutation()

    def audit_system(
        self, action: str, target_id: str, metadata: dict[str, Any]
    ) -> None:
        """One audit event for a change the bot made by itself (ids only)."""
        self._ctx.audit(
            "system:slack",
            action,
            "slack_qa_agent",
            target_id,
            "Slack Q&A agent changed",
            metadata,
        )

    def planner_profile_id(self) -> str | None:
        """The model profile the administrator chose for the Ask planner, if any."""
        record = self._ctx.current()
        return record.planner_profile_id if record is not None else None

    def asker_standing(
        self, slack_user_id: str, channel_id: str
    ) -> tuple[bool, bool, MemberStanding | None]:
        """Return ``(user_listed, channel_listed, mapped member)`` for one asker.

        Only the asker's own standing leaves this method, never the lists.
        """
        user = self.allowlists.get_user(slack_user_id)
        channel = self.allowlists.get_channel(channel_id)
        member = None
        if user is not None and user.principal_id is not None:
            member = self.member_standing(user.principal_id)
        return user is not None, channel is not None, member

    def member_standing(self, principal_id: str) -> MemberStanding:
        """An active-member flag and the Weaves permissions the engine checks."""
        members = self.allowlists.members
        active = members.is_active(principal_id)
        granted = members.permissions(principal_id) if active else frozenset()
        return MemberStanding(principal_id, active, granted & _CHECKED_PERMISSIONS)
