"""Organization lifecycle: suspend, resume, deletion request, cancel and purge start.

Owner actions (request deletion, cancel it) come from an account session on the
selected organization. Operator actions exist only as scripts run on the VM with
database-owner access; they are never reachable over HTTP. Every change writes
the stored ``Organization.status`` projection and the ``OrganizationLifecycle``
record in one unit of work under the per-organization administration lock, and
an ``organization.lifecycle.*`` audit event naming who did it.

State machine (``->`` allowed, anything else is a 409 or a no-op)::

    ACTIVE -> SUSPENDED (operator) -> ACTIVE (operator resume)
    ACTIVE | SUSPENDED -> DELETION_REQUESTED (owner from ACTIVE, operator)
    DELETION_REQUESTED -> previous state (owner cancel in grace, or operator)
    DELETION_REQUESTED -> DELETING (grace elapsed, worker; or operator purge-now)
    DELETING -> DELETED (worker, after the purge converges). DELETED is terminal.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    LifecycleActorKind,
    LifecycleHistoryEntry,
    Organization,
    OrganizationLifecycle,
    OrganizationLifecycleState,
    Permission,
)
from modules.auth.lifecycle_actor import Actor, clean_reason
from modules.auth.lifecycle_state import STATUS_FOR_STATE, effective_organization_state
from modules.auth.organization_guard import AdministrationConflict, OrganizationGuard
from modules.auth.organization_retention import DELETED_NAME_PLACEHOLDER
from shared.clock import new_id, utc_now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    reset_request_principal,
    set_request_identity,
)

State = OrganizationLifecycleState
_MAX_HISTORY = 200
_NOT_AVAILABLE = "Organization not available"


def grace_days_from_env() -> int:
    """Read the deletion grace period (days), defaulting to seven."""
    raw = os.environ.get("WEAVES_ORGANIZATION_DELETION_GRACE_DAYS", "7").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        raise ValueError(
            "WEAVES_ORGANIZATION_DELETION_GRACE_DAYS must be between 1 and 365"
        )
    return int(raw)


class LifecycleConflict(AdministrationConflict):
    """The requested transition is not allowed from the current state."""


class OrganizationLifecycleService:
    """Apply lifecycle transitions atomically and expose the current state."""

    def __init__(
        self,
        *,
        guard: OrganizationGuard,
        organizations: Repository[Organization],
        lifecycle: Repository[OrganizationLifecycle],
        authorize: Callable[[str, Permission], None],
        grace_days: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind tenant-scoped persistence, authorization and the grace period."""
        self.guard = guard
        self.organizations = organizations
        self.lifecycle = lifecycle
        self.authorize = authorize
        self.grace_days = grace_days
        self.now = now

    # ---- reading -------------------------------------------------------
    def current(self, org_id: str) -> OrganizationLifecycle:
        """Return the stored record, or a synthetic ACTIVE one when none exists."""
        try:
            return self.lifecycle.get_scoped(org_id, org_id)
        except KeyError:
            now = self.now()
            return OrganizationLifecycle(
                id=org_id, org_id=org_id, created_at=now, updated_at=now
            )

    def state_of(self, org_id: str) -> State:
        """The effective state combining organization status and lifecycle."""
        try:
            lifecycle = self.lifecycle.get_scoped(org_id, org_id)
        except KeyError:
            lifecycle = None
        return effective_organization_state(self.organizations.get(org_id), lifecycle)

    def view(self) -> dict[str, Any]:
        """The caller's own lifecycle view; admins while active, owner otherwise."""
        org_id = effective_org_id()
        actor = effective_principal_id("local-developer")
        state = self.state_of(org_id)
        owner = self.guard.current_settings().owner_principal_id
        if state is State.ACTIVE:
            self.authorize(actor, Permission.ORGANIZATIONS_ADMIN)
        elif actor != owner:
            raise PermissionError(_NOT_AVAILABLE)
        return self._view(org_id, state, is_owner=actor == owner)

    def _view(self, org_id: str, state: State, *, is_owner: bool) -> dict[str, Any]:
        record = self.current(org_id)
        deadline = record.grace_deadline
        cancellable = (
            state is State.DELETION_REQUESTED
            and record.actor_kind is LifecycleActorKind.OWNER
            and record.previous_state is State.ACTIVE
            and deadline is not None
            and self.now() < deadline
            and not record.purge_now
        )
        return {
            "organization_id": org_id,
            "state": state.value,
            "reason": record.reason,
            "requested_by": record.actor_kind.value if record.actor_kind else None,
            "grace_deadline": deadline,
            "deletion_scheduled_for": deadline
            if state is State.DELETION_REQUESTED
            else None,
            "can_request_deletion": is_owner and state is State.ACTIVE,
            "can_cancel_deletion": is_owner and cancellable,
            "history": [
                {
                    "state": e.state.value,
                    "reason": e.reason,
                    "actor_kind": e.actor_kind.value,
                    "at": e.at,
                }
                for e in record.history
            ],
        }

    # ---- owner actions -------------------------------------------------
    def require_owner(self) -> Actor:
        """Return the owner actor or raise ``PermissionError``."""
        actor = effective_principal_id("local-developer")
        if actor != self.guard.current_settings().owner_principal_id:
            raise PermissionError("Only the organization owner can do this")
        return Actor.owner(actor)

    def request_deletion(
        self, *, confirm_name: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Owner-requested deletion; the exact organization name is required."""
        text = clean_reason(reason, required=False)
        with self.guard.mutation():
            actor = self.require_owner()
            org_id = effective_org_id()
            if confirm_name != self.organizations.get(org_id).name:
                raise ValueError("Type the organization's exact name to confirm")
            return self._deletion_requested(org_id, actor, text)

    def cancel_deletion(self) -> dict[str, Any]:
        """Owner cancellation, only for an owner-requested deletion within grace."""
        with self.guard.mutation():
            actor = self.require_owner()
            org_id = effective_org_id()
            record = self.current(org_id)
            state = self.state_of(org_id)
            if state is State.ACTIVE:
                return self._view(org_id, state, is_owner=True)
            if state is not State.DELETION_REQUESTED:
                raise LifecycleConflict("Deletion has already started")
            if record.actor_kind is not LifecycleActorKind.OWNER:
                raise PermissionError(
                    "An operator requested this deletion; ask the operator to cancel"
                )
            if record.purge_now or (
                record.grace_deadline is not None
                and self.now() >= record.grace_deadline
            ):
                raise LifecycleConflict("The grace period has ended")
            return self._restore(org_id, actor, "Deletion cancelled by the owner")

    # ---- operator actions ---------------------------------------------
    @contextmanager
    def _operator_scope(self, org_id: str, actor: Actor) -> Iterator[None]:
        """Bind the organization as the tenant scope for an operator action."""
        token = set_request_identity(actor.audit_id, org_id, "lifecycle")
        try:
            with self.guard.mutation():
                self.organizations.get(org_id)  # unknown organization -> KeyError
                yield
        finally:
            reset_request_principal(token)

    def suspend(self, org_id: str, operator: str, reason: str) -> dict[str, Any]:
        """Operator suspension; only an operator can undo it."""
        actor, text = Actor.operator(operator), clean_reason(reason, required=True)
        with self._operator_scope(org_id, actor):
            state = self.state_of(org_id)
            if state is State.SUSPENDED:
                return self._summary(org_id)
            self._require(state, {State.ACTIVE}, "suspend")
            self._write(
                org_id, State.SUSPENDED, actor, text, "suspended", previous=state
            )
            return self._summary(org_id)

    def resume(self, org_id: str, operator: str, reason: str) -> dict[str, Any]:
        """Operator resume. Work missed while suspended is never replayed."""
        actor, text = Actor.operator(operator), clean_reason(reason, required=True)
        with self._operator_scope(org_id, actor):
            state = self.state_of(org_id)
            if state is State.ACTIVE:
                return self._summary(org_id)
            self._require(state, {State.SUSPENDED}, "resume")
            self._write(org_id, State.ACTIVE, actor, text, "resumed", previous=state)
            return self._summary(org_id)

    def operator_request_deletion(
        self, org_id: str, operator: str, reason: str
    ) -> dict[str, Any]:
        """Operator-requested deletion starts the same grace period."""
        actor, text = Actor.operator(operator), clean_reason(reason, required=True)
        with self._operator_scope(org_id, actor):
            state = self.state_of(org_id)
            if state in {State.DELETING, State.DELETED}:
                return self._summary(org_id)
            return self._deletion_requested(org_id, actor, text, summary=True)

    def operator_cancel_deletion(
        self, org_id: str, operator: str, reason: str
    ) -> dict[str, Any]:
        """Return a pending deletion to the state it was requested from."""
        actor, text = Actor.operator(operator), clean_reason(reason, required=True)
        with self._operator_scope(org_id, actor):
            state = self.state_of(org_id)
            if state in {State.ACTIVE, State.SUSPENDED}:
                return self._summary(org_id)
            self._require(state, {State.DELETION_REQUESTED}, "cancel deletion of")
            self._restore(org_id, actor, text)
            return self._summary(org_id)

    def purge_now(
        self,
        org_id: str,
        operator: str,
        reason: str,
        confirm_organization_name: str | None,
    ) -> dict[str, Any]:
        """Skip the remaining grace period; needs the exact organization name."""
        actor, text = Actor.operator(operator), clean_reason(reason, required=True)
        with self._operator_scope(org_id, actor):
            state = self.state_of(org_id)
            if state in {State.DELETING, State.DELETED}:
                return self._summary(org_id)
            self._require(state, {State.DELETION_REQUESTED}, "purge")
            if confirm_organization_name != self.organizations.get(org_id).name:
                raise ValueError(
                    "--confirm-organization-name must equal the organization's name"
                )
            self._write(org_id, State.DELETING, actor, text, "deleting", purge_now=True)
            return self._summary(org_id)

    # ---- worker --------------------------------------------------------
    def begin_deleting(self, org_id: str) -> bool:
        """Move one expired DELETION_REQUESTED organization to DELETING."""
        actor = Actor.system()
        token = set_request_identity(actor.audit_id, org_id, "lifecycle")
        try:
            with self.guard.mutation():
                record = self.current(org_id)
                if self.state_of(org_id) is not State.DELETION_REQUESTED:
                    return False
                due = record.grace_deadline
                if not record.purge_now and (due is None or self.now() < due):
                    return False
                self._write(
                    org_id,
                    State.DELETING,
                    actor,
                    "Grace period elapsed",
                    "deleting",
                )
                return True
        finally:
            reset_request_principal(token)

    def begin_restore_purge(self, org_id: str) -> bool:
        """Force DELETING for an organization a tombstone says was purged.

        Used only by restore reconciliation after an older backup revived the
        organization. Returns False when the organization is unknown (created
        after the backup) or already DELETING or DELETED.
        """
        actor = Actor.system()
        try:
            with self._operator_scope(org_id, actor):
                if self.state_of(org_id) in {State.DELETING, State.DELETED}:
                    return False
                self._write(
                    org_id,
                    State.DELETING,
                    actor,
                    "Restore reconciliation: deletion recorded after the backup",
                    "deleting",
                    purge_now=True,
                )
                return True
        except KeyError:
            return False

    def finish_deletion(self, org_id: str) -> None:
        """Tombstone a fully purged organization: DELETED is terminal."""
        now = self.now()
        self._write(org_id, State.DELETED, Actor.system(), "Purge completed", "deleted")
        organization = self.organizations.get(org_id)
        self.organizations.put(
            organization.model_copy(update={"name": DELETED_NAME_PLACEHOLDER})
        )
        self.lifecycle.put(self.current(org_id).model_copy(update={"deleted_at": now}))

    # ---- operator reads ------------------------------------------------
    def operator_status(self, org_id: str) -> dict[str, Any]:
        """Read-only state, deadline and checkpoint for the operator."""
        self.organizations.get(org_id)
        return self._summary(org_id)

    def operator_list(self) -> list[dict[str, Any]]:
        """Every organization with its effective state (operator, owner access)."""
        return [
            {
                "organization_id": organization.id,
                "name": organization.name,
                "state": self.state_of(organization.id).value,
            }
            for organization in sorted(self.organizations.list(), key=lambda o: o.id)
        ]

    # ---- shared transitions -------------------------------------------
    @staticmethod
    def _require(state: State, allowed: set[State], verb: str) -> None:
        if state not in allowed:
            raise LifecycleConflict(f"Cannot {verb} an organization in {state.value}")

    def _deletion_requested(
        self, org_id: str, actor: Actor, reason: str | None, *, summary: bool = False
    ) -> dict[str, Any]:
        state = self.state_of(org_id)
        if state is not State.DELETION_REQUESTED:
            allowed = {State.ACTIVE, State.SUSPENDED}
            if actor.kind is LifecycleActorKind.OWNER:
                allowed = {State.ACTIVE}
            self._require(state, allowed, "request deletion of")
            deadline = self.now() + timedelta(days=self.grace_days)
            self._write(
                org_id,
                State.DELETION_REQUESTED,
                actor,
                reason,
                "deletion_requested",
                previous=state,
                deadline=deadline,
            )
        if summary:
            return self._summary(org_id)
        return self._view(org_id, self.state_of(org_id), is_owner=True)

    def _restore(self, org_id: str, actor: Actor, reason: str | None) -> dict[str, Any]:
        record = self.current(org_id)
        target = record.previous_state or State.ACTIVE
        self._write(org_id, target, actor, reason, "deletion_cancelled", previous=None)
        return self._view(org_id, target, is_owner=True)

    def _summary(self, org_id: str) -> dict[str, Any]:
        record = self.current(org_id)
        return {
            "organization_id": org_id,
            "state": self.state_of(org_id).value,
            "reason": record.reason,
            "grace_deadline": record.grace_deadline,
            "purge_step": record.purge_step,
            "purged_rows": record.purged_rows,
            "history_length": len(record.history),
        }

    def _write(
        self,
        org_id: str,
        target: State,
        actor: Actor,
        reason: str | None,
        audit_suffix: str,
        *,
        previous: State | None = None,
        deadline: datetime | None = None,
        purge_now: bool = False,
    ) -> None:
        """Persist projection, lifecycle record and audit event together."""
        now = self.now()
        organization = self.organizations.get(org_id)
        record = self.current(org_id)
        before = self.state_of(org_id)
        entry = LifecycleHistoryEntry(
            state=target,
            reason=reason,
            actor_kind=actor.kind,
            actor_ref=actor.ref,
            at=now,
        )
        updated = record.model_copy(
            update={
                "state": target,
                "previous_state": previous,
                "reason": reason,
                "actor_kind": actor.kind,
                "actor_ref": actor.ref,
                "grace_deadline": deadline,
                "purge_now": purge_now,
                "purge_step": None,
                "clean_ticks": 0,
                "revision": record.revision + 1,
                "updated_at": now,
                "history": [*record.history, entry][-_MAX_HISTORY:],
            }
        )
        self.organizations.put(
            organization.model_copy(
                update={"status": STATUS_FOR_STATE[target], "updated_at": now}
            )
        )
        self.lifecycle.put(updated)
        self.audit(
            org_id,
            actor,
            f"organization.lifecycle.{audit_suffix}",
            f"Organization moved from {before.value} to {target.value}",
            {
                "actor_kind": actor.kind.value,
                "actor_ref": actor.ref,
                "from_state": before.value,
                "to_state": target.value,
                "reason": reason,
                "grace_deadline": deadline.isoformat() if deadline else None,
            },
        )

    def audit(
        self,
        org_id: str,
        actor: Actor,
        action: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> None:
        """Write one organization-scoped, secret-free audit event."""
        self.guard.audit.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor.audit_id,
                action=action,
                target_type="organization",
                target_id=org_id,
                summary=summary,
                request_id=new_id(),
                metadata=metadata,
                created_at=self.now(),
            )
        )
