"""Operator-only per-organization limit overrides (never reachable over HTTP)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from application.ports.limits import LimitPolicy, OrgLimits
from application.ports.repository import Repository
from contracts.v1 import AuditEvent, AuditTargetScope, Organization
from contracts.v1.limits import OrganizationLimitOverride
from modules.limits.policy import OVERRIDABLE_FIELDS, apply_override
from shared.clock import new_id, utc_now

_REASON_MAX = 500


def _text(value: str, name: str, limit: int) -> str:
    clean = value.strip()
    if not clean or len(clean) > limit or not clean.isprintable():
        raise ValueError(f"{name} must be printable text up to {limit} characters")
    return clean


class OrganizationLimitOverrides:
    """Set, clear and inspect one organization's limit overrides, with audit."""

    def __init__(
        self,
        *,
        overrides: Repository[OrganizationLimitOverride],
        organizations: Repository[Organization],
        audit_events: Repository[AuditEvent],
        defaults: LimitPolicy,
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind persistence, the environment defaults and the transaction."""
        self._overrides = overrides
        self._organizations = organizations
        self._audit = audit_events
        self._defaults = defaults
        self._unit_of_work = unit_of_work

    def _current(self, org_id: str) -> OrganizationLimitOverride | None:
        try:
            return self._overrides.get_scoped(org_id, org_id)
        except KeyError:
            return None

    def _view(self, org_id: str) -> dict[str, Any]:
        base: OrgLimits = self._defaults.for_org(org_id)
        record = self._current(org_id)
        effective = apply_override(base, record) if record is not None else base
        return {
            "organization_id": org_id,
            "mode": base.mode,
            "override": (
                None
                if record is None
                else {
                    **{
                        name: getattr(record, name)
                        for name in OVERRIDABLE_FIELDS
                        if getattr(record, name) is not None
                    },
                    "updated_by": record.updated_by,
                    "reason": record.reason,
                    "updated_at": record.updated_at.isoformat(),
                }
            ),
            "effective": {
                name: getattr(effective, name) for name in OVERRIDABLE_FIELDS
            },
            "defaults": {name: getattr(base, name) for name in OVERRIDABLE_FIELDS},
        }

    def show(self, org_id: str) -> dict[str, Any]:
        """Defaults, override and effective limits of one organization."""
        self._organizations.get(org_id)  # unknown organization -> KeyError
        return self._view(org_id)

    def list(self) -> list[dict[str, Any]]:
        """Every organization that has an override."""
        return [self._view(record.org_id) for record in self._overrides.list()]

    def set(
        self, org_id: str, operator: str, reason: str, values: dict[str, int]
    ) -> dict[str, Any]:
        """Merge ``values`` into the organization's override and audit it."""
        who = _text(operator, "operator", 320)
        why = _text(reason, "reason", _REASON_MAX)
        unknown = set(values) - set(OVERRIDABLE_FIELDS)
        if unknown or not values:
            raise ValueError("choose at least one known limit to override")
        self._organizations.get(org_id)
        now = utc_now()
        with self._unit_of_work():
            existing = self._current(org_id)
            fields = {
                name: getattr(existing, name) if existing is not None else None
                for name in OVERRIDABLE_FIELDS
            }
            fields.update(values)
            record = OrganizationLimitOverride.model_validate(
                {
                    **fields,
                    "org_id": org_id,
                    "updated_by": who,
                    "reason": why,
                    "created_at": existing.created_at if existing is not None else now,
                    "updated_at": now,
                }
            )
            self._overrides.put(record)
            self._record(org_id, who, "organization.limits.override_set", why, values)
        return self._view(org_id)

    def clear(self, org_id: str, operator: str, reason: str) -> dict[str, Any]:
        """Remove the override so the environment defaults apply again."""
        who = _text(operator, "operator", 320)
        why = _text(reason, "reason", _REASON_MAX)
        self._organizations.get(org_id)
        with self._unit_of_work():
            if self._current(org_id) is not None:
                self._overrides.delete(org_id)
                self._record(
                    org_id, who, "organization.limits.override_cleared", why, {}
                )
        return self._view(org_id)

    def _record(
        self, org_id: str, operator: str, action: str, reason: str, values: Any
    ) -> None:
        """Write the organization-scoped audit event for an operator change."""
        actor = f"operator:{operator}"
        if len(actor) > 128:
            actor = "operator:" + new_id()
        event_id = new_id()
        self._audit.create(
            AuditEvent(
                audit_event_id=event_id,
                org_id=org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="organization_limits",
                target_id=org_id,
                summary=reason,
                request_id=event_id,
                metadata={"limits": dict(values), "actor_kind": "operator"},
                created_at=utc_now(),
            )
        )
