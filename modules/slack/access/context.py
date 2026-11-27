"""Shared plumbing of the Slack access services: scope, versions and audit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, AuditTargetScope, Permission
from contracts.v1.slack_access import SlackAccessSettings
from shared.clock import new_id, utc_now
from shared.request_context import effective_org_id, effective_principal_id


class SlackAccessConflict(ValueError):
    """A stale settings version: reload and retry."""


class AccessContext:
    """Own the repositories, the transaction lock and the audit writer."""

    def __init__(
        self,
        *,
        settings: Repository[SlackAccessSettings],
        audit_events: Repository[AuditEvent],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None,
    ) -> None:
        """Bind persistence and the organization-level permission check."""
        self.settings = settings
        self.audit_events = audit_events
        self._authorize = authorize_organization
        self._unit_of_work = unit_of_work
        self._lock = lock

    def require_manage(self) -> str:
        """Return the acting principal if it holds ``slack.manage``."""
        actor = effective_principal_id("local-developer")
        self._authorize(actor, Permission.SLACK_MANAGE)
        return actor

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Serialize the organization's Slack access changes until commit."""
        with self._unit_of_work():
            if self._lock is not None:
                self._lock(f"weaves-slack-access:{effective_org_id()}")
            yield

    def current(self) -> SlackAccessSettings | None:
        """The stored settings row, or ``None`` before the first change."""
        org = effective_org_id()
        try:
            return self.settings.get_scoped(org, org)
        except KeyError:
            return None

    def bump(self, actor: str, **changes: Any) -> SlackAccessSettings:
        """Write the settings row one version higher (defaults on first write)."""
        org = effective_org_id()
        now = utc_now()
        current = self.current()
        if current is None:
            record = SlackAccessSettings(
                org_id=org,
                settings_version=1,
                created_at=now,
                updated_at=now,
                updated_by=actor,
                **changes,
            )
        else:
            record = current.model_copy(
                update={
                    **changes,
                    "settings_version": current.settings_version + 1,
                    "updated_at": now,
                    "updated_by": actor,
                }
            )
        return self.settings.put(record)

    def audit(
        self,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> None:
        """Append one organization-scoped audit event for a change."""
        event_id = new_id()
        self.audit_events.create(
            AuditEvent(
                audit_event_id=event_id,
                org_id=effective_org_id(),
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                summary=summary,
                request_id=event_id,
                metadata=metadata,
                created_at=utc_now(),
            )
        )
