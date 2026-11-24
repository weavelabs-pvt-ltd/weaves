"""Tenant quarantine: stop one organization now, and undo exactly that later.

Quarantine is an operator action (never reachable over HTTP) that combines:

* the existing lifecycle **suspension**, which already blocks the organization's
  API credentials and account selection (403), refuses new work, makes workers
  cancel its running and claimed jobs at the next boundary and makes schedule
  and webhook dispatch skip it;
* **disabling** its active schedules and webhook triggers, recording their ids;
* optionally (``revoke_credentials``) **revoking** its API credentials. That is
  irreversible: each revocation writes a 6.2 tombstone, and release never
  revives a credential.

Account sessions belong to people who may also belong to other organizations,
so they are not revoked; the organization simply stops being selectable.
Connector secrets are untouched but unreachable (they are only used by work
that is blocked). Queued work is not rewritten: workers cancel it when claimed.

``org_quarantines`` records exactly what was changed. Release re-enables only the
schedules and triggers quarantine itself disabled (and only if they are still
disabled), resumes the organization only if quarantine suspended it, and leaves
everything else (including anything an operator or owner changed meanwhile)
alone. Schedule occurrences missed while quarantined are not replayed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    OrganizationLifecycleState,
    WorkflowSchedule,
    WorkflowScheduleStatus,
    WorkflowTrigger,
    WorkflowTriggerStatus,
)
from modules.auth.organization_lifecycle import OrganizationLifecycleService
from modules.platform_controls.audit import (
    OK,
    OperatorAudit,
    clean,
    new_correlation_id,
)
from modules.platform_controls.store import ControlsStore, Quarantine
from modules.recovery.tombstones import (
    CREDENTIAL_REVOKED,
    TombstoneSink,
    digest_of,
)
from shared.clock import utc_now
from shared.request_context import reset_request_principal, set_request_identity

State = OrganizationLifecycleState
QUARANTINED = "quarantined"
RELEASED = "released"


class QuarantineService:
    """Quarantine and release one organization, with audit."""

    def __init__(
        self,
        *,
        store: ControlsStore,
        audit: OperatorAudit,
        lifecycle: OrganizationLifecycleService,
        schedules: Repository[WorkflowSchedule],
        triggers: Repository[WorkflowTrigger],
        credentials: Repository[ApiCredential],
        tombstones: TombstoneSink,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind storage, the lifecycle service and the per-organization repositories."""
        self._store = store
        self._audit = audit
        self._lifecycle = lifecycle
        self._schedules = schedules
        self._triggers = triggers
        self._credentials = credentials
        self._tombstones = tombstones
        self._unit_of_work = unit_of_work
        self._now = now

    def status(self, org_id: str) -> dict[str, Any]:
        """The organization's state and its quarantine record (read only)."""
        state = self._lifecycle.operator_status(org_id)["state"]
        return self._view(org_id, str(state), self._store.get_quarantine(org_id))

    def quarantine(
        self,
        org_id: str,
        operator: str,
        reason: str,
        *,
        revoke_credentials: bool = False,
        ticket: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Quarantine (idempotent; a repeat can add credential revocation)."""
        who = clean(operator, "operator", 320)
        why = clean(reason, "reason", 500)
        state = self._lifecycle.operator_status(org_id)["state"]
        if state not in {State.ACTIVE.value, State.SUSPENDED.value}:
            raise ValueError(f"cannot quarantine an organization in {state}")
        existing = self._store.get_quarantine(org_id)
        previous = (
            existing.changes if existing and existing.state == QUARANTINED else {}
        )
        changes: dict[str, Any] = {
            "suspended_by_quarantine": bool(previous.get("suspended_by_quarantine"))
            or state == State.ACTIVE.value,
            "schedules": list(previous.get("schedules", [])),
            "triggers": list(previous.get("triggers", [])),
            "credentials_revoked": list(previous.get("credentials_revoked", [])),
        }
        scope = set_request_identity(f"operator:{who}"[:128], org_id, "lifecycle")
        try:
            # Plan first and persist the plan, so a crash part-way is releasable.
            self._plan(org_id, changes, revoke_credentials)
            record = Quarantine(
                org_id=org_id,
                state=QUARANTINED,
                changes=changes,
                quarantined_by=who,
                reason=why,
                correlation_id=correlation_id or new_correlation_id(),
                quarantined_at=existing.quarantined_at
                if existing is not None and existing.state == QUARANTINED
                else self._now(),
            )
            action = self._audit.build(
                operator=who,
                command="organization.quarantine",
                target=org_id,
                org_id=org_id,
                reason=why,
                ticket=ticket,
                correlation_id=record.correlation_id,
                result=OK,
                details=_counts(changes, revoke_credentials),
            )
            self._store.put_quarantine(record, action)
            if state == State.ACTIVE.value:
                self._lifecycle.suspend(org_id, who, f"Quarantine: {why}"[:500])
            self._apply(org_id, changes)
        finally:
            reset_request_principal(scope)
        return self._view(org_id, State.SUSPENDED.value, record)

    def release(
        self,
        org_id: str,
        operator: str,
        reason: str,
        *,
        ticket: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Reverse exactly what quarantine did (not credential revocations)."""
        who = clean(operator, "operator", 320)
        why = clean(reason, "reason", 500)
        record = self._store.get_quarantine(org_id)
        if record is None or record.state != QUARANTINED:
            raise ValueError("the organization is not quarantined")
        changes = record.changes
        scope = set_request_identity(f"operator:{who}"[:128], org_id, "lifecycle")
        try:
            restored = self._restore(org_id, changes)
            state = self._lifecycle.operator_status(org_id)["state"]
            resumed = False
            if (
                changes.get("suspended_by_quarantine")
                and state == State.SUSPENDED.value
            ):
                self._lifecycle.resume(org_id, who, f"Quarantine released: {why}"[:500])
                resumed = True
        finally:
            reset_request_principal(scope)
        released = replace(
            record,
            state=RELEASED,
            released_by=who,
            released_at=self._now(),
            release_reason=why,
        )
        action = self._audit.build(
            operator=who,
            command="organization.release_quarantine",
            target=org_id,
            org_id=org_id,
            reason=why,
            ticket=ticket,
            correlation_id=correlation_id,
            result=OK,
            details={
                **restored,
                "resumed": resumed,
                "credentials_left_revoked": len(changes.get("credentials_revoked", [])),
            },
        )
        self._store.put_quarantine(released, action)
        final = self._lifecycle.operator_status(org_id)["state"]
        return self._view(org_id, str(final), released)

    # ---- steps ---------------------------------------------------------
    def _plan(
        self, org_id: str, changes: dict[str, Any], revoke_credentials: bool
    ) -> None:
        """Add the ids this run will change to the persisted plan."""
        for schedule in self._schedules.list_scoped(org_id):
            if schedule.status is WorkflowScheduleStatus.ACTIVE:
                _add(changes["schedules"], schedule.schedule_id)
        for trigger in self._triggers.list_scoped(org_id):
            if trigger.status is WorkflowTriggerStatus.ACTIVE:
                _add(changes["triggers"], trigger.trigger_id)
        if revoke_credentials:
            for credential in self._credentials.list_scoped(org_id):
                if credential.status is ApiCredentialStatus.ACTIVE:
                    _add(changes["credentials_revoked"], credential.id)

    def _apply(self, org_id: str, changes: dict[str, Any]) -> None:
        now = self._now()
        planned = set(changes["schedules"])
        for schedule in self._schedules.list_scoped(org_id):
            if schedule.schedule_id in planned and (
                schedule.status is WorkflowScheduleStatus.ACTIVE
            ):
                self._schedules.put(
                    _copy(schedule, status=WorkflowScheduleStatus.DISABLED, at=now)
                )
        planned = set(changes["triggers"])
        for trigger in self._triggers.list_scoped(org_id):
            if trigger.trigger_id in planned and (
                trigger.status is WorkflowTriggerStatus.ACTIVE
            ):
                self._triggers.put(
                    _copy(trigger, status=WorkflowTriggerStatus.DISABLED, at=now)
                )
        planned = set(changes["credentials_revoked"])
        for credential in self._credentials.list_scoped(org_id):
            if credential.id in planned and (
                credential.status is ApiCredentialStatus.ACTIVE
            ):
                self._revoke(credential, now)

    def _revoke(self, credential: ApiCredential, now: datetime) -> None:
        """Tombstone first (fail closed), then revoke."""
        self._tombstones.record(
            CREDENTIAL_REVOKED,
            credential.org_id,
            subject_kind="api_credential",
            subject_id=credential.id,
            subject_digest=digest_of(credential.token_digest),
        )
        revoked = ApiCredential.model_validate(
            credential.model_copy(
                update={
                    "status": ApiCredentialStatus.REVOKED,
                    "revoked_at": now,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self._credentials.put(revoked)

    def _restore(self, org_id: str, changes: dict[str, Any]) -> dict[str, int]:
        now = self._now()
        schedules = set(changes.get("schedules", []))
        triggers = set(changes.get("triggers", []))
        restored = {"schedules": 0, "triggers": 0}
        for schedule in self._schedules.list_scoped(org_id):
            if schedule.schedule_id in schedules and (
                schedule.status is WorkflowScheduleStatus.DISABLED
            ):
                interval = timedelta(seconds=schedule.interval_seconds)
                self._schedules.put(
                    _copy(
                        schedule,
                        status=WorkflowScheduleStatus.ACTIVE,
                        at=now,
                        next_run_at=max(schedule.next_run_at, now + interval),
                    )
                )
                restored["schedules"] += 1
        for trigger in self._triggers.list_scoped(org_id):
            if trigger.trigger_id in triggers and (
                trigger.status is WorkflowTriggerStatus.DISABLED
            ):
                self._triggers.put(
                    _copy(trigger, status=WorkflowTriggerStatus.ACTIVE, at=now)
                )
                restored["triggers"] += 1
        return restored

    @staticmethod
    def _view(org_id: str, state: str, record: Quarantine | None) -> dict[str, Any]:
        if record is None:
            return {"organization_id": org_id, "state": state, "quarantine": None}
        changes = record.changes
        return {
            "organization_id": org_id,
            "state": state,
            "quarantine": {
                "state": record.state,
                "reason": record.reason,
                "by": record.quarantined_by,
                "at": record.quarantined_at.isoformat(),
                "correlation_id": record.correlation_id,
                "suspended_by_quarantine": changes.get("suspended_by_quarantine"),
                "schedules_disabled": len(changes.get("schedules", [])),
                "triggers_disabled": len(changes.get("triggers", [])),
                "credentials_revoked": len(changes.get("credentials_revoked", [])),
                "released_at": record.released_at.isoformat()
                if record.released_at
                else None,
            },
        }


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _copy(record: Any, *, status: Any, at: datetime, **extra: Any) -> Any:
    """Return a validated copy with a new status."""
    data = record.model_copy(update={"status": status, "updated_at": at, **extra})
    return type(record).model_validate(data.model_dump())


def _counts(changes: dict[str, Any], revoke: bool) -> dict[str, Any]:
    return {
        "suspended_by_quarantine": changes["suspended_by_quarantine"],
        "schedules": len(changes["schedules"]),
        "triggers": len(changes["triggers"]),
        "credentials_revoked": len(changes["credentials_revoked"]),
        "revoke_credentials_requested": revoke,
    }
