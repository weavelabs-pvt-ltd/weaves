"""The one place that derives an organization's effective lifecycle state.

``Organization.status`` retains its three original values so the previous
release's strict reader still parses every organization. The richer state is in
``OrganizationLifecycle``. The two are always written in one unit of work, and
every non-active lifecycle state projects to a non-active ``status``; the
phase-1 guards (work admission, workers, schedules, webhooks, selection) read
that projection and so stop new work in every non-active state, while
``effective_organization_state`` is the single reader that combines both.
"""

from __future__ import annotations

from contracts.v1 import (
    Organization,
    OrganizationLifecycle,
    OrganizationLifecycleState,
    OrganizationStatus,
)

State = OrganizationLifecycleState

STATUS_FOR_STATE: dict[State, OrganizationStatus] = {
    State.ACTIVE: OrganizationStatus.ACTIVE,
    State.SUSPENDED: OrganizationStatus.SUSPENDED,
    State.DELETION_REQUESTED: OrganizationStatus.SUSPENDED,
    State.DELETING: OrganizationStatus.SUSPENDED,
    State.DELETED: OrganizationStatus.DELETED,
}

# The explicit narrow allow-list of (method, path) the middleware lets the owner
# reach while deletion is pending. It is not a loosening of the work guard:
# every other route stays refused, and only the owner is admitted.
RECOVERY_ROUTES = frozenset(
    {
        ("GET", "/api/v0/organization/lifecycle"),
        ("POST", "/api/v0/organization/deletion-cancel"),
        ("POST", "/api/v0/organization/export"),
    }
)


def effective_organization_state(
    organization: Organization, lifecycle: OrganizationLifecycle | None
) -> OrganizationLifecycleState:
    """Return the most restrictive state the two records agree on or imply."""
    if organization.status is OrganizationStatus.DELETED:
        return State.DELETED
    if lifecycle is None or lifecycle.state is State.ACTIVE:
        if organization.status is OrganizationStatus.ACTIVE:
            return State.ACTIVE
        return State.SUSPENDED
    return lifecycle.state


def is_recovery_request(method: str, path: str) -> bool:
    """Report whether a request is on the owner's pending-deletion allow-list."""
    return (method.upper(), path.rstrip("/")) in RECOVERY_ROUTES
